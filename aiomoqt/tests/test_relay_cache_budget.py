"""aiomoqt-relay cache budget: one byte budget across all tracks, held by
dropping the oldest group of the least recently used track."""
import asyncio
import contextlib
import logging
from types import SimpleNamespace

import pytest

from aiomoqt import delivery
from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for, request_error_code
from aiomoqt.messages.fetch import Fetch
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import FilterType, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_NS = (b"live",)
_SIZE = 10
STANDALONE, RELATIVE_JOINING = 0x1, 0x2


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(relay, "_budget", relay._CacheBudget())
    # Byte counts below are payload only.
    monkeypatch.setattr(delivery, "OBJECT_RECORD_BYTES", 0)


@pytest.fixture
def tables(monkeypatch):
    saved = dict(relay._tracks)
    relay._tracks.clear()
    monkeypatch.setattr(relay, "_track_live", lambda t: t.upstream is not None)
    # The fake session has no close future: its reaper would drop its
    # subscriptions at once.
    monkeypatch.setattr(relay, "_watch_session", lambda session, ns=None: None)
    yield
    relay._tracks.clear()
    relay._tracks.update(saved)


def _budget_of(monkeypatch, n):
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES", n)


def _track(name):
    t = relay._RelayedTrack((_NS, name.encode()))
    t.upstream = object()
    return t


def _write(t, gid, oids=(0, 1)):
    for oid in oids:
        t._remember(gid, 0, oid, b"x" * _SIZE, None, 128, None)


def _groups(t):
    return sorted({c[0] for c in t._cache})


def _subscribe(t, session=None, request_id=7):
    """A live subscriber, attached without the forward task."""
    session = SimpleNamespace() if session is None else session
    d = relay._Downstream(session, 1, request_id)
    d.joined_at = t.largest
    t.downstream.append(d)


def _session(draft=18):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._errors, s._served = [], []
    s.fetch_error = lambda request_id, error_code, reason: s._errors.append(
        request_error_code(error_code, s._profile, fetch=True))
    s.fetch_ok = lambda **kw: None

    async def _serve(request_id, objects, group_order=None, fin=True,
                     stream_priority=None):
        s._served.append(list(objects))
        return 0
    s.serve_fetch = _serve
    return s


def _fetch(name, **kw):
    kw.setdefault("fetch_type", STANDALONE)
    kw.setdefault("group_order", None)
    kw.setdefault("request_id", 5)
    kw.setdefault("namespace", _NS)
    kw.setdefault("track_name", name.encode())
    return Fetch(**kw)


def _served_groups(s):
    assert s._served, s._errors
    objs = s._served[-1]
    assert objs, "FETCH served no objects"
    return sorted({o.group_id for o in objs})


def _assert_accounted(*tracks):
    for t in tracks:
        assert t._cache_bytes == sum(len(c[3] or b"") for c in t._cache)
    assert relay._budget.bytes == sum(t._cache_bytes for t in tracks)
    assert set(relay._budget.lru) == {t for t in tracks if t._cache}


def test_the_least_recently_used_track_loses_its_oldest_group(monkeypatch):
    _budget_of(monkeypatch, 100)
    a, b, c = _track("a"), _track("b"), _track("c")
    for t in (a, b):
        _write(t, 0)
        _write(t, 1)
    _write(c, 0)
    assert relay._budget.bytes == 100 and relay._budget.evictions == 0
    _write(c, 1, (0,))
    assert (_groups(a), _groups(b), _groups(c)) == ([1], [0, 1], [0, 1])
    assert relay._budget.bytes == 90 and relay._budget.evictions == 1
    # a is still least recently used; a track with no subscriber can
    # lose its last group.
    _write(c, 1, (1, 2))
    assert (_groups(a), _groups(b)) == ([], [0, 1])
    assert relay._budget.bytes == 90 and relay._budget.evictions == 2
    _assert_accounted(a, b, c)


