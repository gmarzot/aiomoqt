"""Ersatz-relay standalone FETCH, served from its recent-object cache.

The relay registered no FETCH handler, so the session default accepted
with FETCH_OK and never opened the data stream — a peer waited forever.
It now answers from a bounded cache of objects it has forwarded, and
rejects what it cannot serve: joining FETCH (no history), an unknown
track, a range it no longer holds.
"""
import asyncio
import contextlib
import logging
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for, request_error_code
from aiomoqt.messages.data import FetchObject
from aiomoqt.messages.fetch import Fetch
from aiomoqt.messages.subscribe import Subscribe
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (FilterType, GroupOrder, MOQTRequestError, ParamType,
                           RequestErrorCode, StreamResetCode)
from aiomoqt.utils.buffer import Buffer

from aiomoqt.tests._certs import CERT, KEY, requires_certs

STANDALONE, RELATIVE_JOINING = 0x1, 0x2


def _session(draft=18):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._errors = []
    s._oks = []
    s._served = []
    # Recorded as the wire value the session would send at this draft.
    s.fetch_error = lambda request_id, error_code, reason: (
        s._errors.append((request_error_code(error_code, s._profile, fetch=True),
                          reason)))
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


def _join(track, session, request_id, forward=True):
    """Attach a subscriber without starting the forward task (no loop)."""
    d = relay._Downstream(session, 1, request_id, forward=forward)
    d.joined_at = track.largest if forward else None
    track.downstream.append(d)


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


def test_a_joining_fetch_is_held_until_its_subscribe_attaches():
    # §10.12.2: sent right behind its SUBSCRIBE, it waits for it.
    key = ((b"live",), b"cam")
    s = _session()
    s._peer_requests = {7: (Subscribe, key)}
    t = _track([(g, 0) for g in range(3)])
    relay._tracks[key] = t

    async def _run():
        fetch = asyncio.create_task(relay._on_fetch(s, _fetch(
            fetch_type=RELATIVE_JOINING, namespace=None, track_name=None,
            joining_request_id=7, joining_start=1)))
        await asyncio.sleep(0.1)
        assert not fetch.done()
        _join(t, s, 7)
        await asyncio.wait_for(fetch, 2.0)
    asyncio.run(_run())
    assert s._errors == []
    served, _ = s._served[0]
    assert {o.group_id for o in served} == {1, 2}


def test_a_joining_fetch_whose_subscribe_never_attaches_is_refused(
        monkeypatch):
    monkeypatch.setattr(relay, "JOINING_WAIT_S", 0.2)
    key = ((b"live",), b"cam")
    s = _session()
    s._peer_requests = {7: (Subscribe, key)}
    relay._tracks[key] = _track([(0, 0)])
    asyncio.run(relay._on_fetch(s, _fetch(fetch_type=RELATIVE_JOINING,
                                          namespace=None, track_name=None,
                                          joining_request_id=7,
                                          joining_start=1)))
    assert s._errors[0][0] == int(
        RequestErrorCode.INVALID_JOINING_REQUEST_ID)


def test_joining_fetch_on_a_forward_0_subscription_is_refused():
    # §10.12.2: a Joining FETCH needs Forward State 1.
    s = _session()
    t = _track([(g, 0) for g in range(3)])
    relay._tracks[((b"live",), b"cam")] = t
    _join(t, s, 7, forward=False)
    asyncio.run(relay._on_fetch(s, _fetch(fetch_type=RELATIVE_JOINING,
                                          namespace=None, track_name=None,
                                          joining_request_id=7,
                                          joining_start=1)))
    assert s._errors[0][0] == int(RequestErrorCode.INVALID_RANGE)
    assert s._served == []


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
                    for i in range(3)], None
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
            for g in range(0, 6)], None
    monkeypatch.setattr(relay, "_fetch_upstream", _upstream)
    asyncio.run(relay._on_fetch(s, _fetch(start_group=0, start_object=0,
                                          end_group=5, end_object=0)))
    assert asked == [(0, 5)]                      # went upstream
    served, _ = s._served[0]
    assert [o.group_id for o in served] == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("draft,does_not_exist,invalid_range,invalid_joining", [
    (14, 0x4, 0x5, 0x7), (16, 0x10, 0x11, 0x32), (18, 0x10, 0x11, 0x32),
    (20, 0x10, 0x11, 0x0), (22, 0x10, 0x11, 0x0)])
def test_refusals_carry_the_drafts_wire_codes(draft, does_not_exist,
                                              invalid_range, invalid_joining):
    # d14 has its own FETCH_ERROR codes; d20 has no INVALID_JOINING_REQUEST_ID.
    s = _session(draft)
    asyncio.run(relay._on_fetch(s, _fetch(start_group=0, start_object=0)))
    t = _track()
    relay._tracks[((b"live",), b"cam")] = t
    _join(t, s, 7)
    asyncio.run(relay._on_fetch(s, _fetch(fetch_type=RELATIVE_JOINING,
                                          joining_request_id=7,
                                          joining_start=2)))
    asyncio.run(relay._on_fetch(s, _fetch(fetch_type=RELATIVE_JOINING,
                                          joining_request_id=99,
                                          joining_start=2)))
    assert [code for code, _ in s._errors] == [does_not_exist, invalid_range,
                                              invalid_joining]


def test_an_upstream_refusal_is_answered_in_the_subscribers_draft(
        monkeypatch):
    # A d14 publisher's INVALID_RANGE (0x5) reaches a d18 subscriber as
    # 0x11, with the publisher's reason.
    s = _session(18)
    refused = MOQTRequestError(error_code=0x5, reason="range gone")

    async def _upstream(ns, track_name, msg):
        return None, None, relay._upstream_refusal(refused, profile_for(14),
                                                   fetch=True)
    monkeypatch.setattr(relay, "_fetch_upstream", _upstream)
    asyncio.run(relay._on_fetch(s, _fetch(start_group=0, start_object=0,
                                          end_group=1, end_object=0)))
    assert s._errors == [(0x11, "range gone")]


