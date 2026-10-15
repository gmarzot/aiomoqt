"""A WebTransport client offering moqt-NN closes the session with WT_ALPN_ERROR
when the server's response carries no WT-Protocol (draft-ietf-webtrans-http3-16
§3.3); a d14-only offer negotiates nothing and connects as before.
"""
import asyncio

import pytest
from aiopquic.asyncio.webtransport import serve_webtransport

from aiomoqt.client import MOQTClient
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


async def _closed_code(session, timeout=2.0):
    await session.wait_closed(timeout)
    return session._session_close_event.error_code


@pytest.mark.parametrize("drafts", [[18], [18, 16, 14]], ids=["d18", "d18-16-14"])
async def test_missing_wt_protocol_closes_with_wt_alpn_error(drafts):
    port = _BASE_PORT + len(drafts)
    server, sessions = await _bare_wt_server(port)
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=False,
                            verify_tls=False, supported_drafts=drafts)
        with pytest.raises(MOQTNegotiationError, match="no WT-Protocol"):
            async with client.connect():
                pytest.fail("session yielded without a WT-Protocol")
        assert len(sessions) == 1
        assert await _closed_code(sessions[0]) == WT_ALPN_ERROR
    finally:
        await server.aclose()


async def test_d14_only_offer_connects_without_wt_protocol():
    port = _BASE_PORT + 9
    server, sessions = await _bare_wt_server(port)
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=False,
                            verify_tls=False, supported_drafts=[14])
        async with client.connect() as session:
            assert session.negotiated_protocol is None
            await asyncio.sleep(0.05)
            assert not session.session_closed
        assert len(sessions) == 1
    finally:
        await server.aclose()