def test_a_fetch_from_the_cache_refreshes_recency(monkeypatch, tables):
    _budget_of(monkeypatch, 100)
    a, b, c = _track("a"), _track("b"), _track("c")
    for t in (a, b):
        _write(t, 0)
        _write(t, 1)
        relay._tracks[t.key] = t
    _write(c, 0)
    s = _session()
    asyncio.run(relay._on_fetch(s, _fetch("a", start_group=0,
                                          start_object=0)))
    assert _served_groups(s) == [0, 1]
    _write(c, 1, (0,))
    assert (_groups(a), _groups(b)) == ([0, 1], [1])
    _assert_accounted(a, b, c)


def test_a_subscribed_track_keeps_its_live_group(monkeypatch, caplog):
    _budget_of(monkeypatch, 30)
    a, b = _track("a"), _track("b")
    _subscribe(a)
    _write(a, 0)
    _write(b, 0)
    # a is least recently used, but its one group is live.
    assert (_groups(a), _groups(b)) == ([0], [])
    assert relay._budget.bytes == 20
    with caplog.at_level(logging.WARNING, logger=relay.logger.name):
        _write(a, 0, (2, 3, 4))
    assert relay._budget.bytes == 50 and _groups(a) == [0]
    overruns = [r for r in caplog.records if "budget" in r.getMessage()]
    assert len(overruns) == 1 and overruns[0].levelno == logging.WARNING
    # A newer group makes the old one evictable.
    _write(a, 1, (0,))
    assert _groups(a) == [1] and relay._budget.bytes == 10
    _assert_accounted(a, b)


def test_a_closed_subscriber_does_not_protect_the_live_group(monkeypatch):
    _budget_of(monkeypatch, 30)
    a, b = _track("a"), _track("b")
    closed = SimpleNamespace(done=lambda: True)
    _subscribe(a, SimpleNamespace(_moqt_session_closed=closed))
    _write(a, 0)
    _write(b, 0)
    assert (_groups(a), _groups(b)) == ([], [0])
    _assert_accounted(a, b)


def test_fetch_and_joining_fetch_serve_only_what_remains(monkeypatch, tables):
    _budget_of(monkeypatch, 40)
    a = _track("a")
    relay._tracks[a.key] = a
    for g in range(4):
        _write(a, g)
    assert _groups(a) == [2, 3] and relay._budget.evictions == 2
    s = _session()
    _subscribe(a, s)

    asyncio.run(relay._on_fetch(s, _fetch("a", start_group=2,
                                          start_object=0)))
    assert _served_groups(s) == [2, 3] and len(s._served[-1]) == 4
    # Starting in an evicted group, with no publisher to ask: refused,
    # never a short answer from the cache.
    for start in (0, 1):
        s._served.clear()
        asyncio.run(relay._on_fetch(s, _fetch("a", start_group=start,
                                              start_object=0)))
        assert s._served == []
        assert s._errors.pop() == int(RequestErrorCode.DOES_NOT_EXIST)

    joining = dict(fetch_type=RELATIVE_JOINING, namespace=None,
                   track_name=None, joining_request_id=7)
    asyncio.run(relay._on_fetch(s, _fetch("a", joining_start=1, **joining)))
    assert _served_groups(s) == [2, 3]
    s._served.clear()
    asyncio.run(relay._on_fetch(s, _fetch("a", joining_start=3, **joining)))
    assert s._served == []
    assert s._errors.pop() == int(RequestErrorCode.DOES_NOT_EXIST)


