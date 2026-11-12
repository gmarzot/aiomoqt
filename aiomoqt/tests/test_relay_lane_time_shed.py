"""aiomoqt-relay send lanes shed by time behind: a subscriber sheds whole
groups once the oldest live object it has not been sent, queued or still
in the transport, is past its deadline; never for an object's size. The
lane check runs on a timer (relay._time_lanes), never per object."""
import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import canonical_stream_reset, profile_for
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_DEADLINE = 5.0
_FPS = 30
# Seconds between a lane's time checks at _DEADLINE.
_STEP = min(max(_DEADLINE / relay.LANE_TIME_CHECKS, relay.LANE_TIME_MIN_S),
            relay.LANE_TIME_MAX_S)
_BASE_PORT = 25200


class _Viewer:
    """A downstream session over a fake transport. Bytes written to a
    stream stay in its `ring` until drained; with `take` False every
    write waits on `gate`."""

    def __init__(self, take=True):
        self._profile = profile_for(18)
        self._next = 3
        self.take = take
        self.gate = asyncio.Event()
        self.ring = {}                   # sid -> bytes the transport holds
        self.writes = defaultdict(int)   # sid -> non-empty writes
        self.fins, self.resets = [], {}
        self._quic = SimpleNamespace(
            tx_data_ring_used=lambda sid: self.ring.get(sid, 0))

    def set_stream_priority(self, _sid, _band):
        return True

    def _put(self, sid, data, end_stream):
        if data:
            self.ring[sid] = self.ring.get(sid, 0) + len(data)
            self.writes[sid] += 1
        if end_stream:
            self.fins.append(sid)

    async def open_uni_stream(self, initial=b"", priority=None,
                              end_stream=False):
        if not self.take:
            await self.gate.wait()
        sid = self._next
        self._next += 4
        self._put(sid, initial, end_stream)
        return sid

    def stream_write_nowait(self, sid, data, end_stream=False, **_kw):
        if not self.take:
            return False
        self._put(sid, data, end_stream)
        return True

    async def stream_write_drain(self, sid, data, end_stream=False):
        if not self.take:
            await self.gate.wait()
        self._put(sid, data, end_stream)

    def stream_write(self, sid, data, end_stream=False):
        self._put(sid, data, end_stream)

    def stream_reset(self, sid, code):
        self.ring.pop(sid, None)
        self.resets[sid] = int(code)

    def subscribe_done(self, *_a, **_kw):
        pass

    @property
    def held(self):
        return sum(self.ring.values())

    def drain(self, n=None):
        """The peer reads `n` bytes, oldest stream first; all with None."""
        for sid in sorted(self.ring):
            if n is not None and n < self.ring[sid]:
                self.ring[sid] -= n
                return
            if n is not None:
                n -= self.ring[sid]
            del self.ring[sid]


def _obj(gid, oid, size, born):
    return relay._item(gid, True, 0, oid, b"x" * size, None, 128, None,
                       None, {}, None, born)


def _end(gid):
    return relay._item(gid, True, 0, None, None, None, 128, "END", None)


async def _settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


def _tick(now):
    """The relay's lane timer at `now`, the test's clock."""
    relay._time_lanes(now)


@pytest.fixture
def lanes(monkeypatch):
    """Relay tracks with one lane each to a viewer, isolated from the
    relay's tables; `_DEADLINE` is the lag limit."""
    monkeypatch.setattr(relay, "_lanes", set())
    monkeypatch.setattr(relay, "_lanes_over_budget", False)
    monkeypatch.setattr(relay, "_lag_limit", _DEADLINE)
    tracks = []

    def make(viewer, name=b"video"):
        track = relay._RelayedTrack(((b"ns",), name))
        tracks.append(track)
        d = track.add_downstream(viewer, len(tracks), len(tracks))
        lane = track._lane(d)
        assert lane.deadline == _DEADLINE
        return track, lane
    yield make
    for track in tracks:
        track.close()


