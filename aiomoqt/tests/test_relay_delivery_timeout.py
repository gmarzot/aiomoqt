"""aiomoqt-relay delivery timeouts (§8): a subscriber's lane drops objects
older than its OBJECT_DELIVERY_TIMEOUT, else live objects older than
--lag-limit, resetting the subgroup with DELIVERY_TIMEOUT and skipping
the rest of the group. --lag-limit never times a cache replay."""
import asyncio
import contextlib
from collections import defaultdict

import pytest

from aiomoqt.context import canonical_stream_reset
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import ParamType, StreamResetCode

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_relay_send_lanes import (_Fake, _obj, _relay,
                                                 _settle, relay_tables)

assert relay_tables  # fixture, used by name

_BASE_PORT = 29100
_NS = ("timeouts", "live")
_SLOW_S = 0.1
_RATE = 30
_GROUP = 10
_DT = int(StreamResetCode.DELIVERY_TIMEOUT)


@pytest.fixture
def lag(monkeypatch):
    """Sets --lag-limit for the test."""
    def set_lag(seconds):
        monkeypatch.setattr(relay, "_lag_limit", seconds)
    set_lag(relay.LAG_LIMIT_S)
    return set_lag


async def _feed(track, gid, n, start=0):
    for oid in range(start, start + n):
        track.on_object(_obj(gid, oid), 0, 0, gid, 0)
    await _settle(5)


# -- lane mechanics against fake sessions --------------------------------

async def test_a_lagging_lane_expires_live_objects_its_peer_gets_all(lag):
    lag(0.05)
    track = relay._RelayedTrack(("ns", "t"))
    fast, slow = _Fake(), _Fake(blocked=True)
    track.add_downstream(fast, 1, 1)
    d = track.add_downstream(slow, 2, 2)
    try:
        await _feed(track, 0, 4)
        await asyncio.sleep(0.1)
        slow.gate.set()
        await _settle()
        await _feed(track, 1, 2)
        await _settle()
        assert len(fast.kinds("data")) == 6
        # Object 0 was in flight; object 1 expired, its stream reset with
        # DELIVERY_TIMEOUT and the rest of group 0 skipped; group 1 arrives
        # on a new stream.
        assert d.lane.deadline == 0.05 and d.lane.expired == 1
        first, second = [e[1] for e in slow.kinds("open")]
        assert ("reset", first, _DT) in slow.log
        assert len(slow.kinds("data")) == 1 + 2
    finally:
        track.close()


async def test_lag_limit_zero_expires_nothing(lag):
    lag(0)
    track = relay._RelayedTrack(("ns", "t"))
    slow = _Fake(blocked=True)
    d = track.add_downstream(slow, 1, 1)
    try:
        await _feed(track, 0, 4)
        await asyncio.sleep(0.1)
        slow.gate.set()
        await _settle()
        assert d.lane.deadline is None and d.lane.expired == 0
        assert len(slow.kinds("data")) == 4
        assert not slow.kinds("reset")
    finally:
        track.close()


@pytest.mark.parametrize("max_cache_ms", [0, 10_000])
async def test_the_lag_limit_never_expires_a_replay(lag, max_cache_ms):
    lag(0.05)
    track = relay._RelayedTrack(("ns", "t"))
    if max_cache_ms:
        track.properties = {int(ParamType.MAX_CACHE_DURATION): max_cache_ms}
    try:
        await _feed(track, 0, 4)
        await asyncio.sleep(0.1)
        late = _Fake()
        d = track.add_downstream(late, 1, 1, replay=True)
        await _settle()
        assert d.lane.deadline == 0.05
        assert len(late.kinds("data")) == 4
        assert d.lane.expired == 0 and not late.kinds("reset")
    finally:
        track.close()


async def test_an_explicit_timeout_expires_a_replay(lag):
    lag(0)
    track = relay._RelayedTrack(("ns", "t"))
    try:
        await _feed(track, 0, 4)
        await asyncio.sleep(0.1)
        late = _Fake()
        d = track.add_downstream(late, 1, 1, replay=True)
        d.timeout = 50
        await _settle()
        # The first object expires and the rest of its group is skipped.
        assert d.lane.deadline == 0.05 and d.lane.expired == 1
        assert not late.kinds("data")
    finally:
        track.close()


@pytest.mark.parametrize("sub_ms,pub_ms,want", [
    (2000, 0, 2.0), (0, 3000, 3.0), (2000, 3000, 2.0), (4000, 1000, 1.0),
    (0, 0, relay.LAG_LIMIT_S)])
async def test_the_smaller_nonzero_timeout_applies(lag, sub_ms, pub_ms,
                                                   want):
    track = relay._RelayedTrack(("ns", "t"))
    if pub_ms:
        track.properties = {int(ParamType.DELIVERY_TIMEOUT): pub_ms}
    d = track.add_downstream(_Fake(), 1, 1)
    d.take_timeout({ParamType.DELIVERY_TIMEOUT: sub_ms} if sub_ms else {})
    try:
        await _feed(track, 0, 1)
        assert d.lane.deadline == want
    finally:
        track.close()


