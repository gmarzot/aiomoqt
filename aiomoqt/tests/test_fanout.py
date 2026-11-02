"""Fan-out: one packaged object sequence reaching several peers.

Delivery runs against stand-in sessions that record their writes, so
the numbering and the shedding are observable without a transport.
"""
import asyncio
from types import SimpleNamespace

import pytest

from aiomoqt import delivery as delivery_mod
from aiomoqt.context import profile_for
from aiomoqt.delivery import (FanoutDelivery, OBJECT_RECORD_BYTES,
                              PUBLISHER_LANE_BYTES, SendLane, StreamMapping,
                              SubgroupDelivery)
from aiomoqt.fanout import FanoutPublisher
from aiomoqt.media import Catalog, CatalogTrack, LocTrackPublisher
from aiomoqt.media.loc import LOC02_PROP_TIMESTAMP, LOC_PROP_TIMESTAMP
from aiomoqt import track as track_mod
from aiomoqt.track import PublishedTrack, TrackState
from aiomoqt.types import ParamType

_NS = "fan/test"
# A lane bound of two queued objects.
_TWO = 2 * (OBJECT_RECORD_BYTES + 4)


class _Session:
    """Records stream opens and writes. `block=True` parks every drained
    write until `release()`, which is what a backed-up relay looks like."""

    def __init__(self, name="s", block=False, draft=18):
        self.name = name
        self.negotiated_draft = draft
        self._profile = profile_for(draft)
        self._next = 3
        self.opened = []
        self.writes = []            # (stream_id, nbytes, fin)
        self.resets = []            # stream_id
        self._gate = asyncio.Event()
        if not block:
            self._gate.set()

    def release(self):
        self._gate.set()

    def set_stream_priority(self, _sid, _band):
        return True

    async def open_uni_stream(self, initial=b"", priority=None):
        sid = self._next
        self._next += 4
        self.opened.append(sid)
        if initial:
            try:
                await self.stream_write_drain(sid, initial)
            except BaseException:
                self.stream_reset(sid, 0)
                raise
        return sid

    def stream_write(self, sid, data, end_stream=False):
        self.writes.append((sid, len(data), end_stream))

    def stream_reset(self, sid, code):
        self.resets.append(sid)

    def fins(self, sid):
        return [w for w in self.writes if w[0] == sid and w[2]]

    async def stream_write_drain(self, sid, data):
        await self._gate.wait()
        self.writes.append((sid, len(data), False))

    def _unbind_request(self, request_id):
        pass


async def _settle(times=12):
    for _ in range(times):
        await asyncio.sleep(0)


def _delivery(session, alias=1, mapping=StreamMapping.PER_GROUP):
    return SubgroupDelivery(session, alias, mapping=mapping)


# -- one delivery ----------------------------------------------------

@pytest.mark.asyncio
async def test_per_group_holds_one_stream_open_per_group():
    s = _Session()
    out = _delivery(s)
    await out.write(0, 0, b"k", group_start=True)
    await out.write(0, 1, b"p")
    await out.write(1, 0, b"k", group_start=True)
    out.end_group()
    assert len(s.opened) == 2            # one stream per group
    assert out.stream_count == 2
    assert out.largest == (1, 0)
    assert out.objects_sent == 3
    assert sum(1 for _s, _n, fin in s.writes if fin) == 2   # both FINed


@pytest.mark.asyncio
async def test_per_object_opens_and_fins_a_stream_each_time():
    s = _Session()
    out = _delivery(s, mapping=StreamMapping.PER_OBJECT)
    await out.write(0, 0, b"a", group_start=True)
    await out.write(0, 1, b"b")
    assert len(s.opened) == 2
    assert out.stream_count == 2
    assert sum(1 for _s, _n, fin in s.writes if fin) == 2


@pytest.mark.asyncio
async def test_largest_is_a_max_not_the_last_write():
    s = _Session()
    out = _delivery(s)
    await out.write(5, 0, b"k", group_start=True)
    await out.write(2, 0, b"k", group_start=True)
    assert out.largest == (5, 0)


# -- several deliveries ----------------------------------------------

