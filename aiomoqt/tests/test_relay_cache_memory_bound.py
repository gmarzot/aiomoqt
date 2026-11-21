"""aiomoqt-relay cache bounded by memory, not by a group count: tracks of
very different group durations each keep a recent window for FETCH,
the budget takes first from tracks above their floor, and a live
catalog keeps its newest group."""
import asyncio
import contextlib
import random
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import ObjectStatus

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 18950
_NS = (b"live",)


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(relay, "_budget", relay._CacheBudget())
    monkeypatch.setattr(relay, "_lag_limit", 5.0)


def _track(name, upstream=None):
    t = relay._RelayedTrack((_NS, name.encode()))
    t.upstream = upstream
    return t


def _audio(t, groups, t0=0.0, step=0.02, size=150):
    """One object per group, `step` seconds apart."""
    for g in range(groups):
        t._remember(g, 0, 0, b"a" * size, None, 128, None, born=t0 + g * step)


def _video(t, groups, t0=0.0, group_s=2.0, per_group=10, size=1000):
    for g in range(groups):
        for o in range(per_group):
            t._remember(g, 0, o, b"v" * size, None, 128, None,
                        born=t0 + g * group_s + o * group_s / per_group)


def _groups(t):
    return list(t._cache.groups())


def _live_session(closed=False):
    return SimpleNamespace(
        _moqt_session_closed=SimpleNamespace(done=lambda: closed))


def test_audio_keeps_far_more_than_eight_groups():
    t = _track("audio")
    _audio(t, 1500)
    assert _groups(t) == list(range(1500))
    got = t.cached_range((0, 0), (1499, 0))
    assert [(o[0], o[2]) for o in got] == [(g, 0) for g in range(1500)]
    assert t.cache_span_s() == pytest.approx(29.98, abs=0.05)


def test_the_per_track_bound_is_bytes_only(monkeypatch):
    cost = relay.retained_object_bytes(b"a" * 150, None)
    monkeypatch.setattr(relay, "CACHE_BYTES", 100 * cost)
    t = _track("audio")
    _audio(t, 1000)
    assert _groups(t) == list(range(900, 1000))
    assert t._cache_bytes == 100 * cost == relay._budget.bytes


def _check_index(t, held, rnd):
    """The cache's index answers as a full scan of `held`, group ID ->
    [(subgroup, object, status)] in group order."""
    order = [(g, h[1]) for g in sorted(held) for h in held[g]]
    locs = sorted(order)
    cache = t._cache
    assert [(c[0], c[2]) for c in cache] == order
    assert [(c[0], c[2]) for c in reversed(cache)] == order[::-1]
    assert len(cache) == len(order) and bool(cache) == bool(order)
    assert list(cache.groups()) == sorted(held)
    if not order:
        return
    assert (cache[0][0], cache[0][2]) == order[0]
    assert (cache[-1][0], cache[-1][2]) == order[-1]
    for g in sorted(held):
        assert [c[2] for c in cache.group(g)] == [h[1] for h in held[g]]
        assert cache.ids_below(g, 3) == [x for x in sorted(held) if x < g][-3:]
    for _ in range(100):
        a, b = sorted(rnd.choice(locs) for _ in range(2))
        want = [loc for loc in locs if a <= loc <= b]
        assert [(o[0], o[2]) for o in t.cached_range(a, b)] == want
        assert [(o[0], o[2]) for o in t.cached_range(a, None)] == [
            loc for loc in locs if loc >= a]
        g = a[0]
        assert t.group_end(g, (g, 0)) == (g, max(h[1] for h in held[g]))
        end = (b[0], b[1] + 1)
        assert t.holds_through(end) == any(
            (g, h[1]) >= end or (g == end[0] and h[2] is not None)
            for g, hs in held.items() for h in hs)


