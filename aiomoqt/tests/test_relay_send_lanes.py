"""aiomoqt-relay send lanes: each subscriber's objects queue on its own
lane, so a slow subscriber holds up only itself and sheds whole groups
past the lane bound; its truncated subgroups are reset, never FINed."""
import asyncio
import contextlib
import logging
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import canonical_stream_reset, profile_for
from aiomoqt.delivery import LANE_MIN_BYTES, SendLane
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import StreamResetCode, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 25100
_NS = ("lanes", "live")
_OBJ = 40_000          # keyframe-sized
_GROUP = 10
_RATE = 30             # objects/s
_BOUND = 1024 * 1024   # LANE_BYTES for the loopback runs
_SLOW_S = 0.2          # per write to the slow subscriber


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._catalogs):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture
def relay_tables():
    _clear_relay()
    yield
    _clear_relay()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft):
    """A relay; yields a coroutine that connects one more session."""
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            yield connect
    finally:
        handle.close()


class _Sub:
    """Objects per (group, subgroup) and how each subgroup stream ended."""

    def __init__(self):
        self.objects = defaultdict(set)
        self.ends = {}           # (group, subgroup) -> (clean, reset code)

    def on_object(self, msg, size, ts, group_id, subgroup_id):
        if msg.payload:
            self.objects[(group_id, subgroup_id or 0)].add(msg.object_id)

    @property
    def count(self):
        return sum(len(v) for v in self.objects.values())

    async def subscribe(self, session, name):
        track = SubscribedTrack(session, _NS, name, on_object=self.on_object)
        await track.subscribe(timeout=5)
        prof = session._profile

        def on_end(group_id, subgroup_id, clean=True, reset_code=0):
            code = None if clean else canonical_stream_reset(reset_code, prof)
            self.ends[(group_id, subgroup_id or 0)] = (clean, code)
        session.register_stream_end_handler(track.track_alias, on_end)
        return track


def _slow_down(session):
    """Every awaited write to `session` takes _SLOW_S: a subscriber whose
    streams drain slowly."""
    real = session.stream_write_drain

    async def slow(stream_id, data):
        await asyncio.sleep(_SLOW_S)
        await real(stream_id, data)
    session.stream_write_drain = slow


async def _until(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_slow_subscriber_sheds_groups_without_holding_up_others(
        use_quic, draft, relay_tables, monkeypatch, caplog):
    monkeypatch.setattr(relay, "LANE_BYTES", _BOUND)
    caplog.set_level(logging.WARNING, logger="aiomoqt.relay")
    port = _BASE_PORT + 4 * (draft == 22) + 2 * (not use_quic)
    fast, slow, late = _Sub(), _Sub(), _Sub()
    fast_audio, slow_audio = _Sub(), _Sub()
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        video = PublishedTrack(pub, namespace=_NS, trackname="video",
                               object_size=_OBJ, group_size=_GROUP,
                               rate=_RATE)
        video.attach()
        PublishedTrack(pub, namespace=_NS, trackname="audio",
                       object_size=200, group_size=50, rate=50).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)

        s_fast, s_slow = await connect(), await connect()
        await fast.subscribe(s_fast, "video")
        await fast_audio.subscribe(s_fast, "audio")
        key = (relay._ns_tuple(_NS), b"video")
        track = relay._tracks[key]
        before = set(map(id, (d.session for d in track.downstream)))
        await slow.subscribe(s_slow, "video")
        (d_slow,) = [d for d in track.downstream if id(d.session) not in before]
        _slow_down(d_slow.session)
        await slow_audio.subscribe(s_slow, "audio")

        peak = {"lane": 0, "queue": 0}

        async def sample():
            while True:
                lane = d_slow.lane
                if lane is not None:
                    peak["lane"] = max(peak["lane"], lane.bytes)
                peak["queue"] = max(peak["queue"], track.queue.qsize())
                await asyncio.sleep(0.005)
        sampler = asyncio.create_task(sample())
        try:
            await asyncio.sleep(2.0)
            s_late = await connect()
            await late.subscribe(s_late, "video")
            await asyncio.sleep(1.5)
        finally:
            sampler.cancel()
        sent = track._objects_in
        lane = d_slow.lane
        shed, sheds = lane.shed, lane.sheds

    assert fast.count and slow.count and late.count, (
        fast.count, slow.count, late.count)
    # The fast subscriber keeps pace: at most a second behind.
    assert fast.count >= sent - _RATE, (fast.count, sent)
    assert late.count >= _GROUP, late.count
    assert fast_audio.count and slow_audio.count
    # The slow one shed whole groups; the first episode logs a WARNING.
    assert shed > 0 and sheds >= 1, (shed, sheds)
    warned = [r for r in caplog.records if "send lane" in r.getMessage()]
    assert len(warned) == 1, [r.getMessage() for r in warned]
    resets = {k: code for k, (clean, code) in slow.ends.items() if not clean}
    assert resets, slow.ends
    assert set(resets.values()) == {StreamResetCode.CANCELLED}, resets
    # A FINed subgroup is complete; a short one was reset.
    for k, (clean, _code) in slow.ends.items():
        if clean:
            assert slow.objects[k] == set(range(_GROUP)), (k, slow.objects[k])
    # Bounded: the lane holds at most its bound, the track queue stays short.
    assert peak["lane"] <= _BOUND + _OBJ, peak
    assert peak["queue"] < 2 * _RATE, peak


