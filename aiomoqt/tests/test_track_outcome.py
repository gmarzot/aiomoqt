"""How a subscribed track ends, and the load tools retiring viewers on it."""
import asyncio
import queue
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port
from aiomoqt.tests.test_multi_track_publish import (
    _Receiver, _loopback, _track, _until)
from aiomoqt.tools import load_sim
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import (PublishedTrack, SubscribedTrack, TrackEnd,
                           TrackEndKind)
from aiomoqt.types import SessionCloseCode, SubscribeDoneCode
from aiomoqt.utils import workers
from aiomoqt.utils.stats import TrackStats

_NAMES = ("video", "audio")
_TRANSPORTS = pytest.mark.parametrize("raw", (True, False), ids=("quic", "wt"))


def test_only_a_clean_publish_done_is_completed():
    assert TrackEnd(TrackEndKind.DONE, SubscribeDoneCode.TRACK_ENDED).completed
    assert TrackEnd(TrackEndKind.DONE,
                    SubscribeDoneCode.SUBSCRIPTION_ENDED).completed
    assert not TrackEnd(TrackEndKind.DONE,
                        SubscribeDoneCode.INTERNAL_ERROR).completed
    assert not TrackEnd(TrackEndKind.SESSION_CLOSED, 0, "").completed
    assert not TrackEnd(TrackEndKind.CANCELLED).completed
    assert not TrackEnd(TrackEndKind.MALFORMED, reason="bad").completed


async def _two_subs(rx, ns):
    counts = {n: 0 for n in _NAMES}
    subs = []
    for name in _NAMES:
        def got(obj, *_args, n=name):
            if obj.payload:
                counts[n] += 1
        sub = SubscribedTrack(rx.session, ns, name, on_object=got)
        await sub.subscribe(timeout=5)
        subs.append(sub)
    assert await _until(lambda: min(counts.values()) >= 3)
    return subs


@requires_certs
@pytest.mark.parametrize("code", (SessionCloseCode.INTERNAL_ERROR,
                                  SessionCloseCode.NO_ERROR),
                         ids=("internal-error", "no-error"))
@pytest.mark.parametrize("draft", (18, 22))
@_TRANSPORTS
async def test_session_close_without_publish_done_is_not_completed(
        draft, raw, code):
    rx = _Receiver()
    ns = "end/close"
    async with _loopback(free_udp_port(), draft, rx, use_quic=raw) as pub:
        for name in _NAMES:
            _track(pub, ns, name, 200).attach()
        await pub.publish_namespace(namespace=ns, wait_response=True)
        subs = await _two_subs(rx, ns)
        pub.close(code, "producer failure")
        ends = await asyncio.wait_for(
            asyncio.gather(*(s.wait_closed() for s in subs)), 5)
    for sub, end in zip(subs, ends):
        assert sub.publish_done is None
        assert end is sub.end
        assert end.kind is TrackEndKind.SESSION_CLOSED
        assert end.code == code and end.reason == "producer failure"
        assert sub.completed is False


@requires_certs
@_TRANSPORTS
async def test_unsubscribe_ends_cancelled(raw):
    rx = _Receiver()
    ns = "end/cancel"
    async with _loopback(free_udp_port(), 22, rx, use_quic=raw) as pub:
        for name in _NAMES:
            _track(pub, ns, name, 200).attach()
        await pub.publish_namespace(namespace=ns, wait_response=True)
        subs = await _two_subs(rx, ns)
        subs[0].unsubscribe()
        end = await asyncio.wait_for(subs[0].wait_closed(), 5)
        assert end.kind is TrackEndKind.CANCELLED and not subs[0].completed
        assert subs[1].end is None


class _Finite(PublishedTrack):
    async def produce(self, out):
        for group in range(3):
            await out.write(group, 0, b"frame", group_start=True,
                            end_of_group=True)
            await asyncio.sleep(0.05)


