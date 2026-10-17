import asyncio
import contextlib
import ssl

import pytest

from teleport.core.framing import FramingError
from teleport.net.stream import read_message, write_message
from teleport.net.tls import (
    FingerprintMismatch,
    client_context,
    peer_fingerprint,
    server_context,
    verify_pin,
)
from teleport.trust.identity import load_or_create_identity


@pytest.fixture
def identity(tmp_path):
    return load_or_create_identity(tmp_path / "server")


async def start_echo_server(ctx: ssl.SSLContext):
    """Server trả lại mọi message, kèm fingerprint chứng chỉ client nếu có."""

    async def on_connect(reader, writer):
        try:
            client_fp = peer_fingerprint(writer)
        except ValueError:
            client_fp = None
        while (msg := await read_message(reader)) is not None:
            await write_message(writer, {"echo": msg, "client_fp": client_fp})
        writer.close()

    server = await asyncio.start_server(on_connect, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


async def round_trip(port: int, ctx: ssl.SSLContext) -> dict | None:
    """Gửi một message và đọc trả lời. None nghĩa là server đã từ chối kết nối."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    try:
        await write_message(writer, {"text": "xin chào qua TLS"})
        return await asyncio.wait_for(read_message(reader), 5)
    except (ssl.SSLError, ConnectionError, FramingError):
        return None
    finally:
        writer.close()
        with contextlib.suppress(ssl.SSLError, ConnectionError):
            await writer.wait_closed()


@pytest.mark.integration
async def test_tls13_connection_exposes_server_fingerprint(identity):
    server, port = await start_echo_server(server_context(identity))
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context())
        try:
            assert writer.get_extra_info("ssl_object").version() == "TLSv1.3"
            assert peer_fingerprint(writer) == identity.fingerprint
            await write_message(writer, {"text": "xin chào qua TLS"})
            reply = await read_message(reader)
            assert reply == {"echo": {"text": "xin chào qua TLS"}, "client_fp": None}
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.integration
async def test_client_detects_wrong_server(identity, tmp_path):
    impostor = load_or_create_identity(tmp_path / "impostor")
    server, port = await start_echo_server(server_context(impostor))
    async with server:
        _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context())
        try:
            with pytest.raises(FingerprintMismatch):
                verify_pin(peer_fingerprint(writer), identity.short_fingerprint)
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.integration
async def test_mtls_accepts_trusted_client_and_sees_its_fingerprint(identity, tmp_path):
    trusted = load_or_create_identity(tmp_path / "trusted")
    server, port = await start_echo_server(server_context(identity, [trusted.cert_pem()]))
    async with server:
        reply = await round_trip(port, client_context(trusted))
    assert reply is not None
    assert reply["client_fp"] == trusted.fingerprint


@pytest.mark.integration
@pytest.mark.parametrize("client", ["stranger", "no_cert"])
async def test_mtls_rejects_unknown_or_anonymous_client(identity, tmp_path, client):
    trusted = load_or_create_identity(tmp_path / "trusted")
    stranger = load_or_create_identity(tmp_path / "stranger")
    ctx = client_context(stranger) if client == "stranger" else client_context()
    server, port = await start_echo_server(server_context(identity, [trusted.cert_pem()]))
    async with server:
        assert await round_trip(port, ctx) is None


@pytest.mark.integration
async def test_mtls_with_empty_trust_list_rejects_everyone(identity, tmp_path):
    someone = load_or_create_identity(tmp_path / "someone")
    server, port = await start_echo_server(server_context(identity, []))
    async with server:
        assert await round_trip(port, client_context(someone)) is None


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
