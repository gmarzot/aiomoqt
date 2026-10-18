"""Relay under subscriber churn: one publisher, subscribers that leave
mid-stream by UNSUBSCRIBE or by closing their session."""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.track import PublishedTrack
from aiomoqt.types import SessionCloseCode
from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 18300
_NS = "relay/churn"
_KEY = ((b"relay", b"churn"), b"live")
_CHURN = 9
# Reaper delay, as on a loaded host: the fan-out meets the closed session
# before the relay has forgotten it.
_REAP_LAG_S = 0.5
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


async def _until(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond() and loop.time() < end:
        await asyncio.sleep(0.02)
    return cond()


@contextlib.asynccontextmanager
async def _relay_with_publisher(port, use_quic):
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()

    def _client():
        return MOQTClient("localhost", port, path="/", use_quic=use_quic,
                          verify_tls=False, supported_drafts=18)
    try:
        async with _client().connect() as pub:
            await pub.client_session_init()
            track = PublishedTrack(pub, namespace=_NS, trackname="live",
                                   object_size=64, group_size=2,
                                   num_subgroups=2, rate=1000)
            await track.publish(announce_namespace=False,
                                publish_track=True)
            assert await _until(lambda: relay._tracks.get(_KEY) is not None)
            yield _client
    finally:
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


async def _subscriber(session):
    got = []
    session.on_object_received = lambda *a: got.append(a[3])
    await session.client_session_init()
    ok = await session.subscribe(_NS, "live", forward=1, wait_response=True)
    return ok.request_id, got


@pytest.mark.asyncio
@_TRANSPORTS
async def test_departed_subscribers_are_released(use_quic, monkeypatch):
    forget = relay._forget_session

    def _late(session):
        asyncio.get_running_loop().call_later(_REAP_LAG_S, forget, session)
    monkeypatch.setattr(relay, "_forget_session", _late)

    port = _BASE_PORT + (not use_quic)
    async with _relay_with_publisher(port, use_quic) as client:
        for i in range(_CHURN):
            async with client().connect() as s:
                rid, got = await _subscriber(s)
                assert await _until(lambda: len(got) >= 20), f"sub {i}"
                if i % 3 == 1:
                    s.unsubscribe(rid)
                    await asyncio.sleep(0.05)
                elif i % 3 == 2:
                    # Abrupt: no stream FINs ahead of the close.
                    s.close(SessionCloseCode.INTERNAL_ERROR, "gone")

        track = relay._tracks[_KEY]
        assert await _until(lambda: len(relay._sessions) == 1,
                            timeout=_REAP_LAG_S + 2)
        await asyncio.sleep(_REAP_LAG_S + 0.1)
        assert track.task is not None and not track.task.done()
        assert track.downstream == []
        assert track._streams == {} and track._sent_streams == {}
        assert track.queue.qsize() < 100

        async with client().connect() as s:
            _rid, got = await _subscriber(s)
            assert await _until(lambda: got, timeout=1.0), \
                "late subscriber received nothing"


class _Closed:
    """A downstream session that closed under the write: WT stream
    opening raises CancelledError then."""
    _profile = profile_for(18)

    async def open_uni_stream(self):
        raise asyncio.CancelledError()


class _Open:
    _profile = profile_for(18)

    def __init__(self):
        self.writes = []

    async def open_uni_stream(self):
        return 3

    def stream_write(self, _sid, data, end_stream=False):
        self.writes.append(data)

    async def stream_write_drain(self, _sid, data):
        self.writes.append(data)


@pytest.mark.asyncio
async def test_a_write_cancelled_by_a_closed_session_drops_one_subscriber():
    track = relay._RelayedTrack(_KEY)
    closed, live = _Closed(), _Open()
    track.add_downstream(closed, 0, request_id=0)
    track.add_downstream(live, 0, request_id=1)
    try:
        for oid in range(3):
            msg = SimpleNamespace(payload=b"x", group_id=0, object_id=oid,
                                  extensions=None, publisher_priority=128)
            track.on_object(msg, 1, 0, 0, 0)
        assert await _until(lambda: len(live.writes) == 4, timeout=1.0)
        assert track.task is not None and not track.task.done()
        assert [d.session for d in track.downstream] == [live]
        assert not any(k[0] == id(closed) for k in track._streams)
    finally:
        track.close()
