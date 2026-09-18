"""Ersatz-relay standalone FETCH, served from its recent-object cache.

The relay registered no FETCH handler, so the session default accepted
with FETCH_OK and never opened the data stream — a peer waited forever.
It now answers from a bounded cache of objects it has forwarded, and
rejects what it cannot serve: joining FETCH (no history), an unknown
track, a range it no longer holds.
"""
import asyncio
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages.data import FetchObject
from aiomoqt.messages.fetch import Fetch
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import GroupOrder, RequestErrorCode

STANDALONE, RELATIVE_JOINING = 0x1, 0x2


def _session(draft=18):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._errors = []
    s._oks = []
    s._served = []
    s.fetch_error = lambda request_id, error_code, reason: (
        s._errors.append((error_code, reason)))
    s.fetch_ok = lambda **kw: s._oks.append(kw)

    async def _serve(request_id, objects, group_order=None, fin=True):
        s._served.append((list(objects), group_order))
        return 0
    s.serve_fetch = _serve
    return s


def _track(objects=(), live=True):
    t = relay._RelayedTrack(((b"live",), b"cam"))
    t.upstream = object() if live else None
    for gid, oid in objects:
        t._remember(gid, 0, oid, b"x" * 10, None, 128, None)
    return t


def _join(track, session, request_id):
    """Attach a subscriber without starting the forward task (no loop)."""
    track.downstream.append((session, 1, request_id))
    track.joined_at[request_id] = track.largest


def _fetch(**kw):
    kw.setdefault("fetch_type", STANDALONE)
    kw.setdefault("group_order", None)     # omitted on the wire = Ascending
    kw.setdefault("request_id", 5)
    kw.setdefault("namespace", (b"live",))
    kw.setdefault("track_name", b"cam")
    return Fetch(**kw)


@pytest.fixture(autouse=True)
def _tables():
    saved = dict(relay._tracks)
    relay._tracks.clear()
    live = relay._track_live
    relay._track_live = lambda t: t.upstream is not None
    yield
    relay._track_live = live
    relay._tracks.clear()
    relay._tracks.update(saved)


def test_joining_fetch_for_unknown_subscription_is_refused():
    s = _session()
    relay._tracks[((b"live",), b"cam")] = _track([(0, 0)])
    asyncio.run(relay._on_fetch(s, _fetch(fetch_type=RELATIVE_JOINING,
                                          joining_request_id=99,
                                          joining_start=2)))
    assert s._errors[0][0] == int(
        RequestErrorCode.INVALID_JOINING_REQUEST_ID)
    assert s._oks == []


def test_joining_fetch_backfills_to_the_subscription_anchor():
    # §10.12.2: the range ends where the subscription started, so the
    # backfill is contiguous with it; joining_start counts groups back.
    s = _session()
    t = _track([(g, o) for g in range(5) for o in range(2)])
    relay._tracks[((b"live",), b"cam")] = t
    _join(t, s, 7)                               # anchor: largest so far
    # A joining FETCH names no track on the wire: both come from the
    # subscription it joins.
    asyncio.run(relay._on_fetch(s, _fetch(fetch_type=RELATIVE_JOINING,
                                          namespace=None, track_name=None,
                                          joining_request_id=7,
                                          joining_start=2)))
    assert s._errors == []
    served, _ = s._served[0]
    assert {o.group_id for o in served} == {2, 3, 4}
    ok = s._oks[0]
    assert (ok['largest_group_id'], ok['largest_object_id']) == (4, 2)


def test_joining_fetch_without_an_anchor_is_refused():
    # Nothing served yet: the subscription covers the track from its
    # start, so there is no backfill to give.
    s = _session()
    t = _track()
    relay._tracks[((b"live",), b"cam")] = t
    _join(t, s, 7)
    asyncio.run(relay._on_fetch(s, _fetch(fetch_type=RELATIVE_JOINING,
                                          joining_request_id=7,
                                          joining_start=2)))
    assert s._errors[0][0] == int(RequestErrorCode.INVALID_RANGE)


def test_unknown_track_is_refused():
    s = _session()
    asyncio.run(relay._on_fetch(s, _fetch(start_group=0, start_object=0)))
    assert s._errors[0][0] == int(RequestErrorCode.DOES_NOT_EXIST)


def test_range_outside_the_cache_with_no_publisher_is_refused():
    s = _session()
    relay._tracks[((b"live",), b"cam")] = _track([(5, 0), (5, 1)])
    asyncio.run(relay._on_fetch(s, _fetch(start_group=0, start_object=0,
                                          end_group=1, end_object=0)))
    assert s._errors[0][0] == int(RequestErrorCode.DOES_NOT_EXIST)


def test_range_outside_the_cache_falls_back_to_upstream(monkeypatch):
    # Only recent objects are cached, so a historical range — or a track
    # the relay never subscribed to — is fetched from the publisher.
    s = _session()
    asked = []

    async def _upstream(ns, track_name, msg):
        asked.append((ns, track_name, msg.start_group, msg.end_group))
        ok = SimpleNamespace(end_of_track=1)
        return ok, [FetchObject(group_id=0, subgroup_id=0, object_id=i,
                                publisher_priority=128, payload=b"z")
                    for i in range(3)]
    monkeypatch.setattr(relay, "_fetch_upstream", _upstream)
    asyncio.run(relay._on_fetch(s, _fetch(start_group=0, start_object=0,
                                          end_group=1, end_object=0)))
    assert asked == [((b"live",), b"cam", 0, 1)]
    assert s._errors == []
    served, _ = s._served[0]
    assert [o.object_id for o in served] == [0, 1, 2]
    # End Of Track is the publisher's answer, carried over verbatim.
    assert s._oks[0]['end_of_track'] == 1


