"""aiomoqt-relay MAX_CACHE_DURATION (d18 §12.3, d22 §10.3): an object is
not forwarded more than that long after it arrived. An expired cached
object is not cached: a FETCH for it goes upstream, a joiner is not
replayed it, and a live object waiting that long in a lane is dropped
with its subgroup reset."""
import asyncio
from collections import defaultdict

import pytest

from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import FilterType, ParamType, StreamResetCode

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_relay_send_lanes import (_Fake, _obj, _relay,
                                                 _settle, relay_tables)
from aiomoqt.tests.test_relay_delivery_timeout import _Sub, _slow_down

assert relay_tables  # fixture, used by name

_BASE_PORT = 29500
_NS = ("cachetime", "live")
_MAX_CACHE = int(ParamType.MAX_CACHE_DURATION)
_DT = int(StreamResetCode.DELIVERY_TIMEOUT)


@pytest.fixture
def lag(monkeypatch):
    def set_lag(seconds):
        monkeypatch.setattr(relay, "_lag_limit", seconds)
    return set_lag


def _track(max_ms):
    track = relay._RelayedTrack(("ns", "t"))
    track.properties = {_MAX_CACHE: max_ms}
    return track


async def _feed(track, gid, n):
    for oid in range(n):
        track.on_object(_obj(gid, oid), 0, 0, gid, 0)
    track.on_stream_end(gid, 0)
    await _settle(5)


# -- the cache against fake sessions --------------------------------------

async def test_expired_objects_leave_the_cache():
    track = _track(50)
    try:
        await _feed(track, 0, 4)
        await asyncio.sleep(0.1)
        await _feed(track, 1, 2)
        assert not track.cache_covers((0, 0)) and track.cache_covers((1, 0))
        assert [(o[0], o[2]) for o in track.cached_range((0, 0), None)] == [
            (1, 0), (1, 1)]
        assert track._cache_bytes == sum(
            relay.retained_object_bytes(c[3], c[4]) for c in track._cache)
    finally:
        track.close()


async def test_a_joiner_is_not_replayed_expired_objects(lag):
    lag(0)
    track = _track(50)
    try:
        await _feed(track, 0, 4)
        await asyncio.sleep(0.1)
        await _feed(track, 1, 2)
        late = _Fake()
        d = track.add_downstream(late, 1, 1, replay=True)
        await _settle()
        assert len(late.kinds("data")) == 2 and d.lane.expired == 0
        assert len(late.kinds("open")) == 1
    finally:
        track.close()


async def test_a_live_object_past_it_is_dropped_with_a_reset(lag):
    lag(0)
    track = _track(50)
    slow = _Fake(blocked=True)
    d = track.add_downstream(slow, 1, 1)
    try:
        for oid in range(4):
            track.on_object(_obj(0, oid), 0, 0, 0, 0)
        await _settle(5)
        await asyncio.sleep(0.1)
        slow.gate.set()
        await _settle()
        assert d.lane.deadline == 0.05 and d.lane.expired == 1
        (sid,) = [e[1] for e in slow.kinds("open")]
        assert ("reset", sid, _DT) in slow.log
    finally:
        track.close()


@pytest.mark.parametrize("lag_s,timeout_ms,want", [
    (5, 0, 0.05), (0.01, 0, 0.01), (0, 0, 0.05), (5, 20, 0.02),
    (5, 900, 0.05)])
async def test_it_caps_the_lane_deadline(lag, lag_s, timeout_ms, want):
    lag(lag_s)
    track = _track(50)
    d = track.add_downstream(_Fake(), 1, 1)
    d.timeout = timeout_ms
    try:
        await _feed(track, 0, 1)
        assert d.lane.deadline == want
    finally:
        track.close()


# -- over the wire ---------------------------------------------------------

async def _subscribe(sub, session, name):
    track = SubscribedTrack(session, _NS, name, on_object=sub.on_object)
    await track.subscribe(timeout=5)
    sub.watch(session, track.track_alias)

_MAX_MS = 800


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_expired_objects_are_fetched_upstream_not_replayed(
        use_quic, draft, relay_tables, lag, monkeypatch):
    monkeypatch.setattr(relay, "LANE_BYTES", 64 << 20)
    lag(0)
    port = _BASE_PORT + 4 * (draft == 22) + 2 * (not use_quic)
    names = ("video", "audio")
    viewers = {n: _Sub() for n in names}
    joiners = {n: _Sub() for n in names}
    lagging = {n: _Sub() for n in names}
    fetched = defaultdict(int)
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        for name in names:
            PublishedTrack(pub, namespace=_NS, trackname=name,
                           object_size=500, group_size=25, rate=50,
                           fetch_history_bytes=1 << 20,
                           track_properties={_MAX_CACHE: _MAX_MS}).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        viewer, slow, rx = await connect(), await connect(), await connect()
        tracks = {}
        for name in names:
            await _subscribe(viewers[name], viewer, name)
            tracks[name] = relay._tracks[(relay._ns_tuple(_NS),
                                          name.encode())]
        for name in names:
            before = {id(d.session) for d in tracks[name].downstream}
            await _subscribe(lagging[name], slow, name)
        (d_slow,) = [d for d in tracks["video"].downstream
                     if id(d.session) not in before]
        _slow_down(d_slow.session)
        await asyncio.sleep(1.5)

        rx.on_fetch_object = lambda obj, *_: fetched.__setitem__(
            "n", fetched["n"] + 1)
        for name in names:
            # Group 0 arrived 1 to 1.5 s ago, within the cache's group
            # bound but past 800 ms: it comes from upstream.
            fetched["n"] = 0
            ok = await asyncio.wait_for(rx.fetch(
                _NS, name, start_group=0, start_object=0, end_group=0,
                end_object=10, wait_response=True), 5)
            await rx.await_fetch_done(ok.request_id, timeout=5)
            assert fetched["n"] == 10, (name, fetched["n"])
            assert tracks[name]._fetch_bytes["upstream"] > 0
            assert tracks[name]._fetch_bytes["cache"] == 0
            # The group in progress is fresh: served from the cache.
            g = tracks[name].largest[0]
            fetched["n"] = 0
            ok = await asyncio.wait_for(rx.fetch(
                _NS, name, start_group=g, start_object=0, end_group=g,
                end_object=1, wait_response=True), 5)
            await rx.await_fetch_done(ok.request_id, timeout=5)
            assert fetched["n"] == 1, (name, fetched["n"])
            assert tracks[name]._fetch_bytes["cache"] > 0

        # A joiner from {0, 0} is replayed only what is still fresh.
        joiner = await connect()
        for name in names:
            await joiner.subscribe(
                namespace=_NS, track_name=name, wait_response=True,
                filter_type=FilterType.ABSOLUTE_START, start_group=0,
                start_object=0, on_object=joiners[name].on_object)
        await asyncio.sleep(0.5)
        expired = {n: d.lane.expired for n, d in
                   ((n, [d for d in tracks[n].downstream
                         if d.session is d_slow.session][0]) for n in names)}

    for name in names:
        assert viewers[name].count, name
        assert joiners[name].count, name
        assert min(joiners[name].objects) > 0, (name, joiners[name].objects)
        assert expired[name] > 0, expired
        assert StreamResetCode.DELIVERY_TIMEOUT in lagging[name].resets