def test_the_total_is_the_sum_of_the_tracks(monkeypatch):
    _budget_of(monkeypatch, 150)
    a, b, c = _track("a"), _track("b"), _track("c")
    for g in range(3):
        for t in (a, b, c):
            _write(t, g, range(g + 1))
    _assert_accounted(a, b, c)
    assert relay._budget.evictions > 0

    # Backfill groups 3 and 4 into d below where its delivery began.
    d = _track("d")
    _write(d, 5)
    d.complete_from = d.upstream_from = (5, 0)
    up = [SimpleNamespace(group_id=g, subgroup_id=0, object_id=o,
                          payload=b"y" * _SIZE, extensions=None,
                          publisher_priority=128, status=None)
          for g in (3, 4) for o in (0, 1)]

    async def _fetch_upstream(ns, name, msg, timeout=None):
        return None, up, None
    monkeypatch.setattr(relay, "_fetch_sources", lambda ns, name: [object()])
    monkeypatch.setattr(relay, "_fetch_upstream", _fetch_upstream)
    msg = SimpleNamespace(filter_type=FilterType.ABSOLUTE_START,
                          start_group=3, start_object=0, end_group=None,
                          location_filter=None)
    asyncio.run(relay._backfill(d, msg, d.largest))
    assert _groups(d) == [3, 4, 5]
    _assert_accounted(a, b, c, d)

    # Track ends: PUBLISH_DONE through the drain, and close().
    a.queue.put_nowait((None,) * 6 + ("DONE", (0x2, "done", a.key)))
    asyncio.run(a._forward_loop())
    assert a._cache_bytes == 0
    _assert_accounted(a, b, c, d)
    c.close()
    _assert_accounted(a, b, c, d)
    assert set(relay._budget.lru) == {b, d}
    # A backfill landing after close() caches nothing.
    c.complete_from = c.upstream_from = (5, 0)
    asyncio.run(relay._backfill(c, msg, (5, 0)))
    assert not c._cache
    _assert_accounted(a, b, c, d)


# -- end to end ---------------------------------------------------------------

_PORT = 23400
_LIVE_NS = "cachebudget/live"
_NAMES = ("video", "audio")
_OBJ, _GROUP = 200, 10


@contextlib.asynccontextmanager
async def _relay(port, use_quic):
    for table in (relay._announced, relay._announce_requests, relay._tracks):
        table.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            yield connect
    finally:
        handle.close()
        for table in (relay._announced, relay._announce_requests,
                      relay._tracks):
            table.clear()


async def _until(cond, what, tries=250):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    pytest.fail(what)


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_two_live_tracks_share_the_budget(use_quic, monkeypatch):
    budget = 6 * _OBJ * _GROUP
    _budget_of(monkeypatch, budget)
    got = {n: [] for n in _NAMES}
    fetched = {}
    async with _relay(_PORT + (not use_quic), use_quic) as connect:
        pub = await connect()
        for name in _NAMES:
            PublishedTrack(pub, namespace=_LIVE_NS, trackname=name,
                           object_size=_OBJ, group_size=_GROUP,
                           rate=100).attach()
        await pub.publish_namespace(namespace=_LIVE_NS, wait_response=True)
        viewer = await connect()
        for name in _NAMES:
            await SubscribedTrack(
                viewer, _LIVE_NS, name,
                on_object=lambda m, s, t, g, sg, n=name:
                    got[n].append(g)).subscribe(timeout=5)
        ns = relay._ns_tuple(_LIVE_NS)
        tracks = {}

        def _evicting():
            tracks.update({n: relay._tracks.get((ns, n.encode()))
                           for n in _NAMES})
            assert relay._budget.bytes <= budget
            return (relay._budget.evictions >= 6
                    and all(t is not None and t._cache
                            for t in tracks.values()))
        await _until(_evicting, "no evictions across both tracks")
        assert relay._budget.bytes == sum(t._cache_bytes
                                          for t in tracks.values())

        rx = await connect()
        rx.on_fetch_object = lambda obj, _n, _ts, rid: fetched.setdefault(
            rid, []).append(obj.group_id)
        for name, t in tracks.items():
            # The newest group: live, so the budget keeps it.
            live = t._cache[-1][0]
            before = t._fetch_bytes["cache"]
            ok = await asyncio.wait_for(rx.fetch(
                _LIVE_NS, name, start_group=live, start_object=0,
                end_group=live, end_object=0, wait_response=True), 5)
            await rx.await_fetch_done(ok.request_id, timeout=5)
            assert set(fetched.get(ok.request_id, [])) == {live}, name
            assert t._fetch_bytes["cache"] > before
        assert relay._budget.bytes <= budget
    for name in _NAMES:
        assert len(got[name]) > _GROUP, (name, len(got[name]))