class _Relay:
    """aiomoqt-relay on localhost with a two-track publisher."""

    def __init__(self, raw, track_cls=None):
        self.raw = raw
        self.port = free_udp_port()
        self.ns = "end/relay"
        self.track_cls = track_cls

    async def __aenter__(self):
        relay._announced.clear()
        relay._tracks.clear()
        self.server = relay._build_server("localhost", self.port, CERT, KEY,
                                          use_quic=self.raw, draft=22)
        self.handle = await self.server.serve()
        await self.publish()
        return self

    async def publish(self):
        self._pub_cm = MOQTClient(
            "localhost", self.port, path="/", use_quic=self.raw,
            verify_tls=False, supported_drafts=22).connect()
        session = await self._pub_cm.__aenter__()
        await session.client_session_init()
        for name in _NAMES:
            track = (self.track_cls(session, self.ns, name)
                     if self.track_cls else
                     PublishedTrack(session, self.ns, name, object_size=500,
                                    rate=50, group_size=10))
            track.attach()
        await session.publish_namespace(namespace=self.ns, wait_response=True)
        await _until(lambda: len(self.server._live) >= 1)
        self.pub_side = set(self.server._live)

    def close_viewers(self, code=SessionCloseCode.NO_ERROR):
        """Close every relay session but the publisher's."""
        for s in list(self.server._live - self.pub_side):
            s.close(code, "relay dropped viewer")

    def endpoint(self):
        return SimpleNamespace(host="localhost", port=self.port, path="/",
                               use_quic=self.raw)

    async def __aexit__(self, *exc):
        try:
            await self._pub_cm.__aexit__(None, None, None)
        except Exception:
            pass
        self.handle.close()
        for track in list(relay._tracks.values()):
            track.close()
        relay._tracks.clear()
        relay._announced.clear()


def _evs(events):
    return [m["ev"] for m in list(events.queue)]


async def _load_slot(rig, events):
    slot = dict(stop=asyncio.Event(), live=False, done=False,
                stats={n: TrackStats() for n in _NAMES})
    cfg = dict(namespace=rig.ns, tracknames=list(_NAMES), insecure=True,
               draft=22)
    task = asyncio.create_task(load_sim._slot_task(
        cfg, rig.endpoint(), slot, events, 0, 0, 0))
    return slot, task


@requires_certs
@_TRANSPORTS
async def test_load_sim_slot_ends_on_publish_done(raw):
    events = queue.Queue()
    async with _Relay(raw, _Finite) as rig:
        slot, task = await _load_slot(rig, events)
        try:
            await asyncio.wait_for(task, 8)
        finally:
            slot["stop"].set()
        assert _evs(events) == ["joined", "ended"]
        assert all(s.total_objects > 0 for s in slot["stats"].values())
        assert slot["done"] and not slot["live"]


@requires_certs
@_TRANSPORTS
async def test_load_sim_counts_a_relay_closed_viewer_as_died(raw):
    events = queue.Queue()
    async with _Relay(raw) as rig:
        slot, task = await _load_slot(rig, events)
        try:
            assert await _until(lambda: all(
                s.total_objects > 5 for s in slot["stats"].values()), 8)
            rig.close_viewers()
            await asyncio.wait_for(task, 5)
        finally:
            slot["stop"].set()
        assert _evs(events) == ["joined", "died"]
        assert not slot["live"]


@requires_certs
@_TRANSPORTS
async def test_supervisor_redials_a_relay_closed_viewer(raw, monkeypatch):
    monkeypatch.setattr(workers, "SELF_HEAL_BACKOFF_S", 0.1)
    async with _Relay(raw) as rig:
        config = dict(namespace=rig.ns, trackname=_NAMES[0],
                      side_tracks=[(_NAMES[1],)], insecure=True, draft=22)
        state = workers.new_slot_state(workers.new_join_tally())
        stats = workers.new_viewer_stats(config)
        stop = asyncio.Event()
        task = asyncio.create_task(workers._slot_supervisor(
            config, rig.endpoint(), stop, stats, state, 0))
        try:
            assert await _until(lambda: state["active"] and all(
                t.total_objects > 3 for t in stats.tracks.values()), 8)
            rig.close_viewers()
            assert await _until(lambda: not state["active"], 5)
            before = {n: t.total_objects for n, t in stats.tracks.items()}
            assert await _until(lambda: state["active"] and all(
                t.total_objects > before[n] + 3
                for n, t in stats.tracks.items()), 10)
            assert state["tally"]["joins"] == 2
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
