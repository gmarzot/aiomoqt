"""A WebTransport client settles its draft from the server's WT-Protocol when
it is one we offered; with no WT-Protocol, a d14 offer settles d14 in-band.
Anything else closes the WT session with WT_ALPN_ERROR
(draft-ietf-webtrans-http3-16 §3.3) and raises MOQTNegotiationError.
"""
import asyncio

import pytest
from aiopquic.asyncio.webtransport import serve_webtransport

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTSessionWTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import WT_ALPN_ERROR, MOQTNegotiationError
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17360


async def _bare_wt_server(port):
    """aiopquic WT server with no WT-Protocol allowlist; returns (server, sessions)."""
    sessions = []

    async def handler(session):
        sessions.append(session)

    server = await serve_webtransport("localhost", port, "/", handler=handler,
                                      cert_file=CERT, key_file=KEY)
    return server, sessions


async def _moqt_wt_server(port, drafts):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=False,
                        supported_drafts=drafts)
    return await server.serve()


def _client(port, drafts):
    return MOQTClient("localhost", port, path="/", use_quic=False,
                      verify_tls=False, supported_drafts=drafts)


async def _closed_code(session, timeout=2.0):
    await session.wait_closed(timeout)
    return session._session_close_event.error_code


async def _assert_alpn_error(port, drafts, match):
    server, sessions = await _bare_wt_server(port)
    try:
        with pytest.raises(MOQTNegotiationError, match=match):
            async with _client(port, drafts).connect() as session:
                await session.client_session_init(timeout=3)
        assert len(sessions) == 1
        assert await _closed_code(sessions[0]) == WT_ALPN_ERROR
    finally:
        await server.aclose()


async def test_missing_wt_protocol_closes_a_d18_offer():
    await _assert_alpn_error(_BASE_PORT, [18], r"None not in offer \['moqt-18'\]")


async def test_unoffered_wt_protocol_closes_even_with_d14_offered(monkeypatch):
    monkeypatch.setattr(MOQTSessionWTClient, "negotiated_protocol",
                        property(lambda self: "moqt-20"))
    await _assert_alpn_error(_BASE_PORT + 1, [18, 14], "'moqt-20' not in offer")


async def _settles(port, client_drafts, server_drafts, expected):
    handle = await _moqt_wt_server(port, server_drafts)
    try:
        async with _client(port, client_drafts).connect() as session:
            await session.client_session_init(timeout=5)
            assert session.negotiated_draft == expected
            assert await session.subscribe_namespace(
                "probe", wait_response=True) is not None
            assert session._close_err is None
            return session.negotiated_protocol
    finally:
        handle.close()


async def test_mixed_offer_without_wt_protocol_settles_d14():
    assert await _settles(_BASE_PORT + 2, [18, 16, 14], [14], 14) is None


async def test_d14_only_offer_settles_d14():
    assert await _settles(_BASE_PORT + 3, [14], [14], 14) is None


async def test_multi_draft_offer_settles_the_servers_choice():
    assert await _settles(_BASE_PORT + 4, [18, 16, 14], [16, 14], 16) == "moqt-16"