@pytest.mark.asyncio
async def test_every_lane_gets_the_same_ids():
    a, b = _Session("a"), _Session("b")
    da, db = _delivery(a), _delivery(b, alias=7)
    fan = FanoutDelivery([da, db])
    for gid in (0, 1):
        await fan.write(gid, 0, b"key", group_start=True)
        await fan.write(gid, 1, b"pred")
    await fan.close()
    assert da.largest == db.largest == (1, 1)
    assert da.objects_sent == db.objects_sent == 4
    assert fan.largest == (1, 1)


@pytest.mark.asyncio
async def test_a_blocked_lane_sheds_to_the_next_group():
    fast, slow = _Session("fast"), _Session("slow", block=True)
    dfast, dslow = _delivery(fast), _delivery(slow)
    fan = FanoutDelivery(max_bytes=_TWO)
    fan.add(dfast, joining=False)
    lane = fan.add(dslow, joining=False)

    await fan.write(0, 0, b"key", group_start=True)
    await _settle()
    for oid in range(1, 8):                 # overruns the slow lane
        await fan.write(0, oid, b"pred")
        await _settle(2)                    # the fast lane keeps draining
    await fan.write(1, 0, b"key", group_start=True)
    await _settle()
    assert lane.shed > 0
    slow.release()
    await _settle(60)
    await fan.close()

    # The fast lane took everything; the slow one rejoined at group 1
    # and never wrote an object from the middle of group 0.
    assert dfast.objects_sent == 9
    assert dslow.largest == (1, 0)
    assert dslow.objects_sent < dfast.objects_sent
    # The truncated group is reset, never ended as if complete.
    assert slow.resets == [slow.opened[0]]
    assert not slow.fins(slow.opened[0])
    assert fast.resets == [] and fast.fins(fast.opened[0])


async def _overrun_at_a_group_boundary(boundary):
    """A blocked lane's queue overflows on the item that ends group 0
    (`boundary` writes it); returns both sessions once drained."""
    fast, slow = _Session("fast"), _Session("slow", block=True)
    fan = FanoutDelivery(max_bytes=_TWO)
    fan.add(_delivery(fast), joining=False)
    lane = fan.add(_delivery(slow), joining=False)
    await fan.write(0, 0, b"key", group_start=True)
    await _settle()                         # slow lane parked in group 0
    await fan.write(0, 1, b"pred")
    await _settle(2)                        # the fast lane keeps draining
    await fan.write(0, 2, b"pred")          # slow queue now full
    await _settle(2)
    await boundary(fan)                     # overflows: backlog flushed
    assert lane.shed == 2
    slow.release()
    await _settle(60)
    await fan.close()
    return fast, slow


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["next_group", "end_group"])
async def test_overflow_at_a_group_boundary_resets_the_group(boundary):
    async def _next_group(fan):
        await fan.write(1, 0, b"key", group_start=True)

    async def _end_group(fan):
        fan.end_group()
        await fan.write(1, 0, b"key", group_start=True)

    fast, slow = await _overrun_at_a_group_boundary(
        {"next_group": _next_group, "end_group": _end_group}[boundary])
    # Group 0 lost objects 1 and 2 on the slow peer: reset, no
    # END_OF_GROUP and no FIN; the reset lands on the parked header.
    first = slow.opened[0]
    assert slow.resets == [first]
    assert not slow.fins(first)
    assert len([w for w in slow.writes if w[0] == first]) == 1  # header
    assert fast.resets == [] and fast.fins(fast.opened[0])


