"""A refused or cancelled request leaves no per-request state behind, on
either end: hundreds of them return every per-request table to where it
started. At d18 more refusals than the transport's bidi stream credit
(512) also checks that each request stream closes.
"""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 26100
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_DRAFTS = pytest.mark.parametrize("draft", [16, 18])
_REFUSALS = 600
_CANCELS = 100

# Per-request tables of the session; each must return to its baseline.
TABLES = ("_peer_requests", "_requests", "_pending_requests",
          "_request_cancel_handlers", "_publish_done_handlers",
          "_pending_object_handlers", "_pending_reply_hooks",
          "_bidi_streams", "_bidi_stream_requests", "_control_chains",
          "_cancelled_request_streams", "_stopped_streams", "_track_aliases",
          "_published_aliases", "_subscriptions", "_tx_updates",
          "_fill_requests", "_subscribe_tracks_params")


def _port(case: int, draft: int, use_quic: bool) -> int:
    return _BASE_PORT + 8 * case + 2 * (draft == 18) + (not use_quic)


def sizes(session) -> dict:
    return {t: len(getattr(session, t)) for t in TABLES}


async def settled(session, baseline: dict, timeout: float = 5.0) -> dict:
    """The tables still above baseline once the session settles."""
    grown = {}
    try:
        async with asyncio.timeout(timeout):
            while True:
                now = sizes(session)
                grown = {t: (baseline[t], n) for t, n in now.items()
                         if n > baseline[t]}
                if not grown:
                    return grown
                await asyncio.sleep(0.05)
    except TimeoutError:
        return grown


@contextlib.asynccontextmanager
async def loopback(port, draft, use_quic, on_subscribe=None):
    """(client session, server session) on one loopback connection."""
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    if on_subscribe is not None:
        server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            for _ in range(100):
                if server._live:
                    break
                await asyncio.sleep(0.01)
            yield session, next(iter(server._live))
    finally:
        handle.close()


async def _refuse_in_handler(session, msg):
    session.subscribe_error(msg.request_id,
                            error_code=RequestErrorCode.UNAUTHORIZED,
                            reason="refused by the handler")


@_TRANSPORTS
@_DRAFTS
@pytest.mark.parametrize("by", ["default", "handler"])
async def test_refused_subscribes_leave_no_state(draft, use_quic, by):
    handler = _refuse_in_handler if by == "handler" else None
    port = _port(by == "handler", draft, use_quic)
    async with loopback(port, draft, use_quic, handler) as (client, server):
        before_c, before_s = sizes(client), sizes(server)
        for i in range(_REFUSALS):
            with pytest.raises(MOQTRequestError):
                await asyncio.wait_for(
                    client.subscribe("refused", f"t{i}", wait_response=True),
                    5)
        assert await settled(server, before_s) == {}
        assert await settled(client, before_c) == {}


async def _hold(session, msg):
    """Never answers."""


@_TRANSPORTS
@_DRAFTS
async def test_cancelled_subscribes_leave_no_state(draft, use_quic):
    # UNSUBSCRIBE at d16; a terminated request stream at d18.
    port = _port(2, draft, use_quic)
    async with loopback(port, draft, use_quic, _hold) as (client, server):
        before_c, before_s = sizes(client), sizes(server)
        msgs = [client.subscribe("held", f"t{i}") for i in range(_CANCELS)]
        assert await _until(lambda: len(server._peer_requests)
                            >= before_s["_peer_requests"] + _CANCELS)
        for msg in msgs:
            client.unsubscribe(msg.request_id)
        assert await settled(server, before_s) == {}
        assert await settled(client, before_c) == {}


@_TRANSPORTS
async def test_subscriber_fin_then_refusal_leaves_no_state(use_quic):
    # d18: the subscriber ends its half of each request stream before the
    # refusal, so the refusal is what closes the stream.
    held = []

    async def later(session, msg):
        held.append(msg.request_id)

    port = _port(3, 18, use_quic)
    async with loopback(port, 18, use_quic, later) as (client, server):
        before_c, before_s = sizes(client), sizes(server)
        msgs = [client.subscribe("fin", f"t{i}") for i in range(_CANCELS)]
        assert await _until(lambda: all(m.request_id in client._bidi_streams
                                        for m in msgs))
        for msg in msgs:
            client.stream_fin(client._bidi_streams[msg.request_id])
        assert await _until(lambda: len(held) == _CANCELS)
        await asyncio.sleep(0.1)
        for rid in held:
            server.subscribe_error(rid, error_code=RequestErrorCode.TIMEOUT,
                                   reason="refused after FIN")
        assert await settled(server, before_s) == {}
        assert await settled(client, before_c) == {}


async def _until(cond, timeout: float = 5.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.02)
    except TimeoutError:
        return False
    return True