@pytest.mark.parametrize("block", [7, 512])
def test_the_index_matches_a_full_scan(monkeypatch, block):
    monkeypatch.setattr(relay, "CACHE_BLOCK_OBJECTS", block)
    rnd = random.Random(7)
    t = _track("mixed")
    held = {}

    def put(gid, sgid, oid):
        status = ObjectStatus.END_OF_GROUP if oid == 11 else None
        t._remember(gid, sgid, oid, b"x", None, 128, status)
        held.setdefault(gid, []).append((sgid, oid, status))

    late = []
    for g in range(60):
        objs = [(g, s, o) for s in range(3) for o in range(s, 12, 3)]
        rnd.shuffle(objs)
        for obj in objs:
            r = rnd.random()
            if r < 0.1:
                continue
            if r < 0.2:
                # Arrives after newer groups' objects.
                late.append(obj)
                continue
            put(*obj)
        if late and rnd.random() < 0.3:
            put(*late.pop(rnd.randrange(len(late))))
    for obj in late:
        put(*obj)
    _check_index(t, held, rnd)

    t._drop_oldest_group()
    del held[0]
    _check_index(t, held, rnd)

    for _ in range(25):
        gid, oid, _ = t._cache.popleft()
        g = min(held)
        assert (gid, oid) == (g, held[g][0][1])
        del held[g][0]
        if not held[g]:
            del held[g]
    _check_index(t, held, rnd)

    for g in (sorted(held)[len(held) // 2], max(held)):
        assert [c[1] for c in t._cache.pop_group(g)] == [
            h[1] for h in held.pop(g)]
    _check_index(t, held, rnd)

    g = sorted(held)[2]
    last = (g, sorted(h[1] for h in held[g])[len(held[g]) // 2])
    dropped = sorted(c[:2] for c in t._cache.drop_through(last))
    assert dropped == sorted((x, h[1]) for x, hs in held.items()
                             for h in hs if (x, h[1]) <= last)
    for x in list(held):
        held[x] = [h for h in held[x] if (x, h[1]) > last]
        if not held[x]:
            del held[x]
    _check_index(t, held, rnd)

    # A backfill of older groups and of a held group's missing objects.
    head = min(held)
    missing = [o for o in range(12) if o not in {h[1] for h in held[head]}]
    new = [(gid, 0, o) for gid in (head - 3, head - 1) for o in (2, 0, 1)]
    new += [(head, o % 3, o) for o in missing]
    rnd.shuffle(new)
    t._cache.merge([(gid, sgid, oid, b"x", None, 128, None, None, 0.0)
                    for gid, sgid, oid in new])
    for gid, sgid, oid in new:
        held.setdefault(gid, []).append((sgid, oid, None))
    for gid in {n[0] for n in new}:
        held[gid].sort(key=lambda h: h[1])
    _check_index(t, held, rnd)

    # A straggler of a dropped group starts that group again.
    put(0, 1, 4)
    _check_index(t, held, rnd)
    while t._cache:
        t._cache.popleft()
    _check_index(t, {}, rnd)
    assert len(t._cache._cols) == 1 and not t._cache._cols[0]


def test_the_budget_takes_first_from_tracks_above_their_floor(monkeypatch):
    video, audio = _track("video"), _track("audio")
    # Video is the least recently used, but holds only 8 s.
    _video(video, 4)
    _audio(audio, 1500)
    assert video.cache_span_s() < relay.CACHE_FLOOR_S < audio.cache_span_s()
    assert list(relay._budget.lru) == [video, audio]
    audio_cost = relay.retained_object_bytes(b"a" * 150, None)
    budget = relay._budget.bytes - 500 * audio_cost
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES", budget)
    relay._budget.enforce()
    assert _groups(video) == [0, 1, 2, 3]
    assert _groups(audio)[0] == 500
    assert audio.cache_span_s() >= relay.CACHE_FLOOR_S

    # Audio down to its floor: least recently used first again.
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES",
                        budget - 480 * audio_cost - 13_000)
    relay._budget.enforce()
    assert _groups(video)[0] > 0 and _groups(video)[-1] == 3
    assert audio.cache_span_s() >= relay.CACHE_FLOOR_S
    assert not audio._above_floor()
    assert relay._budget.bytes <= relay.CACHE_BUDGET_BYTES


def test_the_floor_is_at_least_the_lag_limit_plus_a_group(monkeypatch):
    t = _track("video")
    _video(t, 9)                 # 16.x s held, 2 s groups
    assert t._above_floor()
    monkeypatch.setattr(relay, "_lag_limit", 15.0)
    assert not t._above_floor()


def test_a_live_catalog_keeps_its_newest_group(monkeypatch):
    up = _live_session()
    catalog = _track("catalog", upstream=up)
    other = _track("other", upstream=_live_session())
    catalog._remember(0, 0, 0, b"c" * 4000, None, 128, None, born=0.0)
    catalog._remember(1, 0, 0, b"c" * 4000, None, 128, None, born=1.0)
    other._remember(0, 0, 0, b"o" * 4000, None, 128, None, born=2.0)
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES", 0)
    relay._budget.enforce()
    assert _groups(catalog) == [1]
    assert _groups(other) == []
    assert not catalog._evictable()

    up._moqt_session_closed = SimpleNamespace(done=lambda: True)
    assert catalog._evictable()
    relay._budget.enforce()
    assert _groups(catalog) == []


def test_status_tick_state_is_swept_with_flat_cost(monkeypatch):
    cost = relay.retained_object_bytes(b"a" * 150, None)
    monkeypatch.setattr(relay, "CACHE_BYTES", 50 * cost)
    t = _track("audio")
    for g in range(2000):
        t._ended.add((g, 0))
        t._remember(g, 0, 0, b"a" * 150, None, 128, None, born=g * 0.02)
    held = len(_groups(t))
    assert held == 50
    assert min(k[0] for k in t._ended) >= 2000 - 2 * held - 1
    assert len(t._ended) <= 2 * held + 1


@contextlib.asynccontextmanager
async def _relay(port, use_quic):
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


def _clear():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._catalogs):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture
def relay_tables():
    _clear()
    yield
    _clear()


async def _until(cond, what, tries=400):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    pytest.fail(what)


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_both_tracks_fetch_past_eight_groups_from_the_cache(
        use_quic, relay_tables):
    ns_str = "memcache/live"
    shapes = {"audio": dict(object_size=150, group_size=1, rate=200),
              "video": dict(object_size=1500, group_size=20, rate=200)}
    want_groups = {"audio": 40, "video": 10}
    got = {}
    async with _relay(_PORT + (not use_quic), use_quic) as connect:
        pub = await connect()
        for name, shape in shapes.items():
            PublishedTrack(pub, namespace=ns_str, trackname=name,
                           **shape).attach()
        await pub.publish_namespace(namespace=ns_str, wait_response=True)
        viewer, rx = await connect(), await connect()
        for name in shapes:
            await SubscribedTrack(viewer, ns_str, name).subscribe(timeout=5)
        ns = relay._ns_tuple(ns_str)
        live = {}

        def _cached():
            for name in shapes:
                live[name] = relay._tracks.get((ns, name.encode()))
            return all(t is not None
                       and len(t._cache.groups()) > want_groups[n] + 1
                       for n, t in live.items())
        await _until(_cached, "tracks not cached past eight groups")

        rx.on_fetch_object = lambda obj, _n, _ts, rid: got.setdefault(
            rid, []).append((obj.group_id, obj.object_id))
        for name, t in live.items():
            g0 = t._cache.groups()[0]
            last = g0 + want_groups[name] - 1
            per_group = shapes[name]["group_size"]
            ok = await asyncio.wait_for(rx.fetch(
                ns_str, name, start_group=g0, start_object=0,
                end_group=last, end_object=per_group, wait_response=True), 5)
            await rx.await_fetch_done(ok.request_id, timeout=5)
            served = got.get(ok.request_id, [])
            assert served == [(g, o) for g in range(g0, last + 1)
                              for o in range(per_group)], name
            assert t._fetch_bytes["upstream"] == 0, name
            assert t._fetch_bytes["cache"] == (
                len(served) * shapes[name]["object_size"]), name
        assert rx._close_err is None
