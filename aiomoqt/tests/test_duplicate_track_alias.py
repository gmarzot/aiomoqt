"""A SUBSCRIBE_OK or PUBLISH using the Track Alias of a different track
with an Established subscription closes the session with
DUPLICATE_TRACK_ALIAS. A subscription PUBLISH_DONE ended frees its alias
at once, though its objects are still taken for a grace period."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SessionCloseCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17240
_DRAFTS = [14, 16, 18]


def _port(case, draft, use_quic):
    return _BASE_PORT + 6 * case + 2 * _DRAFTS.index(draft) + (not use_quic)


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, rx):
    async def on_subscribe(session, msg):
        rx["session"] = session
        rx.setdefault("subs", {})[msg.track_name.decode()] = msg
        session._next_track_alias = 7  # every track gets alias 7

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        handle.close()


async def _until(pred, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


async def _subscribed(session, rx, names):
    for name in names:
        session.subscribe("ns", name)
    await _until(lambda: len(rx.get("subs", ())) == len(names))
    return rx["session"]


async def _closed_with(session):
    try:
        await _until(lambda: session._close_err is not None)
    except TimeoutError:
        return None
    return session._close_err[0]


_CASES = pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])


@_CASES
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_subscribe_ok_reusing_an_alias_closes(draft, use_quic):
    rx = {}
    async with _loopback(_port(0, draft, use_quic), draft, use_quic,
                         rx) as session:
        srv = await _subscribed(session, rx, ("video", "audio"))
        srv.subscribe_ok(rx["subs"]["video"])
        await asyncio.sleep(0.1)
        assert session._close_err is None
        srv._next_track_alias = 7
        srv.subscribe_ok(rx["subs"]["audio"])
        assert await _closed_with(session) == \
            SessionCloseCode.DUPLICATE_TRACK_ALIAS


@_CASES
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_publish_reusing_an_alias_closes(draft, use_quic):
    rx = {}
    async with _loopback(_port(1, draft, use_quic), draft, use_quic,
                         rx) as session:
        srv = await _subscribed(session, rx, ("video",))
        srv.subscribe_ok(rx["subs"]["video"])
        await asyncio.sleep(0.1)
        srv._next_track_alias = 7
        srv.publish("ns", "audio")
        assert await _closed_with(session) == \
            SessionCloseCode.DUPLICATE_TRACK_ALIAS


@_CASES
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_alias_of_a_finished_subscription_is_free(draft, use_quic):
    rx = {}
    async with _loopback(_port(2, draft, use_quic), draft, use_quic,
                         rx) as session:
        srv = await _subscribed(session, rx, ("video", "audio"))
        srv.subscribe_ok(rx["subs"]["video"])
        await asyncio.sleep(0.1)
        srv.subscribe_done(rx["subs"]["video"].request_id)
        await asyncio.sleep(0.1)
        srv._next_track_alias = 7
        srv.subscribe_ok(rx["subs"]["audio"])
        assert await _closed_with(session) is None