async def test_a_quiet_tail_then_a_large_keyframe_sheds_nothing(lanes):
    viewer = _Viewer()
    track, lane = lanes(viewer)
    t = time.monotonic()
    sent = 0
    # 90 s of a quiet scene: 2 KB keyframes, 300-byte frames.
    for gid in range(45):
        for oid in range(60):
            _tick(t)
            lane.offer(_obj(gid, oid, 2000 if oid == 0 else 300, t))
            t += 1 / _FPS
            sent += 1
            if oid == 0:
                await _settle(5)      # the lane task opens the stream
            viewer.drain()
        lane.offer(_end(gid))
        await _settle(2)
    # The next scene: a 256 KB keyframe, then 30 KB frames; the viewer
    # takes a third of a second to read them.
    for gid in range(45, 48):
        for oid in range(60):
            _tick(t)
            lane.offer(_obj(gid, oid, 256_000 if oid == 0 else 30_000, t))
            t += 1 / _FPS
            sent += 1
            if oid == 0:
                await _settle(5)
            if oid % 10 == 9:
                viewer.drain()
        lane.offer(_end(gid))
        await _settle(2)
    viewer.drain()
    await _settle()
    assert sent and track._objects_out == sent, (track._objects_out, sent)
    assert lane.sheds == 0 and lane.shed == 0 and not viewer.resets
    assert len(viewer.fins) == 48


async def test_a_stalled_reader_sheds_whole_groups_and_resumes_newer(lanes):
    viewer = _Viewer(take=False)
    track, lane = lanes(viewer)
    t0 = t = time.monotonic()
    shed_at = None
    for gid in range(8):
        for oid in range(_FPS):
            _tick(t)
            lane.offer(_obj(gid, oid, 10_000, t))
            if lane.sheds and shed_at is None:
                shed_at = t - t0
            t += 1 / _FPS
            if oid == 0:
                await _settle(5)
        lane.offer(_end(gid))
    assert lane.sheds >= 1 and lane.floor is not None, lane.floor
    # Shed past the deadline, within one check.
    assert _DEADLINE < shed_at <= _DEADLINE + _STEP + 1 / _FPS, shed_at
    floor = lane.floor
    viewer.take = True
    viewer.gate.set()
    for gid in range(8, 10):
        for oid in range(_FPS):
            _tick(t)
            lane.offer(_obj(gid, oid, 10_000, t))
            t += 1 / _FPS
            if oid == 0:
                await _settle(5)
        lane.offer(_end(gid))
        await _settle(5)
    await _settle()
    tx = track._tx_streams[id(viewer)]
    gids = {sid: k[0] for k, sid in tx.items()}
    # Shed past the deadline: reset as the expiry path would (§8).
    assert viewer.resets, viewer.resets
    assert set(viewer.resets.values()) == {
        int(StreamResetCode.DELIVERY_TIMEOUT)}
    finned = [sid for sid in viewer.fins if sid not in viewer.resets]
    assert finned, viewer.fins
    for sid in finned:
        # A FINed stream carries its whole group.
        assert viewer.writes[sid] == _FPS, (sid, viewer.writes[sid])
        assert gids[sid] >= floor - 1, (gids[sid], floor)
    assert 9 in gids.values()


