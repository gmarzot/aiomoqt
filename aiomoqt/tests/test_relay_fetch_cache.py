"""Relay status: FETCH payload bytes served from the cache and from
upstream, per track and relay-wide."""
import asyncio
import contextlib
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages.fetch import Fetch
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import FilterType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 17800
_NS = "fetchcache/live"
_LIVE, _HIST, _UNKNOWN = "live", "hist", "unknown"
_GROUP = 3


class _FetchOnly(PublishedTrack):
    """History and a Largest Location, nothing sent on a subscription."""

    async def _start_generating(self, session, trigger):
        return None


def _clear():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._catalogs):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()
    for k in relay._fetch_bytes:
        relay._fetch_bytes[k] = 0


@pytest.fixture
def relay_tables():
    _clear()
    yield
    _clear()


def _snapshot():
    status = relay._Status("/nonexistent/status.json", {"started": time.time()})
    return status.snapshot(asyncio.get_running_loop().time(), time.time())


def test_a_cancelled_fetch_counts_only_what_it_served(relay_tables):
    # The object being written when the FETCH is cancelled is not counted.
    t = relay._RelayedTrack(((b"live",), b"cam"))
    t.upstream = object()
    for oid in range(4):
        t._remember(0, 0, oid, b"x" * 10, None, 128, None)
    relay._tracks[((b"live",), b"cam")] = t
    s = type("S", (), {})()
    s.fetch_ok = lambda **kw: None

    async def _serve(request_id, objects, group_order=None, fin=True):
        it = iter(objects)
        next(it)
        next(it)
        return 0
    s.serve_fetch = _serve
    live = relay._track_live
    relay._track_live = lambda tr: tr.upstream is not None
    try:
        asyncio.run(relay._on_fetch(s, Fetch(
            fetch_type=1, group_order=None, request_id=5,
            namespace=(b"live",), track_name=b"cam",
            start_group=0, start_object=0)))
    finally:
        relay._track_live = live
    assert t._fetch_bytes == {"cache": 10, "upstream": 0}
    assert relay._fetch_bytes == {"cache": 10, "upstream": 0}


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


async def _until(cond, what, tries=150):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    pytest.fail(what)


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_fetch_bytes_by_source_in_status(use_quic, relay_tables):
    port = _PORT + (not use_quic)
    got = {}
    async with _relay(port, use_quic) as connect:
        pub = await connect()
        PublishedTrack(pub, namespace=_NS, trackname=_LIVE, object_size=100,
                       group_size=5, rate=200).attach()
        for name, size in ((_HIST, 60), (_UNKNOWN, 40)):
            track = _FetchOnly(pub, namespace=_NS, trackname=name,
                               object_size=size, group_size=_GROUP)
            track.prefill(1)
            track.attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        viewer, rx = await connect(), await connect()

        fresh = _snapshot()["totals"]
        assert (fresh["fetch_cache_bytes"], fresh["fetch_upstream_bytes"],
                fresh["cache_efficiency"]) == (0, 0, None)

        await SubscribedTrack(viewer, _NS, _LIVE).subscribe(timeout=5)
        ns = relay._ns_tuple(_NS)
        live = None

        def _cached():
            nonlocal live
            live = relay._tracks.get((ns, _LIVE.encode()))
            return live is not None and len({c[0] for c in live._cache}) >= 3
        await _until(_cached, "live track not cached")

        rx.on_fetch_object = lambda obj, _n, _ts, rid: got.setdefault(
            rid, []).append(len(obj.payload or b""))

        async def _served(rid):
            await rx.await_fetch_done(rid, timeout=5)
            return sum(got.get(rid, []))

        # Inside the cache: served from it.
        g0, _, o0 = live._cache[0][:3]
        ok = await asyncio.wait_for(rx.fetch(
            _NS, _LIVE, start_group=g0, start_object=o0,
            end_group=g0 + 1, end_object=0, wait_response=True), 5)
        cache_bytes = await _served(ok.request_id)

        # Nothing forwarded: the joining FETCH goes upstream.
        msg = rx.subscribe(_NS, _HIST, forward=1,
                           filter_type=FilterType.LATEST_OBJECT)
        ok = await rx.joining_fetch(msg.request_id, joining_start=0,
                                    wait_response=True)
        hist_bytes = await _served(ok.request_id)

        # No track state: upstream, in the totals only.
        ok = await asyncio.wait_for(rx.fetch(
            _NS, _UNKNOWN, start_group=0, start_object=0,
            end_group=0, end_object=_GROUP, wait_response=True), 5)
        unknown_bytes = await _served(ok.request_id)
        await _until(lambda: relay._fetch_bytes["upstream"]
                     == hist_bytes + unknown_bytes, "upstream bytes")
        snap = _snapshot()

    assert cache_bytes > 0
    assert hist_bytes == _GROUP * 60 and unknown_bytes == _GROUP * 40
    tracks = {r["name"]: r for r in snap["tracks"]}
    assert _UNKNOWN not in tracks
    assert (tracks[_LIVE]["fetch_cache_bytes"],
            tracks[_LIVE]["fetch_upstream_bytes"]) == (cache_bytes, 0)
    assert (tracks[_HIST]["fetch_cache_bytes"],
            tracks[_HIST]["fetch_upstream_bytes"]) == (0, hist_bytes)
    totals = snap["totals"]
    assert totals["fetch_cache_bytes"] == sum(
        r["fetch_cache_bytes"] for r in tracks.values()) == cache_bytes
    assert totals["fetch_upstream_bytes"] == sum(
        r["fetch_upstream_bytes"] for r in tracks.values()) + unknown_bytes
    total = cache_bytes + hist_bytes + unknown_bytes
    assert totals["cache_efficiency"] == round(cache_bytes / total, 4)
