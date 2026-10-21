"""Relay subscriptions under a d20/d22 Location Filter (d22 §3.3.1): the
inclusive range it admits, a REQUEST_UPDATE that changes it, and the
subscription outliving its End."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.params import LocationFilter
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import FilterType, ParamType, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 18700
_NS = "relay/lf"
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case, draft, use_quic):
    return _PORT + 8 * case + 2 * (draft // 2 % 4) + (not use_quic)


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft):
    """A relay; yields connect(), which opens a session to it."""
    _reset()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            yield connect
    finally:
        handle.close()
        _reset()


async def _write(session, alias, name, layout):
    """One stream per (group, subgroup, object ids) under `alias`; each
    payload names the track and its Location."""
    for gid, sgid, oids in layout:
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(track_alias=alias, group_id=gid,
                             subgroup_id=sgid, publisher_priority=128,
                             extensions_present=True, prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        for oid in oids:
            await session.stream_write_drain(sid, hdr.next_object(
                payload=f"{name}-{gid}-{oid}".encode(), extensions=None,
                object_id=oid).data)
        session.stream_fin(sid)


class _Layout(PublishedTrack):
    """Writes `layout` once, at its first subscription."""

    layout = ((0, 0, (0, 1)), (0, 1, (2,)), (1, 0, (0,)))
    written = False

    async def generate(self, session, track_alias):
        if not self.written:
            self.written = True
            await _write(session, track_alias, self.trackname, self.layout)


def _relayed(name):
    return relay._tracks.get(((b"relay", b"lf"), name.encode()))


async def _until(cond, tries=150):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def _subscribe(rx, filters, got):
    """Subscribe to each track under its filter; payload-bearing objects
    land in got[name] as Locations."""
    for name, lf in filters.items():
        await rx.subscribe(
            _NS, name, forward=1, location_filter=lf, wait_response=True,
            on_object=lambda m, _s, _t, g, _sg, n=name: got[n].append(
                (g, m.object_id)) if m.payload else None)


@pytest.mark.parametrize("draft", [20, 22])
@_TRANSPORTS
async def test_a_range_ending_mid_group_admits_only_its_objects(draft,
                                                                use_quic):
    # §3.3.1: the range is inclusive and nothing outside it is sent; the
    # end is a Location, so object 2 of the End Group stays out though it
    # rides another subgroup (§8.2).
    filters = {"video": LocationFilter.absolute(0, 0, 0, 1),
               "audio": LocationFilter.absolute(0, 1, 0, 2)}
    want = {"video": [(0, 0), (0, 1)], "audio": [(0, 1), (0, 2)]}
    async with _relay(_port(0, draft, use_quic), use_quic, draft) as connect:
        pub = await connect()
        for name in filters:
            _Layout(pub, namespace=_NS, trackname=name).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        rx = await connect()
        got = {name: [] for name in filters}
        await _subscribe(rx, filters, got)
        assert await _until(lambda: all(len(got[n]) >= len(want[n])
                                        for n in want)), got
        # Group 1 follows both ranges.
        await asyncio.sleep(0.3)
        assert {n: sorted(v) for n, v in got.items()} == want
        assert rx._close_err is None


@pytest.mark.parametrize("draft", [20, 22])
@_TRANSPORTS
async def test_a_start_before_largest_replays_only_the_range(draft,
                                                            use_quic):
    # A range or a relative start at or before Largest Object is served
    # from the cache, and only inside the filter.
    filters = {"video": LocationFilter.absolute(0, 0, 0, 1),
               "audio": LocationFilter.relative(1)}
    want = {"video": [(0, 0), (0, 1)], "audio": [(0, 0), (0, 1), (0, 2)]}
    async with _relay(_port(1, draft, use_quic), use_quic, draft) as connect:
        pub = await connect()
        for name in filters:
            track = _Layout(pub, namespace=_NS, trackname=name)
            track.layout = ((0, 0, (0, 1)), (0, 1, (2,)))
            await track.publish(publish_track=True, forward=1)
        assert await _until(lambda: all(
            _relayed(n) is not None and _relayed(n).largest == (0, 2)
            for n in filters))
        rx = await connect()
        got = {name: [] for name in filters}
        await _subscribe(rx, filters, got)
        await _until(lambda: all(len(got[n]) >= len(want[n]) for n in want))
        await asyncio.sleep(0.2)
        assert {n: sorted(v) for n, v in got.items()} == want
        assert rx._close_err is None


async def _flowing(connect, names, rate=100, filters=None):
    """A publisher producing `names` (5-object groups at `rate` objects/s)
    and a subscriber session, subscribed under filters[name] (subscribe()
    keyword arguments; none = unfiltered). Returns (rx, subscriptions,
    got)."""
    pub = await connect()
    for name in names:
        PublishedTrack(pub, namespace=_NS, trackname=name, object_size=100,
                       group_size=5, rate=rate).attach()
    await pub.publish_namespace(namespace=_NS, wait_response=True)
    rx = await connect()
    got = {name: [] for name in names}
    subs = {}
    for name in names:
        kw = (filters or {}).get(name, {"filter_type": None})
        subs[name] = await rx.subscribe(
            _NS, name, forward=1, wait_response=True, **kw,
            on_object=lambda m, _s, _t, g, _sg, n=name: got[n].append(
                (g, m.object_id)) if m.payload else None)
    assert await _until(lambda: all(got[n] for n in names)), got
    return rx, subs, got


@pytest.mark.parametrize("draft", [20, 22])
@_TRANSPORTS
async def test_a_request_update_replaces_the_filter(draft, use_quic, caplog):
    # §9.5.1: a REQUEST_UPDATE's LOCATION_FILTER replaces the window; each
    # track keeps its own.
    spans = {"video": ((0, 1), (0, 3)), "audio": ((0, 3), (1, 1))}
    async with _relay(_port(2, draft, use_quic), use_quic, draft) as connect:
        rx, subs, got = await _flowing(connect, spans)
        want, ends = {}, {}
        for name, ((g0, o0), (g1, o1)) in spans.items():
            base = _relayed(name).largest[0] + 3
            start, end = (base + g0, o0), (base + g1, o1)
            await rx.request_update(
                subs[name].request_id, wait_response=True,
                parameters={ParamType.LOCATION_FILTER: LocationFilter.absolute(
                    *start, *end)})
            want[name] = [loc for loc in ((g, o) for g in range(start[0],
                                                                end[0] + 1)
                                          for o in range(5))
                          if start <= loc <= end]
            ends[name] = (start, end)
        assert await _until(lambda: all(
            _relayed(n).largest >= (ends[n][1][0] + 1, 1) for n in spans))
        await asyncio.sleep(0.1)
        for name, (start, _end) in ends.items():
            assert sorted(loc for loc in got[name] if loc >= start) == (
                want[name]), name
        assert rx._close_err is None
    assert not [r for r in caplog.records if "handler failed" in r.message]


@pytest.mark.parametrize("draft", [18, 20, 22])
@_TRANSPORTS
async def test_a_range_behind_production_keeps_its_subscription(draft,
                                                               use_quic):
    # d22 §3.3.1: a subscription does not end because Largest Object
    # passed its filter; its streams close and it stays open. Up to d18 it
    # ends with PUBLISH_DONE SUBSCRIPTION_ENDED.
    ends = {"video": 2, "audio": 3}
    if draft >= 20:
        filters = {n: {"location_filter": LocationFilter.absolute(0, 0, e)}
                   for n, e in ends.items()}
    else:
        filters = {n: {"filter_type": FilterType.ABSOLUTE_RANGE,
                       "start_group": 0, "start_object": 0, "end_group": e}
                   for n, e in ends.items()}
    async with _relay(_port(3, draft, use_quic), use_quic, draft) as connect:
        rx, subs, got = await _flowing(connect, ends, filters=filters)
        done = {}
        for name, ok in subs.items():
            done[name] = asyncio.get_running_loop().create_future()
            rx.register_publish_done_handler(ok.request_id,
                                             done[name].set_result)
        assert await _until(lambda: all(
            _relayed(n).largest >= (e + 2, 0) for n, e in ends.items()))
        await asyncio.sleep(0.1)
        for name, end in ends.items():
            assert {g for g, _ in got[name]} == set(range(end + 1)), name
        if draft < 20:
            for name in ends:
                status = await asyncio.wait_for(done[name], 5.0)
                assert status.status_code == (
                    SubscribeDoneCode.SUBSCRIPTION_ENDED)
            return
        assert not any(f.done() for f in done.values())
        assert not any(_relayed(n)._streams for n in ends)
        # Still subscribed: a wider filter brings objects again.
        starts = {}
        for name in ends:
            starts[name] = _relayed(name).largest[0] + 2
            await rx.request_update(
                subs[name].request_id, wait_response=True,
                parameters={ParamType.LOCATION_FILTER: LocationFilter.absolute(
                    starts[name], 0)})
        assert await _until(lambda: all(
            any(g >= starts[n] for g, _ in got[n]) for n in ends)), got
        assert not any(f.done() for f in done.values())
        assert rx._close_err is None
