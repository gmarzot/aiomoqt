"""Fan-out: one packaged object sequence reaching several peers.

Delivery runs against stand-in sessions that record their writes, so
the numbering and the shedding are observable without a transport.
"""
import asyncio
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.delivery import FanoutDelivery, StreamMapping, SubgroupDelivery
from aiomoqt.fanout import FanoutPublisher
from aiomoqt.media import Catalog, CatalogTrack, LocTrackPublisher
from aiomoqt.media.loc import LOC02_PROP_TIMESTAMP, LOC_PROP_TIMESTAMP
from aiomoqt.track import PublishedTrack, TrackState

_NS = "fan/test"


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
        self._gate = asyncio.Event()
        if not block:
            self._gate.set()

    def release(self):
        self._gate.set()

    async def open_uni_stream(self):
        sid = self._next
        self._next += 4
        self.opened.append(sid)
        return sid

    def stream_write(self, sid, data, end_stream=False):
        self.writes.append((sid, len(data), end_stream))

    async def stream_write_drain(self, sid, data):
        await self._gate.wait()
        self.writes.append((sid, len(data), False))


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
    fan = FanoutDelivery(queue_size=2)
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


@pytest.mark.asyncio
async def test_a_paused_peer_is_gated_and_rejoins_at_a_group():
    a, b = _Session("a"), _Session("b")
    da, db = _delivery(a), _delivery(b)
    paused = SimpleNamespace(forward=True)
    fan = FanoutDelivery()
    fan.add(da, joining=False)
    fan.add(db, lambda: paused.forward, joining=False)

    await fan.write(0, 0, b"key", group_start=True)
    await _settle()
    paused.forward = False
    await fan.write(0, 1, b"pred")
    await fan.write(1, 0, b"key", group_start=True)
    await _settle()
    assert db.objects_sent == 1              # nothing while paused

    paused.forward = True
    await fan.write(1, 1, b"pred")           # mid-group: still skipped
    await fan.write(2, 0, b"key", group_start=True)
    await _settle()
    await fan.close()
    assert db.largest == (2, 0)
    assert da.objects_sent == 5


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