@pytest.mark.asyncio
async def test_a_paused_peer_is_gated_and_rejoins_at_a_group():
    a, b = _Session("a"), _Session("b")
    da, db = _delivery(a), _delivery(b)
    fan = FanoutDelivery()
    fan.add(da, joining=False)
    paused = fan.add(db, joining=False)

    await fan.write(0, 0, b"key", group_start=True)
    await _settle()
    paused.gate = False
    await fan.write(0, 1, b"pred")
    await fan.write(1, 0, b"key", group_start=True)
    await _settle()
    assert db.objects_sent == 1              # nothing while paused

    paused.gate = True
    await fan.write(1, 1, b"pred")           # mid-group: still skipped
    await fan.write(2, 0, b"key", group_start=True)
    await _settle()
    await fan.close()
    assert db.largest == (2, 0)
    assert da.objects_sent == 5
    assert b.resets == [b.opened[0]] and not b.fins(b.opened[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("rejoin", [True, False], ids=["rejoin", "wait"])
async def test_a_resumed_peer_rejoins_the_open_group_or_waits(rejoin):
    a, b = _Session("a"), _Session("b")
    da, db = _delivery(a), _delivery(b)
    fan = FanoutDelivery()
    fan.add(da, joining=False)
    paused = fan.add(db, joining=False)

    await fan.write(0, 0, b"key", group_start=True)
    await _settle()
    paused.gate = False
    db.cancel()                              # cut at Forward State 0
    await fan.write(0, 1, b"pred")
    await _settle()
    paused.gate = True
    paused.resume(0, rejoin=rejoin)
    await fan.write(0, 2, b"pred")
    await _settle()
    if rejoin:
        # Group 0 again on a new stream, FIRST_OBJECT clear.
        assert db.open_group == 0 and not db._header.first_object
    else:
        assert db.open_group is None
    await fan.write(1, 0, b"key", group_start=True)
    await _settle()
    assert db.open_group == 1 and db._header.first_object
    await fan.close()

    assert da.objects_sent == 4 and len(a.opened) == 2 and a.resets == []
    assert db.objects_sent == (3 if rejoin else 2)
    assert len(b.opened) == (3 if rejoin else 2)
    assert b.resets == [b.opened[0]]


@pytest.mark.asyncio
async def test_a_shed_group_is_not_rejoined():
    slow = _Session("slow", block=True)
    ds = _delivery(slow)
    fan = FanoutDelivery(max_bytes=_TWO)
    lane = fan.add(ds, joining=False)

    await fan.write(0, 0, b"key", group_start=True)
    await _settle()
    for oid in range(1, 6):                 # overruns the lane in group 0
        await fan.write(0, oid, b"pred")
    assert lane.floor == 1
    lane.resume(0, rejoin=True)
    slow.release()
    await fan.write(0, 6, b"pred")
    await fan.write(1, 0, b"key", group_start=True)
    await _settle(30)
    await fan.close()
    assert ds.objects_sent == 1 and ds.largest == (1, 0)


@pytest.mark.asyncio
async def test_on_sent_reports_only_written_objects():
    """Once per peer per object written; nothing for a peer that is
    paused, filtered out or shed."""
    names = ("all", "paused", "filtered", "slow")
    sessions = {n: _Session(n, block=n == "slow") for n in names}
    deliveries = {n: _delivery(s) for n, s in sessions.items()}
    deliveries["filtered"].passes = lambda g, o: o != 1
    fan = FanoutDelivery(max_bytes=_TWO)
    sent = []
    fan.on_sent = lambda g, o: sent.append((g, o))
    fan.add(deliveries["all"], joining=False)
    fan.add(deliveries["paused"], gate=False, joining=False)
    fan.add(deliveries["filtered"], joining=False)
    slow = fan.add(deliveries["slow"], joining=False)

    await fan.write(0, 0, b"key", group_start=True)
    await _settle()
    for oid in range(1, 6):                 # overruns the slow lane
        await fan.write(0, oid, b"pred")
        await _settle(2)
    sessions["slow"].release()
    await _settle(60)
    await fan.close()

    assert slow.shed > 0
    assert deliveries["paused"].objects_sent == 0
    # The slow peer's one object was reset with its parked header.
    assert sorted(sent) == sorted(
        [(0, o) for o in range(6)]              # all
        + [(0, o) for o in (0, 2, 3, 4, 5)])    # filtered
    assert len(sent) == sum(d.objects_sent for d in deliveries.values())


@pytest.mark.asyncio
async def test_a_lane_added_mid_stream_joins_at_the_next_group():
    a, b = _Session("a"), _Session("b")
    da, db = _delivery(a), _delivery(b)
    fan = FanoutDelivery([da])
    await fan.write(0, 0, b"key", group_start=True)
    await _settle()
    fan.add(db)                              # joining=True by default
    await fan.write(0, 1, b"pred")
    await _settle()
    assert db.objects_sent == 0              # cannot start mid-group
    await fan.write(1, 0, b"key", group_start=True)
    await _settle()
    await fan.close()
    assert db.largest == (1, 0)


@pytest.mark.asyncio
async def test_drop_session_detaches_a_lane():
    a, b = _Session("a"), _Session("b")
    da, db = _delivery(a), _delivery(b)
    fan = FanoutDelivery([da, db])
    fan.drop_session(b)
    await fan.write(0, 0, b"key", group_start=True)
    await _settle()
    await fan.close()
    assert [ln.session for ln in fan.lanes] == [a]
    assert db.objects_sent == 0


@pytest.mark.asyncio
async def test_a_reset_during_a_header_drain_leaves_the_lane_writing():
    s = _Session(block=True)
    d = _delivery(s)
    fan = FanoutDelivery([d])
    sent = []
    fan.on_sent = lambda g, o: sent.append((g, o))
    lane = fan.lanes[0]
    await fan.write(0, 0, b"k0", group_start=True)
    await _settle()                     # parked in the header drain
    d.cancel()                          # e.g. PUBLISH_OK with FORWARD=0
    s.release()
    await _settle()
    await fan.write(1, 0, b"k1", group_start=True)
    await fan.write(1, 1, b"p1")
    await _settle()
    assert not lane.task.done()
    first, second = s.opened
    assert s.resets == [first]
    assert [w for w in s.writes if w[0] == second and w[1]] != []
    assert sent == [(1, 0), (1, 1)]
    assert d.objects_sent == 2
    await fan.close()


class _Failing(_Session):
    """The first drained write raises."""

    def __init__(self):
        super().__init__()
        self.failed = False

    async def stream_write_drain(self, sid, data):
        if not self.failed:
            self.failed = True
            raise RuntimeError("transport fault")
        await super().stream_write_drain(sid, data)


@pytest.mark.asyncio
async def test_a_lane_survives_a_failed_write(caplog):
    s = _Failing()
    d = _delivery(s)
    fan = FanoutDelivery()
    fan.name = "fan/test/video"
    lane = fan.add(d, joining=False)
    delivery_mod.logger.addHandler(caplog.handler)
    try:
        await fan.write(0, 0, b"k0", group_start=True)
        await fan.write(0, 1, b"p0")    # skipped: the lane waits for a group
        await fan.write(1, 0, b"k1", group_start=True)
        await _settle()
    finally:
        delivery_mod.logger.removeHandler(caplog.handler)
    assert not lane.task.done()
    assert d.objects_sent == 1 and d.largest == (1, 0)
    assert s.resets == [s.opened[0]]
    assert any("fan/test/video" in r.getMessage() and r.levelname == "WARNING"
               for r in caplog.records)
    await fan.close()



class _Backlogged(_Session):
    """Reports the bytes the transport still holds per stream."""

    def __init__(self, name="s"):
        super().__init__(name)
        self.held = {}
        self._quic = SimpleNamespace(
            tx_data_ring_used=lambda sid: self.held.get(sid, 0))


@pytest.mark.asyncio
async def test_a_lane_counts_what_the_transport_still_holds():
    s = _Backlogged()
    d = _delivery(s)
    fan = FanoutDelivery(max_bytes=10_000)
    lane = fan.add(d, joining=False)
    await fan.write(0, 0, b"key", group_start=True)
    await fan.write(0, 1, b"pred")
    fan.end_group()
    await _settle()
    first = s.opened[0]
    assert s.fins(first)
    s.held[first] = 20_000              # FINed, not yet sent out
    await fan.write(1, 0, b"key", group_start=True)
    await fan.write(1, 1, b"pred")
    await _settle()
    # Over the bound on the backlog alone: the group below the floor is
    # reset, nothing queued is dropped, and group 1 goes out.
    assert lane.sheds == 1 and lane.shed == 0 and lane.floor == 1
    assert s.resets == [first]
    assert d.largest == (1, 1)
    s.held.clear()
    assert d.tx_queued() == 0 and not d._tx_streams
    await fan.close()



class _Nowait(_Session):
    """Takes a write without waiting unless `refuse(n)` says no for the
    n-th; records the bytes of every stream write in order."""

    def __init__(self, refuse):
        super().__init__("nowait")
        self.refuse = refuse
        self.tries = 0
        self.inline = 0
        self.data = []              # (stream_id, bytes, fin)

    def stream_write_nowait(self, sid, data):
        self.tries += 1
        if self.refuse(self.tries):
            return False
        self.inline += 1
        self.data.append((sid, bytes(data), False))
        return True

    def stream_write(self, sid, data, end_stream=False):
        super().stream_write(sid, data, end_stream)
        self.data.append((sid, bytes(data), end_stream))

    async def stream_write_drain(self, sid, data):
        await super().stream_write_drain(sid, data)
        self.data.append((sid, bytes(data), False))

    def streams(self):
        """Each stream's writes, streams in header order."""
        return sorted([(d, fin) for s, d, fin in self.data if s == sid]
                      for sid in self.opened)


async def _two_tracks_mixed(monkeypatch, write_through):
    """Two tracks on one session; every third inline write is refused and
    drains park for a stretch of group 1."""
    monkeypatch.setattr(SendLane, "write_through", write_through)
    s = _Nowait(lambda n: n % 3 == 0)
    fans = [FanoutDelivery([_delivery(s, alias)]) for alias in (1, 2)]
    for gid in range(3):
        for oid in range(6):
            if (gid, oid) == (1, 2):
                s._gate.clear()
            if (gid, oid) == (1, 5):
                s.release()
            for k, fan in enumerate(fans):
                await fan.write(gid, oid, b"%d.%d.%d" % (k, gid, oid),
                                group_start=oid == 0)
        for fan in fans:
            fan.end_group()
    for fan in fans:
        await fan.close()
    return s


@pytest.mark.asyncio
async def test_write_through_keeps_the_queued_order(monkeypatch):
    fast = await _two_tracks_mixed(monkeypatch, True)
    queued = await _two_tracks_mixed(monkeypatch, False)
    assert fast.inline and fast.tries > fast.inline     # both paths ran
    assert queued.tries == 0
    assert len(fast.opened) == len(queued.opened) == 6
    assert fast.streams() == queued.streams()
    assert fast.resets == queued.resets == []


@pytest.mark.asyncio
async def test_write_through_respects_gate_floor_and_bound():
    s = _Nowait(lambda n: False)
    d = _delivery(s)
    fan = FanoutDelivery(max_bytes=_TWO)
    lane = fan.add(d, joining=False)
    await fan.write(0, 0, b"key", group_start=True)     # opens: queued
    await _settle()
    await fan.write(0, 1, b"pred")                      # inline
    assert s.inline == 1 and not lane.items
    lane.gate = False
    await fan.write(0, 2, b"pred")                      # queued, dropped
    await _settle()
    assert s.inline == 1 and lane.floor == 1
    lane.gate = True
    await fan.write(0, 3, b"pred")                      # below the floor
    assert s.inline == 1 and lane.shed == 1
    await fan.write(1, 0, b"key", group_start=True)
    await _settle()
    await fan.write(1, 1, b"x" * (2 * _TWO))            # over the bound
    assert s.inline == 1 and lane.sheds == 1
    await fan.close()
    assert d.largest == (1, 0)


@pytest.mark.asyncio
async def test_an_offer_behind_a_queued_object_queues():
    """An object offered while an earlier one waits in the queue queues
    too, though the writer would take it inline: order is kept."""
    s = _Nowait(lambda n: n == 1)
    fan = FanoutDelivery()
    lane = fan.add(_delivery(s), joining=False)
    await fan.write(0, 0, b"key", group_start=True)     # opens: queued
    await _settle()

    def item(oid, payload):
        return (0, OBJECT_RECORD_BYTES + len(payload), True, None, oid,
                payload, None, False, None)
    assert lane.offer(item(1, b"one"))                  # refused: queued
    assert lane.offer(item(2, b"two"))                  # no yield between
    assert s.tries == 1 and len(lane.items) == 2
    await _settle()
    await fan.close()
    sent = [p for _, data, _ in s.data for p in (b"one", b"two")
            if data.endswith(p)]
    assert sent == [b"one", b"two"]


# -- the track layer -------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_are_per_peer_and_the_first_is_the_track():
    a, b = _Session("a"), _Session("b")
    track = PublishedTrack(a, _NS, "video")
    assert track.demand == (0, 1)

    sub_b = track.add_session(b)
    assert track._sub_for(b) is sub_b
    assert track._sub_for(a) is track.subscriptions[0]

    # The plain attributes are the first peer's, as they always were.
    track.track_alias = 9
    track.state = TrackState.SUBSCRIBED
    track._generating = True
    assert track.subscriptions[0].track_alias == 9
    assert track.demand == (1, 2)

    sub_b.state = TrackState.SUBSCRIBED
    sub_b.generating = True
    assert track.demand == (2, 2)
    assert track.track_alias == 9        # peer B has its own alias
    assert sub_b.track_alias == 0

    # An idle peer is subscribed but not producing.
    track._on_request_cancelled(4, sub_b)
    assert track.demand == (1, 2)

    track.drop_session(b)
    assert track.demand == (1, 1)


@pytest.mark.asyncio
async def test_dropping_the_last_peer_leaves_the_track_usable():
    a = _Session("a")
    track = PublishedTrack(a, _NS, "video")
    track.drop_session(a)
    assert len(track.subscriptions) == 1


@pytest.mark.asyncio
async def test_publisher_shares_one_track_across_sessions():
    a, b = _Session("a"), _Session("b")
    catalog = Catalog(generatedAt=0, tracks=[CatalogTrack(
        name="video", packaging="loc", isLive=True, role="video",
        codec="avc1.42c00d", bitrate=1)])
    pub = FanoutPublisher([a, b], _NS, catalog)
    track = pub.add_track(LocTrackPublisher(pub.session, _NS, "video",
                                            config=b"avcC"))

    assert pub.tracks["video"] is track
    assert [s.session for s in track.subscriptions] == [a, b]
    for mp in pub.publishers:
        assert mp._by_name["video"] is track      # the same object
    assert pub.catalog_track.catalog is catalog
    assert len(pub.catalog_track.members) == 2    # catalog stays per-peer

    pub.drop(a)
    assert [s.session for s in track.subscriptions] == [b]
    assert [p.session for p in pub.publishers] == [b]
    assert [m.session for m in pub.catalog_track.members] == [b]



@pytest.mark.asyncio
async def test_objects_are_timed_only_while_a_lane_has_a_timeout():
    s = _Session()
    fan = FanoutDelivery()
    lane = fan.add(_delivery(s), joining=False)
    items = []
    real = lane.offer
    lane.offer = lambda item: items.append(item) or real(item)
    await fan.write(0, 0, b"k", group_start=True)
    lane.deadline = 0.5
    await fan.write(0, 1, b"p")
    lane.deadline = None
    await fan.write(0, 2, b"p")
    assert [item[3] is not None for item in items] == [False, True, False]
    await fan.close()


@pytest.mark.asyncio
async def test_a_peer_filter_reaches_its_delivery_only_while_filtered():
    a = _Session("a")
    track = _Counter(a, 0)
    sub = track.subscriptions[0]
    sub.generating = True
    track._add_delivery(sub)
    assert sub.delivery.passes is None
    sub.window = ((2, 0), None)
    assert sub.delivery.passes == sub.passes
    assert not sub.delivery.passes(1, 5) and sub.delivery.passes(2, 0)
    sub.window = track_mod._UNFILTERED
    assert sub.delivery.passes is None
    await track._out.close()


# -- the track layer owns fan-out ------------------------------------

class _Counter(PublishedTrack):
    """The smallest produce() track."""

    def __init__(self, session, n):
        super().__init__(session, _NS, "count")
        self.n = n

    async def produce(self, out):
        for i in range(self.n):
            await out.write(i, 0, b"%d" % i, group_start=True)


@pytest.mark.asyncio
async def test_any_produce_track_fans_out_without_knowing():
    a, b = _Session("a"), _Session("b")
    track = _Counter(a, 3)
    track.add_session(b)
    await asyncio.gather(track.generate(a, 1), track.generate(b, 2))
    assert len(a.opened) == 3 and len(b.opened) == 3
    assert track._production.done()


@pytest.mark.asyncio
async def test_forward_state_and_timeouts_reach_the_lane():
    a = _Session("a")
    track = _Counter(a, 0)
    sub = track.subscriptions[0]
    sub.generating = True
    track._add_delivery(sub)
    lane = sub.delivery.lane
    assert lane.gate and lane.deadline is None
    sub.forward = False
    assert not lane.gate
    sub.forward = True
    assert lane.gate
    track._generating = False
    assert not lane.gate
    track.object_delivery_timeout = 250
    assert lane.deadline == 0.25
    track._take_timeouts(sub, {ParamType.DELIVERY_TIMEOUT: 100}, a._profile)
    assert lane.deadline == 0.1
    await track._out.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,bound", [({}, PUBLISHER_LANE_BYTES),
                                      ({"lane_bytes": 1 << 20}, 1 << 20)])