# -- lane mechanics against fake sessions --------------------------------

class _Fake:
    """A downstream session whose awaited writes block until released."""

    def __init__(self, blocked=False):
        self._profile = profile_for(18)
        self._next = 3
        self.log = []   # ("open"|"hdr"|"data"|"fin"|"reset"|"done", ...)
        self._unwritten = set()  # opened, header not yet written
        self.gate = asyncio.Event()
        if not blocked:
            self.gate.set()

    def set_stream_priority(self, _sid, _band):
        return True

    async def open_uni_stream(self, initial=b"", priority=None):
        sid = self._next
        self._next += 4
        self.log.append(("open", sid))
        self._unwritten.add(sid)
        if initial:
            # The header write carries the stream's first object.
            try:
                await self.stream_write_drain(sid, initial)
            except BaseException:
                self.stream_reset(sid, StreamResetCode.CANCELLED)
                raise
            self.log.append(("data", sid))
        return sid

    def stream_write(self, sid, data, end_stream=False):
        if end_stream:
            self.log.append(("fin", sid))

    async def stream_write_drain(self, sid, data):
        await self.gate.wait()
        if sid in self._unwritten:
            self._unwritten.discard(sid)
            self.log.append(("hdr", sid))
        else:
            self.log.append(("data", sid))

    def stream_reset(self, sid, code):
        self.log.append(("reset", sid, int(code)))

    def subscribe_done(self, request_id, status_code=0, stream_count=0,
                       reason=""):
        self.log.append(("done", int(status_code)))

    def kinds(self, kind):
        return [e for e in self.log if e[0] == kind]


def _obj(gid, oid, size=1000):
    return SimpleNamespace(group_id=gid, object_id=oid, payload=b"x" * size,
                           extensions=None, publisher_priority=128,
                           status=None, stream_flags=None)


async def _settle(n=50):
    for _ in range(n):
        await asyncio.sleep(0)


async def test_a_blocked_lane_holds_up_neither_the_track_nor_its_peers(
        monkeypatch):
    monkeypatch.setattr(relay, "LANE_BYTES", 10_000)
    track = relay._RelayedTrack(("ns", "t"))
    fast, slow = _Fake(), _Fake(blocked=True)
    track.add_downstream(fast, 1, 1)
    d_slow = track.add_downstream(slow, 2, 2)
    try:
        for gid in range(4):
            for oid in range(4):
                track.on_object(_obj(gid, oid), 0, 0, gid, 0)
                await _settle(5)
            track.on_stream_end(gid, 0)
        await _settle()
        assert len(fast.kinds("data")) == 16 and len(fast.kinds("fin")) == 4
        assert track.queue.empty()
        lane = d_slow.lane
        # 16 KB queued against a 10 KB bound: the older groups were cut.
        assert lane.shed > 0 and lane.sheds == 1
        assert lane.bytes <= relay.LANE_BYTES
        slow.gate.set()
        await _settle()
        # The stream open when the lane shed was reset, not FINed; the
        # subscriber resumed at a later group's first object.
        (first,) = [e[1] for e in slow.kinds("open")[:1]]
        assert ("reset", first, int(StreamResetCode.CANCELLED)) in slow.log
        assert ("fin", first) not in slow.log
        assert len(slow.kinds("open")) >= 2
        assert lane.idle and not lane.shedding
    finally:
        track.close()