# -- over the wire ---------------------------------------------------------

class _Sub:
    def __init__(self):
        self.objects = defaultdict(set)
        self.resets = []

    def on_object(self, msg, size, ts, group_id, subgroup_id):
        if msg.payload:
            self.objects[group_id].add(msg.object_id)

    @property
    def count(self):
        return sum(len(v) for v in self.objects.values())

    def watch(self, session, alias):
        prof = session._profile

        def on_end(group_id, subgroup_id, clean=True, reset_code=0):
            if not clean:
                self.resets.append(canonical_stream_reset(reset_code, prof))
        session.register_stream_end_handler(alias, on_end)

    async def subscribe(self, session, name, timeout_ms=None):
        if timeout_ms is None:
            track = SubscribedTrack(session, _NS, name,
                                    on_object=self.on_object)
            await track.subscribe(timeout=5)
            self.watch(session, track.track_alias)
            return track.request_id
        ok = await session.subscribe(
            namespace=_NS, track_name=name, wait_response=True,
            parameters={ParamType.DELIVERY_TIMEOUT: timeout_ms},
            on_object=self.on_object)
        self.watch(session, ok.track_alias)
        return ok.request_id


def _slow_down(session):
    real = session.stream_write_drain

    async def slow(stream_id, data):
        await asyncio.sleep(_SLOW_S)
        await real(stream_id, data)
    session.stream_write_drain = slow


def _publish(pub):
    for name in ("video", "audio"):
        PublishedTrack(pub, namespace=_NS, trackname=name, object_size=2000,
                       group_size=_GROUP, rate=_RATE).attach()


def _new_downstream(track, before):
    (d,) = [d for d in track.downstream if id(d.session) not in before]
    return d


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_lagging_viewer_expires_while_a_healthy_one_gets_all(
        use_quic, draft, relay_tables, lag, monkeypatch):
    monkeypatch.setattr(relay, "LANE_BYTES", 64 << 20)
    lag(0.5)
    port = _BASE_PORT + 4 * (draft == 22) + 2 * (not use_quic)
    healthy = {n: _Sub() for n in ("video", "audio")}
    lagging = {n: _Sub() for n in ("video", "audio")}
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        _publish(pub)
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        s_ok, s_lag = await connect(), await connect()
        tracks, lanes = {}, {}
        for name in ("video", "audio"):
            await healthy[name].subscribe(s_ok, name)
            tracks[name] = relay._tracks[(relay._ns_tuple(_NS),
                                          name.encode())]
        before = {n: set(map(id, (d.session for d in t.downstream)))
                  for n, t in tracks.items()}
        for name in ("video", "audio"):
            await lagging[name].subscribe(s_lag, name)
            lanes[name] = _new_downstream(tracks[name], before[name])
        _slow_down(lanes["video"].session)
        await asyncio.sleep(2.5)
        sent = {n: t._objects_in for n, t in tracks.items()}
        expired = {n: d.lane.expired for n, d in lanes.items()}
        sheds = {n: d.lane.sheds for n, d in lanes.items()}
        await asyncio.sleep(0.3)

    for name in ("video", "audio"):
        assert healthy[name].count >= sent[name] - _RATE, (
            name, healthy[name].count, sent[name])
        assert lagging[name].count, name
        # Dropped by the deadline, not the byte bound.
        assert expired[name] > 0 and sheds[name] == 0, (expired, sheds)
        assert StreamResetCode.DELIVERY_TIMEOUT in lagging[name].resets, (
            name, lagging[name].resets)


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_subscribe_and_update_set_the_timeout(
        use_quic, draft, relay_tables, lag, monkeypatch):
    monkeypatch.setattr(relay, "LANE_BYTES", 64 << 20)
    lag(0.5)
    port = _BASE_PORT + 50 + 4 * (draft == 22) + 2 * (not use_quic)
    subs = {n: _Sub() for n in ("video", "audio")}
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        _publish(pub)
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        viewer = await connect()
        # The subscriber's 60 s overrides the 0.5 s default on video.
        rid = await subs["video"].subscribe(viewer, "video", timeout_ms=60_000)
        await subs["audio"].subscribe(viewer, "audio")
        ds = {n: relay._tracks[(relay._ns_tuple(_NS), n.encode())]
              .downstream[0] for n in ("video", "audio")}
        _slow_down(ds["video"].session)
        await asyncio.sleep(1.5)
        video, audio = ds["video"].lane, ds["audio"].lane
        assert (video.deadline, audio.deadline) == (60.0, 0.5)
        # The session is slow on both tracks; only audio has 0.5 s.
        assert video.expired == 0 and video.sheds == 0
        assert audio.expired > 0, audio.expired
        reply = viewer.request_update(
            rid, parameters={ParamType.DELIVERY_TIMEOUT: 200},
            wait_response=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reply, 2)
        assert ds["video"].timeout == 200 and video.deadline == 0.2
        await asyncio.sleep(1.5)
        assert video.expired > 0 and video.sheds == 0, video.expired

    assert subs["video"].count and subs["audio"].count
    assert StreamResetCode.DELIVERY_TIMEOUT in subs["video"].resets