async def test_a_tracks_lanes_take_its_bound(kw, bound):
    track = PublishedTrack(_Session("a"), _NS, "t", **kw)
    sub = track.subscriptions[0]
    track._add_delivery(sub)
    assert sub.delivery.lane.max_bytes == bound
    await track._out.close()


@pytest.mark.asyncio
async def test_loc_properties_follow_each_peers_draft():
    # d18 refuses 0x06 as a Track-scope id; the d16 loc-02 ecosystem
    # reads it. One frame, encoded per peer.
    a, b = _Session("a", draft=18), _Session("b", draft=16)
    track = LocTrackPublisher(a, _NS, "video")
    track.add_session(b)
    built = {}
    real = track._object_extensions

    def spy(frame, group_start, session):
        exts = real(frame, group_start, session)
        built[session.name] = set(exts)
        return exts

    track._object_extensions = spy
    for sub in track.subscriptions:
        await track._start_generating(sub.session, "SUBSCRIBE")
    await track.send_frame(b"idr", key_frame=True, timestamp=1)
    await track.finish()
    await track._production
    assert LOC_PROP_TIMESTAMP in built["a"]
    assert LOC02_PROP_TIMESTAMP not in built["a"]
    assert LOC02_PROP_TIMESTAMP in built["b"]


@pytest.mark.asyncio
async def test_a_peer_returning_from_idle_swaps_its_lane_not_production():
    a, b = _Session("a"), _Session("b")
    track = LocTrackPublisher(a, _NS, "video")
    sub_b = track.add_session(b)
    await track._start_generating(a, "SUBSCRIBE")
    sub_b.track_alias, sub_b.subscribers = 5, {7}
    await track._start_generating(b, "SUBSCRIBE")
    production = track._production
    assert len(track._out.lanes) == 2

    track._on_request_cancelled(7, sub_b)        # b's last viewer left
    assert track.demand == (1, 2)
    assert track.producing                       # a still takes objects

    sub_b.track_alias = 9                        # b's next viewer
    await track._start_generating(b, "SUBSCRIBE")
    assert track._production is production
    assert len(track._out.lanes) == 2
    assert sub_b.delivery.track_alias == 9
    await track.finish()
    await production


@pytest.mark.asyncio
async def test_a_stopped_lane_is_replaced_when_forward_resumes():
    a = _Session("a")
    track = LocTrackPublisher(a, _NS, "video")
    await track._start_generating(a, "PUBLISH_OK")
    dead = track._out.lanes[0]
    dead.task.cancel()
    await _settle()
    await track._start_generating(a, "REQUEST_UPDATE")
    lane, = track._out.lanes
    assert lane is not dead and not lane.task.done()
    await track.send_frame(b"idr", key_frame=True, timestamp=1)
    await track.finish()
    await track._production
    assert track._sub_for(a).delivery.objects_sent == 1


def test_packaging_does_not_know_about_peers():
    # Fan-out belongs to the track and delivery layers, so any packager
    # gets it unchanged. broadcast.py composes sessions and is exempt.
    import pathlib
    import aiomoqt.media as media
    root = pathlib.Path(media.__file__).parent
    for path in root.glob("*.py"):
        if path.name == "broadcast.py":
            continue
        text = path.read_text()
        for name in ("FanoutDelivery", "SubgroupDelivery", "_subs",
                     "subscriptions", "add_session", "_sub_for",
                     "drop_session"):
            assert name not in text, f"{path.name} references {name}"
