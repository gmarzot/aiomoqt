"""aiomoqt-relay cache records hold nothing the cyclic GC tracks, nor
does the per-group state kept beside them, and what a replay or a
FETCH sends from them is what was received."""
import gc
from collections import defaultdict
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages.base import KeyValuePairs
from aiomoqt.messages.data import ObjectDatagram
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import ObjectStatus

from aiomoqt.tests.test_relay_send_lanes import _settle


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(relay, "_budget", relay._CacheBudget())
    monkeypatch.setattr(relay, "CACHE_BYTES", 1 << 40)
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES", 1 << 40)


def _fill(t, lo, hi):
    for g in range(lo, hi):
        # One object per group, as LOC audio: the most per-group state.
        t._copy(None, g, 0, 0, False)
        t._heads.add((g, 0))
        t._remember(g, 0, 0, b"a", {2: 1 << 40 | g, 7: b"\x01\x02"}, 128,
                    ObjectStatus.NORMAL, (True, False, False), born=g * 0.02)
        t._ended.add((g, 0))


def _records_tracked():
    """Tracked tuples shaped like a cache record of _fill(). A sealed
    block's payload tuple is not one: before 3.14 it stays tracked until
    a collection first sees it."""
    return sum(1 for o in gc.get_objects()
               if type(o) is tuple and len(o) >= 9 and type(o[0]) is int
               and o[3] == b"a")


def test_cached_records_add_no_tracked_objects():
    t = relay._RelayedTrack(((b"gc",), b"audio"))
    gc.collect()
    gc.disable()
    try:
        before = len(gc.get_objects())
        _fill(t, 0, 50_000)
        at_50k = len(gc.get_objects())
        _fill(t, 50_000, 200_000)
        at_200k = len(gc.get_objects())
        # No collection has run: nothing per record may be tracked.
        assert _records_tracked() == 0
        assert at_50k - before < 50_000 // 100, at_50k - before
        assert at_200k - at_50k < 150_000 // 100, at_200k - at_50k
    finally:
        gc.enable()
    gc.collect()
    assert len(gc.get_objects()) - before < 200_000 // 100
    assert len(t._cache) == 200_000 and t._cache.group_count() == 200_000
    assert len(t._subgroups) <= 2 * relay.STATE_GROUPS
    t._drop_cache()


def test_records_read_back_as_written():
    kvp = KeyValuePairs({2: 5, 9: b"d"})
    kvp.duplicates = frozenset({2})
    cases = [
        (0, 0, b"p", {2: 5, 3: b"x"}, 128, None, (True, False, True)),
        (1, None, b"", {1: [b"a", b"b"]}, None, ObjectStatus.END_OF_GROUP,
         ("DGRAM", True)),
        (2, 3, b"q" * 300, b"\x02\x05", 0, ObjectStatus.NORMAL, None),
        (3, 0, b"r", kvp, 255, ObjectStatus.END_OF_TRACK, (1, 0, 0)),
        (4, 0, b"s", None, 7, None, (False,)),
    ]
    c = relay._ObjectCache()
    for gid, sgid, payload, props, prio, status, shape in cases:
        c.append(gid, sgid, 0, payload, props, prio, status, shape, 1.5)
    # In the open block, then sealed.
    for _ in range(2):
        for gid, sgid, payload, props, prio, status, shape in cases:
            (r,) = c.group(gid)
            assert r[:9] == (gid, sgid, 0, payload, props, prio, status,
                             shape, 1.5)
            assert [type(x) for x in r[7] or ()] == [
                type(x) for x in shape or ()]
            assert r[6] is status and r[3] is payload
            assert r[9] == relay.retained_object_bytes(payload, props)
        c._seal()


class _Wire:
    """A downstream session that keeps each stream's bytes and each
    datagram."""

    def __init__(self):
        self._profile = profile_for(18)
        self._next = 3
        self.streams = defaultdict(bytearray)
        self.fins = []
        self.dgrams = []

    def set_stream_priority(self, _sid, _band):
        return True

    async def open_uni_stream(self, initial=b"", priority=None,
                              end_stream=False):
        sid = self._next
        self._next += 4
        self.stream_write(sid, initial, end_stream)
        return sid

    def stream_write(self, sid, data, end_stream=False):
        self.streams[sid] += data
        if end_stream:
            self.fins.append(sid)

    def stream_write_nowait(self, sid, data, end_stream=False, wait_at=None):
        self.stream_write(sid, data, end_stream)
        return True

    async def stream_write_drain(self, sid, data, end_stream=False):
        self.stream_write(sid, data, end_stream)

    def stream_reset(self, sid, code):
        raise AssertionError(f"stream {sid} reset ({code})")

    def datagram_max_payload(self):
        return 1200

    def send_dgram_message(self, buf):
        self.dgrams.append(bytes(buf.data))

    def wire(self):
        return [bytes(self.streams[s]) for s in sorted(self.streams)]


def _objects():
    """Two groups on streams, each ending in END_OF_GROUP, with Object
    Properties of both value forms, then a group of datagrams."""
    objs = []
    for g in range(2):
        for o in range(3):
            props = KeyValuePairs({2: 1_794_322_654_000 + o, 7: b"loc"})
            props.duplicates = frozenset()
            objs.append(SimpleNamespace(
                group_id=g, object_id=o, payload=b"p%d%d" % (g, o),
                extensions=props if o else {2: 9}, publisher_priority=64,
                status=None, stream_flags=(o == 0, False, False)))
        objs.append(SimpleNamespace(
            group_id=g, object_id=3, payload=b"", extensions=None,
            publisher_priority=64, status=ObjectStatus.END_OF_GROUP,
            stream_flags=(False, False, False)))
    for o in range(2):
        objs.append(ObjectDatagram(
            track_alias=0, group_id=2, object_id=o, publisher_priority=64,
            extensions={2: 77 + o, 9: b"d"}, payload=b"dg%d" % o,
            end_of_group=o == 1))
    return objs


async def test_replay_and_fetch_send_what_was_received():
    track = relay._RelayedTrack(((b"wire",), b"t"))
    live = _Wire()
    track.add_downstream(live, 5, 1)
    try:
        objs = _objects()
        for obj in objs:
            track.on_object(obj, 0, 0, obj.group_id, 0)
        await _settle()
        late = _Wire()
        track.add_downstream(late, 5, 2, replay=True)
        await _settle()
        assert len(live.streams) == 2 and len(live.fins) == 2
        assert late.wire() == live.wire()
        assert len(late.fins) == 2
        assert late.dgrams == live.dgrams and len(live.dgrams) == 2

        got = track.cached_range((0, 0), None)
        want = [(o.group_id, 0, o.object_id, o.payload, o.extensions,
                 o.publisher_priority, o.status) for o in objs]
        assert got == want
        for g, w in zip(got, want):
            assert type(g[4]) in (dict, type(None)) and g[6] is w[6]

        def fetch_wire(rows, prof):
            # As serve_fetch(): status objects only where FETCH carries them.
            out, prior = [], None
            for fo in relay._counted_fetch(rows, None, "cache"):
                fo.status = fo.status or ObjectStatus.NORMAL
                if (not prof.fetch_object_status
                        and fo.status != ObjectStatus.NORMAL):
                    continue
                out.append(bytes(fo.serialize(prof=prof, prior=prior).data))
                prior = fo
            return out
        for draft in (14, 16, 18, 22):
            prof = profile_for(draft)
            assert fetch_wire(got, prof) == fetch_wire(want, prof), draft
    finally:
        track.close()