async def test_publish_done_follows_each_lane(monkeypatch):
    track = relay._RelayedTrack(("ns", "t"))
    fast, slow = _Fake(), _Fake(blocked=True)
    track.add_downstream(fast, 1, 1)
    track.add_downstream(slow, 2, 2)
    relay._tracks[track.key] = track
    try:
        for oid in range(3):
            track.on_object(_obj(0, oid), 0, 0, 0, 0)
        track.on_stream_end(0, 0)
        track._enqueue_terminal(SubscribeDoneCode.TRACK_ENDED, "done",
                                track.key)
        await _settle()
        assert fast.log[-2:] == [("fin", 3), ("done", 2)]
        assert not slow.kinds("done")
        assert track.task is None and track.key not in relay._tracks
        slow.gate.set()
        await _settle()
        assert len(slow.kinds("data")) == 3
        assert slow.log[-2:] == [("fin", 3), ("done", 2)]
        assert not relay._lanes
    finally:
        relay._tracks.pop(track.key, None)
        track.close()


async def test_a_late_joiner_replays_on_its_own_lane(monkeypatch):
    # The replay goes behind nothing queued for a blocked peer.
    track = relay._RelayedTrack(("ns", "t"))
    slow = _Fake(blocked=True)
    track.add_downstream(slow, 1, 1)
    try:
        for oid in range(3):
            track.on_object(_obj(0, oid), 0, 0, 0, 0)
        await _settle()
        late = _Fake()
        track.add_downstream(late, 2, 2, replay=True)
        track.on_object(_obj(0, 3), 0, 0, 0, 0)
        await _settle()
        assert len(late.kinds("data")) == 4
        assert not slow.kinds("data")
    finally:
        track.close()


async def test_a_closed_session_stops_its_draining_lane(monkeypatch):
    track = relay._RelayedTrack(("ns", "t"))
    slow = _Fake(blocked=True)
    d = track.add_downstream(slow, 1, 1)
    track.on_object(_obj(0, 0), 0, 0, 0, 0)
    track.on_object(_obj(0, 1), 0, 0, 0, 0)
    await _settle()
    track.finish()
    track.close()
    lane = d.lane
    assert lane in relay._lanes and not lane.stopped
    relay._forget_session(slow)
    await _settle()
    assert lane.stopped and lane.task.done() and lane not in relay._lanes
    assert not slow.kinds("done")


# -- the lane's time bound -------------------------------------------------
class _Writer:
    """Holds `tx` bytes in the transport, and every send waits on `gate`."""

    def __init__(self):
        self.session = SimpleNamespace()
        self.tx = 0
        self.resets = []
        self.gate = asyncio.Event()

    async def send(self, item):
        await self.gate.wait()
        return True

    def tx_queued(self, floor=None):
        return self.tx

    def reset_below(self, floor):
        self.resets.append(floor)

    def reset_group(self, code=StreamResetCode.CANCELLED):
        self.resets.append(code)

    def end_group(self):
        pass

    def close_streams(self):
        pass

    def expire(self, gid):
        return True

    def final(self, *args):
        pass


def _item(gid, cost=1000):
    return (gid, cost, True, None)


@pytest.mark.parametrize("deadline,rate,expected", [
    (None, 100_000, 4_000_000),  # no deadline: bytes alone
    (2.0, 0.0, 4_000_000),       # rate unknown
    (2.0, 125_000, 250_000),     # 1 Mbps for 2 s
    (2.0, 1_000, LANE_MIN_BYTES),   # a slow track keeps the floor
    (60.0, 125_000, 4_000_000),  # never past max_bytes
])
async def test_the_bound_is_the_deadline_at_the_input_rate(deadline, rate, expected):
    lane = SendLane(_Writer(), max_bytes=4_000_000)
    lane.deadline, lane.rate = deadline, rate
    assert lane.bound == expected
    lane.stop()
    await _settle()


async def test_transport_backlog_counts_against_the_time_bound():
    writer = _Writer()
    lane = SendLane(writer, max_bytes=4_000_000)
    lane.deadline, lane.rate = 1.0, 100_000
    writer.tx = 90_000
    lane.offer(_item(1, 5_000))   # read tx_bytes; under the bound with this
    assert lane.sheds == 0
    lane.offer(_item(2, 8_000))   # a new group reads it again: 103 KB held
    assert lane.sheds == 1 and lane.shedding
    assert lane.floor is not None and lane.floor >= 2
    lane.stop()
    await _settle()