def test_an_upstream_code_the_draft_lacks_becomes_internal_error():
    refused = MOQTRequestError(error_code=0x99, reason="")
    assert relay._upstream_refusal(refused, profile_for(18)) == (
        RequestErrorCode.INTERNAL_ERROR, "upstream error")


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


# -- end to end: a catalog served only by FETCH, through the relay --------

_E2E_PORT = 16640
_NS = "relay/catalogs"
_SIZES = {"cat-a": 120, "cat-b": 60}
_GROUP = 3


class _FetchOnly(PublishedTrack):
    """History and a Largest Location, nothing sent on a subscription."""

    async def _start_generating(self, session, trigger):
        return None


def _param_largest(msg):
    raw = (msg.parameters or {}).get(ParamType.LARGEST_OBJECT)
    if raw is None or isinstance(raw, tuple):
        return raw
    buf = Buffer(data=raw)
    return (buf.pull_vint(), buf.pull_vint())


@contextlib.asynccontextmanager
async def _catalog_relay(port, use_quic, draft):
    """A relay; a publisher announcing two fetch-only tracks; a
    subscriber session. Yields the subscriber."""
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            sessions = []
            for _ in range(2):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                sessions.append(s)
            pub, sub = sessions
            for name, size in _SIZES.items():
                track = _FetchOnly(pub, namespace=_NS, trackname=name,
                                   object_size=size, group_size=_GROUP)
                track.prefill(1)
                track.attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            yield sub
    finally:
        handle.close()


@requires_certs
@pytest.mark.parametrize("draft", [16, 18])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_fetch_only_track_is_served_through_the_relay(draft,
                                                              use_quic):
    # The G5 catalog: SUBSCRIBE with a joining FETCH right behind it; the
    # publisher sends nothing on the subscription, so the relay anchors
    # the FETCH on the upstream SUBSCRIBE_OK's Largest and fetches upstream.
    port = _E2E_PORT + 2 * (draft == 18) + (not use_quic)
    got = {}
    async with _catalog_relay(port, use_quic, draft) as rx:
        rx.on_fetch_object = lambda obj, _n, _ts, rid: got.setdefault(
            rid, []).append((obj.group_id, obj.object_id, len(obj.payload)))
        for name, size in _SIZES.items():
            msg = rx.subscribe(_NS, name, forward=1,
                               filter_type=FilterType.LATEST_OBJECT)
            ok = await rx.joining_fetch(msg.request_id, joining_start=0,
                                        wait_response=True)
            assert (ok.largest_group_id, ok.largest_object_id) == (0, _GROUP)
            for _ in range(100):
                if len(got.get(ok.request_id, [])) >= _GROUP:
                    break
                await asyncio.sleep(0.02)
            assert got[ok.request_id] == [(0, o, size) for o in range(_GROUP)]
            status = await rx.track_status(_NS, name, wait_response=True)
            assert _param_largest(status) == (0, _GROUP - 1)
        assert rx._close_err is None


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_fetch_cancelled_before_the_upstream_answers(
        draft, use_quic, monkeypatch, caplog):
    # The subscriber cancels while the relay waits on the publisher; the
    # late FETCH_OK / REQUEST_ERROR is dropped and the session carries on.
    gate, asked, finished, relayed = asyncio.Event(), [], [], []
    real_upstream, real_on_fetch = relay._fetch_upstream, relay._on_fetch

    async def _held_upstream(ns, track_name, msg):
        asked.append(msg.request_id)
        await gate.wait()
        return await real_upstream(ns, track_name, msg)

    async def _on_fetch(session, msg):
        relayed.append(session)
        try:
            await real_on_fetch(session, msg)
        finally:
            finished.append(msg.request_id)

    monkeypatch.setattr(relay, "_fetch_upstream", _held_upstream)
    monkeypatch.setattr(relay, "_on_fetch", _on_fetch)
    port = _E2E_PORT + 10 + 2 * (draft == 22) + (not use_quic)
    async with _catalog_relay(port, use_quic, draft) as rx:
        # cat-a is served upstream; "missing" is declined upstream.
        reqs = [rx.fetch(_NS, name, start_group=0, start_object=0,
                         end_group=1, end_object=0)
                for name in ("cat-a", "missing")]
        for _ in range(200):
            if len(asked) == len(reqs):
                break
            await asyncio.sleep(0.02)
        assert len(asked) == len(reqs)
        down = relayed[0]
        for m in reqs:
            sid = rx._bidi_streams[m.request_id]
            rx.stream_reset(sid, StreamResetCode.CANCELLED)
            rx.stream_stop_sending(sid, StreamResetCode.CANCELLED)
        for _ in range(200):
            if not any(m.request_id in down._bidi_streams for m in reqs):
                break
            await asyncio.sleep(0.02)
        gate.set()
        for _ in range(500):
            if len(finished) == len(reqs):
                break
            await asyncio.sleep(0.02)
        assert len(finished) == len(reqs)
        got = []
        rx.on_fetch_object = lambda obj, _n, _ts, rid: got.append(rid)
        ok = await asyncio.wait_for(rx.fetch(
            _NS, "cat-b", start_group=0, start_object=0, end_group=1,
            end_object=0, wait_response=True), 10.0)
        for _ in range(100):
            if got.count(ok.request_id) >= _GROUP:
                break
            await asyncio.sleep(0.02)
        assert got.count(ok.request_id) == _GROUP
        assert rx._close_err is None and down._close_err is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
