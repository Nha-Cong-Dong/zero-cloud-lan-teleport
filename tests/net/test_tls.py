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
    verify_fingerprint,
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
    server, port = await start_echo_server(server_context(identity, [], allow_anonymous=True))
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
    server, port = await start_echo_server(server_context(impostor, [], allow_anonymous=True))
    async with server:
        _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context())
        try:
            with pytest.raises(FingerprintMismatch):
                verify_fingerprint(peer_fingerprint(writer), identity.fingerprint)
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


@pytest.mark.integration
async def test_single_port_accepts_trusted_and_anonymous_clients(identity, tmp_path):
    """Một cổng (protocol.md mục 2): client tin cậy có fingerprint, client ẩn danh thì không."""
    trusted = load_or_create_identity(tmp_path / "trusted")
    ctx = server_context(identity, [trusted.cert_pem()], allow_anonymous=True)
    server, port = await start_echo_server(ctx)
    async with server:
        trusted_reply = await round_trip(port, client_context(trusted))
        anonymous_reply = await round_trip(port, client_context())
    assert trusted_reply is not None
    assert trusted_reply["client_fp"] == trusted.fingerprint
    assert anonymous_reply is not None
    assert anonymous_reply["client_fp"] is None


@pytest.mark.integration
@pytest.mark.parametrize("trusted_list", ["one", "empty"])
async def test_single_port_still_rejects_client_with_unknown_cert(identity, tmp_path, trusted_list):
    """Gửi chứng chỉ lạ thì bị từ chối khi bắt tay, dù cổng cho phép kết nối ẩn danh."""
    trusted = load_or_create_identity(tmp_path / "trusted")
    stranger = load_or_create_identity(tmp_path / "stranger")
    certs = [trusted.cert_pem()] if trusted_list == "one" else []
    server, port = await start_echo_server(server_context(identity, certs, allow_anonymous=True))
    async with server:
        assert await round_trip(port, client_context(stranger)) is None


@pytest.mark.integration
async def test_server_issues_no_session_ticket(identity, tmp_path):
    """Không có ticket thì không dùng lại được session để né kiểm tra sau khi bị hủy ghép đôi."""
    trusted = load_or_create_identity(tmp_path / "trusted")
    ctx = server_context(identity, [trusted.cert_pem()], allow_anonymous=True)
    assert ctx.num_tickets == 0
    server, port = await start_echo_server(ctx)
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context(trusted))
        try:
            await write_message(writer, {"text": "xin chào"})
            assert await asyncio.wait_for(read_message(reader), 5) is not None
            session = writer.get_extra_info("ssl_object").session
            assert session is None or not session.has_ticket
        finally:
            writer.close()
            await writer.wait_closed()


FP = "ab" * 32


def test_verify_fingerprint_accepts_equal():
    verify_fingerprint(FP, FP)


@pytest.mark.parametrize(
    "other",
    ["ab" * 31 + "ac", FP[:16], FP + "ab", "", FP.upper(), "đ" * 64, "\uff41" * 64, None, b"ab" * 32],
    ids=repr,
)
def test_verify_fingerprint_rejects_anything_else(other):
    """Kể cả giá trị sai dạng hay không phải ASCII: chỉ một loại lỗi cho bên gọi bắt."""
    with pytest.raises(FingerprintMismatch):
        verify_fingerprint(FP, other)
    with pytest.raises(FingerprintMismatch):
        verify_fingerprint(other, FP)