async def test_audio_and_video_on_a_stalled_session_shed_together(lanes):
    viewer = _Viewer(take=False)
    _vt, video = lanes(viewer, b"video")
    _at, audio = lanes(viewer, b"audio")
    t0 = time.monotonic()
    first = {}
    events = sorted([(i / _FPS, "v", i) for i in range(_FPS * 8)]
                    + [(i / 50, "a", i) for i in range(50 * 8)])
    for at, kind, i in events:
        if kind == "v":
            lane, item = video, _obj(i // _FPS, i % _FPS, 20_000, t0 + at)
        else:
            lane, item = audio, _obj(i, 0, 340, t0 + at)
        _tick(t0 + at)
        lane.offer(item)
        if kind == "a":
            lane.offer(_end(i))
        if lane.sheds and kind not in first:
            first[kind] = at
        if i == 0:
            await _settle(5)
    assert set(first) == {"v", "a"}, first
    for kind, at in first.items():
        assert _DEADLINE < at <= _DEADLINE + _STEP + 1 / _FPS, (kind, at)
    # Within one check plus one video frame of each other.
    assert abs(first["v"] - first["a"]) <= _STEP + 1 / _FPS, first


async def test_bytes_held_by_the_transport_count_toward_time_behind(lanes):
    viewer = _Viewer()
    track, lane = lanes(viewer)
    t0 = t = time.monotonic()
    starts = []            # (bytes written before each object, its arrival)

    def written():
        return track._tx_written.get(id(viewer), 0)
    # The transport takes every write; the peer reads half the rate.
    for gid in range(4):
        for oid in range(_FPS):
            starts.append((written(), t))
            _tick(t)
            lane.offer(_obj(gid, oid, 4000, t))
            t += 1 / _FPS
            if oid == 0:
                await _settle(5)
            assert lane.bytes == 0          # nothing waits on the lane
            if oid % 2:
                viewer.drain(4000)
        lane.offer(_end(gid))
    assert lane.sheds == 0 and viewer.held
    # The oldest byte the transport holds maps to its object's arrival,
    # at most one check late.
    lane._probe(lane.floor)
    unsent = written() - viewer.held
    oldest = max(b for at, b in starts if at <= unsent)
    assert oldest <= lane._oldest <= oldest + _STEP + 1 / _FPS + 1e-6, (
        oldest - t0, lane._oldest - t0)
    # Past the deadline with the queue still empty, the lane sheds.
    gid = 4
    while not lane.sheds and gid < 12:
        for oid in range(_FPS):
            _tick(t)
            lane.offer(_obj(gid, oid, 4000, t))
            t += 1 / _FPS
            if oid == 0:
                await _settle(5)
            if lane.sheds:
                break
            assert lane.bytes == 0
        gid += 1
    assert lane.sheds == 1, gid
    assert t - oldest > _DEADLINE
    await _settle()
    # Old groups' streams are reset; what the transport holds is newer.
    tx = track._tx_streams[id(viewer)]
    assert viewer.resets and all(k[0] >= lane.floor for k in tx), (
        lane.floor, tx)
    assert set(viewer.resets.values()) == {
        int(StreamResetCode.DELIVERY_TIMEOUT)}


@pytest.mark.parametrize("name,keeps", [
    (b"catalog", True), (b"catalog.json", True), (b"video", False)])
async def test_a_catalog_lane_never_sheds_its_newest_group(lanes, name, keeps):
    viewer = _Viewer(take=False)
    _track, lane = lanes(viewer, name)
    t = time.monotonic()
    # Updates every two seconds, then one group spanning 8 s.
    for gid in range(4):
        _tick(t)
        lane.offer(_obj(gid, 0, 500, t))
        lane.offer(_end(gid))
        t += 2.0
        await _settle(5)
        assert lane.floor is None or lane.floor <= gid
    for oid in range(8):
        _tick(t)
        lane.offer(_obj(4, oid, 500, t))
        t += 1.0
    assert lane.sheds >= 1
    if keeps:
        assert lane.floor == 4, lane.floor
        assert any(it[0] == 4 and it[5] is not None for it in lane.items)
    else:
        assert lane.floor == 5, lane.floor


async def test_lanes_over_the_budget_are_held_to_lane_bytes(
        lanes, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    monkeypatch.setattr(relay, "LANE_BYTES", 50_000)
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES", 20_000)
    viewer = _Viewer(take=False)
    _track, lane = lanes(viewer)
    t = time.monotonic()
    for oid in range(3):
        lane.offer(_obj(0, oid, 10_000, t))
        t += 0.01
    await _settle(5)
    assert lane.bound == 0 and lane.sheds == 0
    relay._budget_lanes()
    assert relay._lanes_over_budget and lane.bound == 50_000
    # Well inside the deadline, but past the cap.
    for gid in range(1, 4):
        for oid in range(3):
            lane.offer(_obj(gid, oid, 10_000, t))
            t += 0.01
    assert lane.sheds == 1 and lane.floor >= 2, lane.floor
    assert lane.bytes <= 50_000
    viewer.take = True
    viewer.gate.set()
    await _settle()
    # A shed at the cap is not a deadline drop.
    assert viewer.resets and set(viewer.resets.values()) == {
        int(StreamResetCode.CANCELLED)}, viewer.resets
    msgs = [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "resetting" in r.getMessage()]
    assert len(msgs) == 1, msgs
    assert "at its 50000-byte cap (send lanes over --cache-bytes)" in msgs[0]


async def test_the_shed_warning_states_the_time_behind(lanes, caplog):
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    viewer = _Viewer(take=False)
    _track, lane = lanes(viewer)
    t = time.monotonic()
    for gid in range(7):
        for oid in range(_FPS):
            _tick(t)
            lane.offer(_obj(gid, oid, 1000, t))
            t += 1 / _FPS
        await _settle(2)
    msgs = [r.getMessage() for r in caplog.records
            if "resetting groups below" in r.getMessage()]
    assert lane.sheds >= 1 and msgs, msgs
    warn = [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "resetting" in r.getMessage()]
    assert len(warn) == 1, warn
    assert " s behind (deadline 5.0 s), " in warn[0], warn[0]
    assert "bytes held: resetting groups below " in warn[0], warn[0]
    assert warn[0].startswith("relay: sub "), warn[0]


async def test_a_viewer_keeping_up_costs_no_lane_check_per_object(
        lanes, monkeypatch):
    calls = []
    tx_queued = relay._RelayedTrack._tx_queued

    def counted(self, session, floor=None):
        calls.append(floor)
        return tx_queued(self, session, floor)
    monkeypatch.setattr(relay._RelayedTrack, "_tx_queued", counted)
    viewer = _Viewer()
    track, lane = lanes(viewer)
    t = time.monotonic()
    for gid in range(10):
        for oid in range(_FPS):
            lane.offer(_obj(gid, oid, 4000, t))
            t += 1 / _FPS
            if oid == 0:
                await _settle(5)
            viewer.drain()
        lane.offer(_end(gid))
    await _settle()
    assert track._objects_out == 10 * _FPS and lane.sheds == 0
    # Objects alone neither probe the transport nor mark the lane.
    assert not calls and not lane._marks and not lane.late, calls
    _tick(t)
    assert len(calls) == 1 and not lane.late
    _tick(t + _STEP / 2)
    assert len(calls) == 1


async def test_the_late_mark_clears_without_a_deadline(lanes):
    viewer = _Viewer(take=False)
    _track, lane = lanes(viewer)
    t = time.monotonic()
    for oid in range(3):
        lane.offer(_obj(0, oid, 1000, t))
    await _settle(5)
    _tick(t + _DEADLINE + 1)
    assert lane.late
    lane.deadline = None
    _tick(t + _DEADLINE + 2)
    assert not lane.late and lane.sheds == 0


# -- through the relay, two tracks on one session --------------------------

_NS = ("lanetime", "live")


class _Sub:
    def __init__(self):
        self.objects = defaultdict(set)
        self.ends = {}

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


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._catalogs):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_viewer_keeping_up_on_two_tracks_sheds_neither(
        use_quic, monkeypatch):
    # A short deadline the viewer stays well inside.
    monkeypatch.setattr(relay, "_lag_limit", 0.5)
    _clear_relay()
    port = _BASE_PORT + (not use_quic)
    sizes = {"video": (40_000, 10, _FPS), "audio": (340, 1, 50)}
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=22)
    handle = await server.serve()
    subs = {n: _Sub() for n in sizes}
    watch = asyncio.ensure_future(relay._watch_lanes())
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=22)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            pub = await connect()
            for name, (size, group, rate) in sizes.items():
                PublishedTrack(pub, namespace=_NS, trackname=name,
                               object_size=size, group_size=group,
                               rate=rate).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            viewer = await connect()
            for name, sub in subs.items():
                await sub.subscribe(viewer, name)
            await asyncio.sleep(2.5)
            tracks = {n: relay._tracks[(relay._ns_tuple(_NS), n.encode())]
                      for n in sizes}
            lane_of = {n: t.downstream[0].lane for n, t in tracks.items()}
            sheds = {n: (ln.sheds, ln.shed, ln.expired)
                     for n, ln in lane_of.items()}
            checked = {n: ln._time_due > 0 for n, ln in lane_of.items()}
            groups = {n: t._groups_in for n, t in tracks.items()}
    finally:
        watch.cancel()
        handle.close()
        _clear_relay()
    assert checked == {n: True for n in sizes}, checked
    assert sheds == {n: (0, 0, 0) for n in sizes}, sheds
    for name, sub in subs.items():
        size, group, rate = sizes[name]
        # Every group but the last few in flight, each one whole.
        assert sub.count and len(sub.objects) >= groups[name] - rate // group, (
            name, len(sub.objects), groups[name])
        complete = [k for k, objs in sub.objects.items()
                    if objs == set(range(group))]
        assert len(complete) >= len(sub.objects) - 1, (name, sub.objects)
        resets = [k for k, (clean, _c) in sub.ends.items() if not clean]
        assert not resets, (name, resets)