def test_cached_window_is_served_with_exclusive_end_location():
    s = _session()
    relay._tracks[((b"live",), b"cam")] = _track(
        [(1, 0), (1, 1), (2, 0), (2, 1)])
    asyncio.run(relay._on_fetch(s, _fetch(start_group=1, start_object=1,
                                          end_group=2, end_object=0)))
    assert s._errors == []
    ok = s._oks[0]
    # §10.13: End Location is the last object PLUS 1.
    assert (ok['largest_group_id'], ok['largest_object_id']) == (2, 1)
    served, _order = s._served[0]
    assert [(o.group_id, o.object_id) for o in served] == [(1, 1), (2, 0)]


def test_descending_request_serves_groups_in_reverse():
    s = _session()
    relay._tracks[((b"live",), b"cam")] = _track([(1, 0), (2, 0), (3, 0)])
    asyncio.run(relay._on_fetch(s, _fetch(start_group=1, start_object=0,
                                          group_order=GroupOrder.DESCENDING)))
    served, order = s._served[0]
    assert [o.group_id for o in served] == [3, 2, 1]
    assert order == int(GroupOrder.DESCENDING)


def test_cache_drops_oldest_groups():
    t = _track([(g, 0) for g in range(relay.CACHE_GROUPS + 4)])
    groups = {entry[0] for entry in t._cache}
    assert len(groups) == relay.CACHE_GROUPS
    assert min(groups) == 4                      # oldest four evicted


def test_cache_drops_on_the_byte_bound(monkeypatch):
    monkeypatch.setattr(relay, "CACHE_BYTES", 100)
    t = relay._RelayedTrack(((b"live",), b"cam"))
    for oid in range(20):
        t._remember(0, 0, oid, b"x" * 10, None, 128, None)
    assert t._cache_bytes <= 100
    assert [e[2] for e in t._cache] == list(range(10, 20))


def test_a_raising_handler_still_answers_the_request():
    # The failure shape behind most conformance losses: a handler that
    # raises leaves the request stream open with no reply and the peer
    # waits out its timeout. Every request gets a terminal answer.
    s = _session()
    sent = []
    s._send_reply = lambda rid, m, fin=False: sent.append((rid, m, fin))

    async def _boom(session, msg):
        raise RuntimeError("kaboom")

    asyncio.run(relay._answered(_boom)(s, _fetch(request_id=11)))
    assert len(sent) == 1
    rid, msg, fin = sent[0]
    assert rid == 11 and fin is True
    assert msg.error_code == int(RequestErrorCode.INTERNAL_ERROR)


def test_terminal_waits_for_the_streams_publish_done_counts():
    # §10.11: PUBLISH_DONE follows every stream the publisher opened, but
    # the control message races those streams on the wire. Cutting the
    # subscriber off early loses objects it was still owed.
    async def _run():
        t = _track()
        t.task = object()                   # a drain exists
        t.note_upstream_done(0x2, "done", ((b"live",), b"cam"),
                             stream_count=2)
        assert t.queue.qsize() == 0         # held: no streams ended yet
        t._upstream_ended = 1
        t._check_pending_done()
        assert t.queue.qsize() == 0         # still one short
        t._upstream_ended = 2
        t._check_pending_done()
        assert t.queue.get_nowait()[6] == "DONE"
    asyncio.run(_run())


def test_range_older_than_the_cache_goes_upstream(monkeypatch):
    # A partly-held range is not a cache hit: the evicted head has to
    # come from the publisher, or the subscriber gets a short answer
    # with no way to tell it was short.
    s = _session()
    t = _track([(g, 0) for g in range(3, 6)])     # groups 0-2 evicted
    relay._tracks[((b"live",), b"cam")] = t
    asked = []

    async def _upstream(ns, track_name, msg):
        asked.append((msg.start_group, msg.end_group))
        return SimpleNamespace(end_of_track=0), [
            FetchObject(group_id=g, subgroup_id=0, object_id=0,
                        publisher_priority=128, payload=b"z")
            for g in range(0, 6)]
    monkeypatch.setattr(relay, "_fetch_upstream", _upstream)
    asyncio.run(relay._on_fetch(s, _fetch(start_group=0, start_object=0,
                                          end_group=5, end_object=0)))
    assert asked == [(0, 5)]                      # went upstream
    served, _ = s._served[0]
    assert [o.group_id for o in served] == [0, 1, 2, 3, 4, 5]


def test_range_inside_the_cache_is_served_from_it(monkeypatch):
    s = _session()
    t = _track([(g, 0) for g in range(3, 6)])
    relay._tracks[((b"live",), b"cam")] = t

    async def _never(ns, track_name, msg):
        raise AssertionError("should not ask upstream")
    monkeypatch.setattr(relay, "_fetch_upstream", _never)
    asyncio.run(relay._on_fetch(s, _fetch(start_group=4, start_object=0)))
    served, _ = s._served[0]
    assert [o.group_id for o in served] == [4, 5]