async def test_shed_bytes_count_what_the_transport_held():
    writer = _Writer()
    lane = SendLane(writer, max_bytes=4_000_000)
    lane.deadline, lane.rate = 1.0, 100_000
    writer.tx_queued = lambda floor=None: 95_000 if floor is None else 0
    writer.gate.set()
    lane.offer(_item(1, 1_000))
    lane._probe_gid = None
    lane.tx_bytes = 95_000
    lane.items.clear()
    lane.bytes = 0
    lane.offer(_item(2, 10_000))
    assert lane.sheds == 1 and lane.shed == 0
    assert lane.shed_bytes == 95_000, lane.shed_bytes
    lane.stop()
    await _settle()


async def test_no_rate_leaves_the_byte_bound_alone():
    writer = _Writer()
    lane = SendLane(writer, max_bytes=4_000_000)
    lane.deadline = 1.0
    writer.tx = 2_000_000
    lane.offer(_item(1))
    lane.offer(_item(2))
    assert lane.sheds == 0
    lane.stop()
    await _settle()


def test_the_input_rate_decays_from_a_peak_and_reaches_the_lanes():
    lane = SimpleNamespace(rate=0.0)
    track = SimpleNamespace(_rate_mark=(0.0, 0), _bytes_in=0, _in_rate=0.0,
                            _lanes=[lane])
    update = relay._RelayedTrack.update_in_rate
    track._bytes_in = 1000
    update(track, 1.0)
    assert track._in_rate == 1000 and lane.rate == 1000
    update(track, 2.0)            # nothing arrived: held near the peak
    assert lane.rate == pytest.approx(1000 * relay.IN_RATE_DECAY)
    track._bytes_in = 3000
    update(track, 3.0)
    assert lane.rate == 2000


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_viewer_past_the_lag_limit_sheds_under_the_byte_bound(
        use_quic, draft, relay_tables, monkeypatch):
    monkeypatch.setattr(relay, "_lag_limit", 0.5)
    port = _BASE_PORT + 8 + 4 * (draft == 22) + 2 * (not use_quic)
    fast, slow, slow_audio = _Sub(), _Sub(), _Sub()
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        PublishedTrack(pub, namespace=_NS, trackname="video", object_size=_OBJ,
                       group_size=_GROUP, rate=_RATE).attach()
        PublishedTrack(pub, namespace=_NS, trackname="audio", object_size=200,
                       group_size=50, rate=50).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)

        s_fast, s_slow = await connect(), await connect()
        await fast.subscribe(s_fast, "video")
        track = relay._tracks[(relay._ns_tuple(_NS), b"video")]
        before = set(map(id, (d.session for d in track.downstream)))
        await slow.subscribe(s_slow, "video")
        (d_slow,) = [d for d in track.downstream if id(d.session) not in before]
        _slow_down(d_slow.session)
        await slow_audio.subscribe(s_slow, "audio")
        audio = relay._tracks[(relay._ns_tuple(_NS), b"audio")]
        (d_audio,) = list(audio.downstream)

        async def tick():
            while True:
                await asyncio.sleep(0.2)
                relay._check_lanes()
        ticker = asyncio.create_task(tick())
        try:
            await asyncio.sleep(3.0)
        finally:
            ticker.cancel()
        lane = d_slow.lane
        sent, sheds, bound = track._objects_in, lane.sheds, lane.bound
        shed_total = track.shed_bytes()
        audio_sheds = d_audio.lane.sheds

    assert relay.LANE_BYTES > bound > 0, bound
    assert sheds >= 1 and shed_total > 0, (sheds, shed_total)
    assert audio_sheds == 0
    assert slow_audio.count
    assert fast.count >= sent - _RATE, (fast.count, sent)
    assert slow.count
    resets = {k: code for k, (clean, code) in slow.ends.items() if not clean}
    assert resets and all(c is not None for c in resets.values()), slow.ends
    for k, (clean, _code) in slow.ends.items():
        if clean:
            assert slow.objects[k] == set(range(_GROUP)), (k, slow.objects[k])


def test_the_deployed_relay_config_parses():
    path = Path(relay.__file__).parents[2] / "deploy/aiomoqt-relay/relay.toml"
    if not path.exists():
        pytest.skip("source tree only")
    args = relay.parse_args(["--config", str(path)])
    assert args.lag_limit == relay.LAG_LIMIT_S
