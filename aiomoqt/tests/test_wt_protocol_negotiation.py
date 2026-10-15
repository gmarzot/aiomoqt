"""WebTransport draft negotiation follows the server's draft order, as raw
QUIC ALPN does, and an offer with no supported draft is refused at CONNECT.
Both behaviours live in aiopquic's WT-Protocol selection, which takes the
server's `supported_drafts` order.
"""
import asyncio

import aiopquic
import pytest
from packaging.version import Version

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16900
_SERVER_DRAFTS = [22, 21, 20, 18, 16, 14]
_ASCENDING = [14, 16, 18, 20, 21, 22]

requires_wt_server_order = pytest.mark.skipif(
    Version(aiopquic.__version__) <= Version("0.5.0a4"),
    reason="aiopquic <= 0.5.0a4 selects the WT-Protocol by client order "
           "and accepts an offer with no match")


async def _start(port, drafts, *, use_quic=True, dual=False):
    """Start a server; returns (handle, sessions whose SETUP arrived)."""
    seen = []

    async def on_setup(session, msg):
        seen.append(session)
        _, default = session.CONTROL_REGISTRY[session.negotiated_draft][
            MOQTMessageType.SETUP]
        await default(session, msg)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=drafts)
    server.register_handler(MOQTMessageType.SETUP, on_setup)
    handle = await (server.serve_dual() if dual else server.serve())
    return handle, seen


def _client(port, use_quic, drafts):
    return MOQTClient("localhost", port, path="/", use_quic=use_quic,
                      verify_tls=False, supported_drafts=drafts)


async def _negotiate(port, use_quic, drafts):
    async with _client(port, use_quic, drafts).connect() as session:
        await session.client_session_init()
        return (session.negotiated_draft,
                getattr(session, "negotiated_protocol", None))


@requires_wt_server_order
@pytest.mark.parametrize("dual", [False, True], ids=["serve", "serve_dual"])
async def test_wt_ascending_offer_gets_the_servers_highest(dual):
    port = _BASE_PORT + (1 if dual else 0)
    handle, seen = await _start(port, _SERVER_DRAFTS, use_quic=False, dual=dual)
    try:
        draft, proto = await _negotiate(port, False, _ASCENDING)
        assert (draft, proto) == (22, "moqt-22")
        assert [s.negotiated_draft for s in seen] == [22]
    finally:
        handle.close()


async def test_quic_ascending_offer_gets_the_servers_highest():
    port = _BASE_PORT + 2
    handle, seen = await _start(port, _SERVER_DRAFTS)
    try:
        draft, _ = await _negotiate(port, True, _ASCENDING)
        assert draft == 22
        assert [s.negotiated_draft for s in seen] == [22]
    finally:
        handle.close()


@requires_wt_server_order
@pytest.mark.parametrize("dual", [False, True], ids=["serve", "serve_dual"])
async def test_wt_offer_with_no_supported_draft_is_refused(dual):
    """A d22-only client against a 18/16/14 server fails at CONNECT with
    the HTTP status, before any MoQT message is exchanged."""
    port = _BASE_PORT + 3 + (1 if dual else 0)
    handle, seen = await _start(port, None, use_quic=False, dual=dual)
    try:
        with pytest.raises(ConnectionError, match=r"refused \(HTTP 400\)"):
            async with asyncio.timeout(8):
                await _negotiate(port, False, 22)
        assert seen == []
    finally:
        handle.close()


@requires_wt_server_order
async def test_relay_probe_reports_a_wt_refusal():
    from aiomoqt.tools import relay_probe
    port = _BASE_PORT + 5
    handle, _ = await _start(port, None, use_quic=False)
    try:
        r = await relay_probe.probe_version(
            "localhost", port, "/", False, 22, verify_tls=False)
        assert not r["live"], r
        assert "HTTP 400" in (r["error"] or ""), r
    finally:
        handle.close()


async def test_wt_d14_client_offering_no_protocol_still_connects():
    port = _BASE_PORT + 6
    handle, _ = await _start(port, None, use_quic=False)
    try:
        draft, proto = await _negotiate(port, False, 14)
        assert (draft, proto) == (14, None)
    finally:
        handle.close()
