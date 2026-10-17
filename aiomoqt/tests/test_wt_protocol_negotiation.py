"""WebTransport draft negotiation follows the server's draft order, as raw
QUIC ALPN does, and an offer with no supported draft is refused at CONNECT.
Both behaviours live in aiopquic's WT-Protocol selection, which takes the
server's `supported_drafts` order.
"""
import asyncio

import pytest
from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16900
_SERVER_DRAFTS = [22, 20, 18, 16, 14]
_ASCENDING = [14, 16, 18, 20, 22]

_PROBE_PORT = _BASE_PORT + 7
_probed = {}


async def _aiopquic_wt_selection():
    """What this aiopquic's WT-Protocol selection does, found by asking it:
    (picks by server order, refuses an offer with no match)."""
    if not _probed:
        async def handler(session):
            pass

        server = await serve_webtransport(
            "localhost", _PROBE_PORT, "/", handler=handler, cert_file=CERT,
            key_file=KEY, wt_supported_protocols=["p-b", "p-a"])
        try:
            async with connect_webtransport(
                    "localhost", _PROBE_PORT, "/", verify_peer=False,
                    wt_available_protocols=["p-a", "p-b"]) as wt:
                _probed["server_order"] = wt.negotiated_protocol == "p-b"
            try:
                async with connect_webtransport(
                        "localhost", _PROBE_PORT, "/", verify_peer=False,
                        wt_available_protocols=["p-c"], timeout=5.0):
                    _probed["refuses"] = False
            except Exception:
                _probed["refuses"] = True
        finally:
            await server.aclose()
    return _probed["server_order"], _probed["refuses"]


@pytest.fixture
async def wt_server_order():
    server_order, refuses = await _aiopquic_wt_selection()
    if not (server_order and refuses):
        pytest.skip("aiopquic selects the WT-Protocol by client order or "
                    "accepts an offer with no match")


requires_wt_server_order = pytest.mark.usefixtures("wt_server_order")


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
    handle, seen = await _start(port, [18, 16, 14], use_quic=False,
                                dual=dual)
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
    handle, _ = await _start(port, [18, 16, 14], use_quic=False)
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
