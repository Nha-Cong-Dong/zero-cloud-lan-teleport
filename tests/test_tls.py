import asyncio

import pytest

from teleport.core.framing import read_message, write_message
from teleport.net.identity import load_or_create_identity
from teleport.net.tls import (
    FingerprintMismatch,
    client_context,
    peer_fingerprint,
    server_context,
    verify_pin,
)


@pytest.fixture
def identity(tmp_path):
    return load_or_create_identity(tmp_path / "server")


async def start_echo_server(identity):
    async def on_connect(reader, writer):
        while (msg := await read_message(reader)) is not None:
            await write_message(writer, {"echo": msg})
        writer.close()

    server = await asyncio.start_server(on_connect, "127.0.0.1", 0, ssl=server_context(identity))
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_tls13_connection_exposes_server_fingerprint(identity):
    server, port = await start_echo_server(identity)
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context())
        try:
            assert writer.get_extra_info("ssl_object").version() == "TLSv1.3"
            assert peer_fingerprint(writer) == identity.fingerprint

            await write_message(writer, {"text": "xin chào qua TLS"})
            assert await read_message(reader) == {"echo": {"text": "xin chào qua TLS"}}
        finally:
            writer.close()
            await writer.wait_closed()


async def test_client_detects_wrong_server(identity, tmp_path):
    impostor = load_or_create_identity(tmp_path / "impostor")
    server, port = await start_echo_server(impostor)
    async with server:
        _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context())
        try:
            with pytest.raises(FingerprintMismatch):
                verify_pin(peer_fingerprint(writer), identity.short_fingerprint)
        finally:
            writer.close()
            await writer.wait_closed()


FP = "ab" * 32


@pytest.mark.parametrize("pin", [FP, FP[:16], "AB:AB:AB:AB:AB:AB:AB:AB", " abab abab abab abab "])
def test_verify_pin_accepts_matching_prefix(pin):
    verify_pin(FP, pin)


def test_verify_pin_rejects_mismatch():
    with pytest.raises(FingerprintMismatch):
        verify_pin(FP, "ab" * 7 + "ac")


@pytest.mark.parametrize("pin", ["abab", "zz" * 8, "đ" * 16])
def test_verify_pin_rejects_short_or_non_hex(pin):
    with pytest.raises(ValueError):
        verify_pin(FP, pin)
