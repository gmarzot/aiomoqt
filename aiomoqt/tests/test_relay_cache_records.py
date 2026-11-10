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


def test_cached_records_add_no_tracked_objects():
    n = 200_000
    t = relay._RelayedTrack(((b"gc",), b"audio"))
    gc.collect()
    before = len(gc.get_objects())
    for g in range(n):
        # One object per group, as LOC audio: the most per-group state.
        t._copy(None, g, 0, 0, False)
        t._heads.add((g, 0))
        t._remember(g, 0, 0, b"a", {2: 1 << 40 | g, 7: b"\x01\x02"}, 128,
                    ObjectStatus.NORMAL, (True, False, False), born=g * 0.02)
        t._ended.add((g, 0))
    gc.collect()
    grown = len(gc.get_objects()) - before
    assert len(t._cache) == n and t._cache.group_count() == n
    assert grown < n // 100, grown
    assert len(t._subgroups) <= 2 * relay.STATE_GROUPS
    t._drop_cache()


def test_properties_thaw_to_what_was_frozen():
    for props in ({2: 5, 3: b"x"}, {1: [b"a", b"b"]}, b"\x02\x05", None):
        frozen = relay._cache_record(0, 0, 0, b"p", props, 128, None, None,
                                     0.0)[4]
        assert relay._thaw_props(frozen) == props
    rec = relay._cache_record(0, 0, 0, b"p", {2: 5}, 128,
                              ObjectStatus.END_OF_GROUP, None, 0.0)
    assert relay._thaw_status(rec[6]) is ObjectStatus.END_OF_GROUP
    assert relay._record_cost(rec) == relay.retained_object_bytes(
        b"p", {2: 5})


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
