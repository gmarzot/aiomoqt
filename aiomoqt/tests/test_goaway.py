"""A received GOAWAY is recorded and reported to `on_goaway`. After a
control-stream GOAWAY no new request is sent (§10.4 SHOULD NOT); a
GOAWAY on one request's stream (d18+, sent with `goaway(request_id=)`)
asks to migrate that request only."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17260
_DRAFTS = [14, 16, 18, 20]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case, draft, use_quic):
    return _BASE_PORT + 8 * case + 2 * _DRAFTS.index(draft) + (not use_quic)


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, rx):
    async def on_subscribe(session, msg):
        rx["session"] = session
        rx.setdefault("subs", {})[msg.track_name.decode()] = msg
        session.subscribe_ok(msg)

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


@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_control_goaway_stops_new_requests(draft, use_quic):
    rx, seen = {}, []
    async with _loopback(_port(0, draft, use_quic), draft, use_quic,
                         rx) as session:
        session.on_goaway = lambda msg, rid: seen.append((msg, rid))
        await session.subscribe("ns", "video", wait_response=True)
        rx["session"].goaway(timeout=500)
        await _until(lambda: seen)
        (msg, rid), = seen
        assert rid is None
        if draft >= 18:  # no Timeout field before d18
            assert msg.timeout == 500
        assert session.goaway_received is msg
        with pytest.raises(MOQTRequestError) as err:
            session.subscribe("ns", "audio")
        assert err.value.error_code == RequestErrorCode.GOING_AWAY
        await asyncio.sleep(0.1)
        assert set(rx["subs"]) == {"video"}
        assert session._close_err is None


@_TRANSPORTS
@pytest.mark.parametrize("draft", [18, 20])
async def test_request_goaway_migrates_one_request(draft, use_quic):
    rx, seen = {}, []
    async with _loopback(_port(1, draft, use_quic), draft, use_quic,
                         rx) as session:
        session.on_goaway = lambda msg, rid: seen.append((msg, rid))
        video = await session.subscribe("ns", "video", wait_response=True)
        await session.subscribe("ns", "audio", wait_response=True)
        rx["session"].goaway(request_id=rx["subs"]["video"].request_id)
        await _until(lambda: seen)
        (msg, rid), = seen
        assert rid == video.request_id
        assert session.goaway_received is None
        await session.subscribe("ns", "data", wait_response=True)
        assert session._close_err is None


def test_request_goaway_needs_d18():
    from aiomoqt.context import profile_for
    from aiomoqt.protocol import _MOQTSessionMixin
    s = object.__new__(_MOQTSessionMixin)
    s._profile = profile_for(16)
    s.negotiated_draft = 16
    s.is_client = False
    with pytest.raises(ValueError):
        s.goaway(request_id=1)
