"""Relay Range Filters (d22 §3.3.2, d20 §5.1.4): SUBGROUP, OBJECTID,
PRIORITY and OBJECT_PROPERTY filters per subscriber on live forwarding,
cache replay, fill streams and FETCH; REQUEST_UPDATE replacing or
removing them; TRACK_PROPERTY_FILTER on SUBSCRIBE_TRACKS; the
MAX_FILTER_RANGES bound. Two tracks per session."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.params import (D20ParamType as P, D20RequestErrorCode,
                                     LocationFilter, RangeFilter,
                                     RangeFilters, merge_range_filters)
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTRequestError, ParamType, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 21100
_NS = "relay/rf"
_NAMES = ("video", "audio")
_PROP = 0x40                       # Object Property: object id % 3
_TRACK_PROP = 0x3800
# Per group: (subgroup, publisher priority, object ids).
_SUBGROUPS = ((0, 10, (0, 2, 4)), (1, 200, (1, 3, 5)))
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_FROM_START = LocationFilter.absolute(0, 0, 100)


def _rf(*ranges, set_id=0, prop=None):
    return RangeFilter(set_id=set_id, ranges=tuple(ranges),
                       property_type=prop)


# -- matching (no transport) ----------------------------------------------

def _admits(params, sgid, oid, prio, props=None):
    return RangeFilters.from_params(params).admits(sgid, oid, prio, props)


def test_ranges_or_filters_and_set_ids_or():
    # §3.3.2: Ranges OR'd within a filter; filters of one SetID AND'd;
    # SetIDs OR'd.
    params = {P.SUBGROUP_FILTER: [_rf((0, 0))],
              P.OBJECTID_FILTER: [_rf((0, 2), (9, None)),
                                  _rf((5, 5), set_id=1)]}
    got = [o for o in range(12) if _admits(params, o % 2, o, 128)]
    assert got == [0, 2, 5, 10]
    assert _admits({}, 3, 3, 3)


def test_priority_and_object_property_filters():
    params = {P.PRIORITY_FILTER: [_rf((0, 100))],
              P.OBJECT_PROPERTY_FILTER: [_rf((1, 2), prop=_PROP)]}
    assert _admits(params, 0, 0, 10, {_PROP: 2})
    assert not _admits(params, 0, 0, 200, {_PROP: 2})
    assert not _admits(params, 0, 0, 10, {_PROP: 0})
    # A required property the object lacks fails the filter.
    assert not _admits(params, 0, 0, 10, None)
    assert not _admits(params, 0, 0, 10, {_PROP + 2: 1})


def test_track_property_filter_selects_tracks_per_set():
    # A set whose TRACK_PROPERTY_FILTER a track fails drops out for that
    # track; its object filters go with it.
    f = RangeFilters.from_params({
        P.TRACK_PROPERTY_FILTER: [_rf((1, 1), prop=_TRACK_PROP),
                                  _rf((2, 2), set_id=1, prop=_TRACK_PROP)],
        P.OBJECTID_FILTER: [_rf((0, 0))]})
    assert f.for_track({_TRACK_PROP: 3}) is None
    assert f.for_track({}) is None
    one = f.for_track({_TRACK_PROP: 1})
    assert [o for o in range(3) if one.admits(0, o, 0, None)] == [0]
    two = f.for_track({_TRACK_PROP: 2})
    assert not two and two.admits(0, 7, 0, None)


def test_merge_replaces_or_removes_by_parameter_type():
    # §3.3.2: zero Length removes, non-zero replaces, omitted is unchanged.
    cur = {P.SUBGROUP_FILTER: [_rf((0, 0))], P.OBJECTID_FILTER: [_rf((1, 1))]}
    upd = {P.SUBGROUP_FILTER: RangeFilter(set_id=None),
           P.PRIORITY_FILTER: [_rf((5, 5), set_id=3)]}
    assert merge_range_filters(cur, upd) == {
        P.OBJECTID_FILTER: [_rf((1, 1))],
        P.PRIORITY_FILTER: [_rf((5, 5), set_id=3)]}
    assert merge_range_filters({}, {ParamType.FORWARD: 1}) == {}


# -- through the relay ----------------------------------------------------

def _port(case, draft, use_quic):
    return _PORT + 4 * case + 2 * (draft == 22) + (not use_quic)


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._track_sub_filters.clear()
    relay._ns_subs.clear()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft=22):
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


async def _write(session, alias, name, groups):
    """Each group as _SUBGROUPS, one stream per subgroup; payloads name
    the track and Location."""
    for gid in groups:
        for sgid, prio, oids in _SUBGROUPS:
            sid = await session.open_uni_stream()
            hdr = SubgroupHeader(track_alias=alias, group_id=gid,
                                 subgroup_id=sgid, publisher_priority=prio,
                                 extensions_present=True,
                                 prof=session._profile)
            session.stream_write(sid, hdr.serialize().data)
            for oid in oids:
                await session.stream_write_drain(sid, hdr.next_object(
                    payload=f"{name}-{gid}-{oid}".encode(),
                    extensions={_PROP: oid % 3}, object_id=oid).data)
            session.stream_fin(sid)


class _Layered(PublishedTrack):
    """Writes groups 0 and 1 at its first subscription; more on write()."""

    lane = None

    async def generate(self, session, track_alias):
        if self.lane is None:
            self.lane = (session, track_alias)
            await _write(session, track_alias, self.trackname, (0, 1))

    async def write(self, *groups):
        await _write(*self.lane, self.trackname, groups)


async def _publisher(connect):
    pub = await connect()
    tracks = {n: _Layered(pub, namespace=_NS, trackname=n) for n in _NAMES}
    for t in tracks.values():
        t.attach()
    await pub.publish_namespace(namespace=_NS, wait_response=True)
    return tracks


def _relayed(name):
    return relay._tracks.get(((b"relay", b"rf"), name.encode()))


async def _until(cond, tries=250):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def _collect(got, name):
    return lambda m, _s, _t, g, _sg: got[name].append(
        (g, m.object_id)) if m.payload else None


async def _subscribe(rx, filters, got, lf=_FROM_START, **kw):
    oks = {}
    for name, params in filters.items():
        oks[name] = await rx.subscribe(
            _NS, name, forward=1, location_filter=lf, parameters=params,
            wait_response=True, on_object=_collect(got, name), **kw)
    return oks


def _locs(groups, oids):
    return sorted((g, o) for g in groups for o in oids)


async def _settled(got, want):
    """Every wanted object arrived, then nothing more for a while."""
    assert await _until(lambda: all(len(got[n]) >= len(want[n])
                                    for n in want)), got
    await asyncio.sleep(0.3)
    return {n: sorted(v) for n, v in got.items()} == want


_CASES = {
    "subgroup": ({"video": {P.SUBGROUP_FILTER: _rf((1, 1))},
                  "audio": {P.SUBGROUP_FILTER: _rf((0, 0))}},
                 {"video": (1, 3, 5), "audio": (0, 2, 4)}),
    "object-id": ({"video": {P.OBJECTID_FILTER: _rf((2, 3))},
                   "audio": {P.OBJECTID_FILTER: _rf((0, 0), (5, None))}},
                  {"video": (2, 3), "audio": (0, 5)}),
    "priority": ({"video": {P.PRIORITY_FILTER: _rf((0, 100))},
                  "audio": {P.PRIORITY_FILTER: _rf((150, None))}},
                 {"video": (0, 2, 4), "audio": (1, 3, 5)}),
    "object-property": (
        {"video": {P.OBJECT_PROPERTY_FILTER: _rf((0, 0), prop=_PROP)},
         "audio": {P.OBJECT_PROPERTY_FILTER: _rf((1, 2), prop=_PROP)}},
        {"video": (0, 3), "audio": (1, 2, 4, 5)}),
    "set-ids": (
        {"video": {P.SUBGROUP_FILTER: _rf((0, 0)),
                   P.OBJECTID_FILTER: [_rf((0, 2)), _rf((5, 5), set_id=1)]},
         "audio": {P.PRIORITY_FILTER: _rf((200, 200), set_id=7),
                   P.OBJECT_PROPERTY_FILTER: _rf((1, 1), set_id=7,
                                                 prop=_PROP)}},
        {"video": (0, 2, 5), "audio": (1,)}),
}


@requires_certs
@pytest.mark.parametrize("draft", [20, 22])
@pytest.mark.parametrize("case", list(_CASES))
@_TRANSPORTS
async def test_a_subscription_gets_only_matching_objects(case, draft,
                                                        use_quic):
    filters, oids = _CASES[case]
    want = {n: _locs((0, 1), oids[n]) for n in _NAMES}
    port = _port(list(_CASES).index(case), draft, use_quic)
    async with _relay(port, use_quic, draft) as connect:
        await _publisher(connect)
        rx = await connect()
        assert rx._peer_max_filter_ranges == relay.MAX_FILTER_RANGES
        got = {n: [] for n in _NAMES}
        await _subscribe(rx, filters, got)
        assert await _settled(got, want), got
        assert rx._close_err is None


class _FillRx:
    def __init__(self, session):
        self.session = session
        self.objects: dict = {}
        session.on_fetch_object = (
            lambda obj, _n, _ts, rid: self.objects.setdefault(rid, [])
            .append((obj.group_id, obj.object_id)))

    async def done(self, request_id):
        clean = await self.session.await_fetch_done(request_id, timeout=8)
        return clean, sorted(self.objects.get(request_id, []))


@requires_certs
@_TRANSPORTS
async def test_replay_live_fill_and_request_update(use_quic):
    # Cache replay and live objects take the subscription's filters; the
    # fill stream inherits them, with FILL_PARAMETERS overriding by type
    # (§3.4, §9.20.15); REQUEST_UPDATE replaces or removes them (§3.3.2).
    async with _relay(_port(5, 22, use_quic), use_quic) as connect:
        tracks = await _publisher(connect)
        prime = await connect()
        primed = {n: [] for n in _NAMES}
        await _subscribe(prime, {n: None for n in _NAMES}, primed)
        assert await _until(lambda: all(len(v) == 12 for v in
                                        primed.values()))
        # Replay from the cache, then live.
        rx = await connect()
        got = {n: [] for n in _NAMES}
        oks = await _subscribe(rx, {
            "video": {P.SUBGROUP_FILTER: _rf((1, 1))},
            "audio": {P.OBJECTID_FILTER: _rf((0, 1))}}, got)
        for t in tracks.values():
            await t.write(2)
        assert await _settled(got, {"video": _locs((0, 1, 2), (1, 3, 5)),
                                    "audio": _locs((0, 1, 2), (0, 1))})
        # Replace video's subgroup filter; remove audio's object filter.
        for name, params in (
                ("video", {P.SUBGROUP_FILTER: _rf((0, 0))}),
                ("audio", {P.OBJECTID_FILTER: RangeFilter(set_id=None)})):
            await rx.request_update(oks[name].request_id, parameters=params,
                                    wait_response=True)
        for name in _NAMES:
            got[name].clear()
        for t in tracks.values():
            await t.write(3)
        assert await _settled(got, {"video": _locs((3,), (0, 2, 4)),
                                    "audio": _locs((3,), range(6))})
        # Fill: video's OBJECTID overridden, its SUBGROUP inherited;
        # audio's SUBGROUP removed for the fill only.
        fx = _FillRx(await connect())
        fill = {ParamType.LOCATION_FILTER: LocationFilter.absolute(0, 0, 1)}
        sub = {"video": ({P.SUBGROUP_FILTER: _rf((0, 0)),
                          P.OBJECTID_FILTER: _rf((0, 1))},
                         {**fill, P.OBJECTID_FILTER: _rf((3, 4))}),
               "audio": ({P.SUBGROUP_FILTER: _rf((1, 1))},
                         {**fill, P.SUBGROUP_FILTER: RangeFilter(None)})}
        oks = {n: await fx.session.subscribe(
            _NS, n, location_filter=LocationFilter.next_object(),
            parameters={**s, P.FILL_PARAMETERS: f}, wait_response=True)
            for n, (s, f) in sub.items()}
        assert await fx.done(oks["video"].request_id) == (
            True, _locs((0, 1), (4,)))
        assert await fx.done(oks["audio"].request_id) == (
            True, _locs((0, 1), range(6)))
        assert rx._close_err is None and fx.session._close_err is None


@requires_certs
@_TRANSPORTS
async def test_fetch_takes_range_filters(use_quic):
    async with _relay(_port(6, 22, use_quic), use_quic) as connect:
        await _publisher(connect)
        prime = await connect()
        primed = {n: [] for n in _NAMES}
        await _subscribe(prime, {n: None for n in _NAMES}, primed)
        assert await _until(lambda: all(len(v) == 12 for v in
                                        primed.values()))
        fx = _FillRx(await connect())
        params = {"video": {P.OBJECTID_FILTER: _rf((1, 2))},
                  "audio": {P.PRIORITY_FILTER: _rf((0, 100)),
                            P.OBJECT_PROPERTY_FILTER: _rf((2, 2),
                                                          prop=_PROP)}}
        oks = {n: await fx.session.fetch(
            _NS, n, start_group=0, end_group=1, parameters=p,
            wait_response=True) for n, p in params.items()}
        assert await fx.done(oks["video"].request_id) == (
            True, _locs((0, 1), (1, 2)))
        assert await fx.done(oks["audio"].request_id) == (
            True, _locs((0, 1), (2,)))
        # Filtering leaves the End Location as requested (§3.2.2).
        assert (oks["video"].largest_group_id,
                oks["video"].largest_object_id) in ((1, 6), (1, 5))
        assert fx.session._close_err is None


class _Propertied(PublishedTrack):
    def __init__(self, *args, props=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.props = props or {}

    def _track_properties(self, session):
        return {**(super()._track_properties(session) or {}), **self.props}


@requires_certs
@pytest.mark.parametrize("draft", [20, 22])
@_TRANSPORTS
async def test_subscribe_tracks_offers_only_matching_tracks(draft,
                                                            use_quic):
    # §3.3.2, §3.6.1: only PUBLISHes passing the TRACK_PROPERTY_FILTER
    # are forwarded; the other Range Filters filter their objects.
    props = {"video": {_TRACK_PROP: 1}, "audio": {_TRACK_PROP: 1},
             "data": {_TRACK_PROP: 2}, "meta": {}}
    async with _relay(_port(7, draft, use_quic), use_quic, draft) as connect:
        pub = await connect()
        for name, p in props.items():
            await _Propertied(pub, namespace=_NS, trackname=name,
                              object_size=50, group_size=5, rate=100,
                              props=p).publish()
        assert await _until(lambda: all(_relayed(n) for n in props))
        rx = await connect()
        got = {n: [] for n in _NAMES}
        await rx.subscribe_tracks(namespace=_NS, parameters={
            P.TRACK_PROPERTY_FILTER: _rf((1, 1), prop=_TRACK_PROP),
            P.OBJECTID_FILTER: _rf((0, 1))})
        offered = []
        for _ in _NAMES:
            offer = await rx.await_publish(timeout=5.0)
            name = offer.track_name.decode()
            offered.append(name)
            rx._track_aliases[offer.track_alias] = offer.request_id
            rx.register_object_handler(offer.track_alias,
                                       _collect(got, name))
            rx.publish_ok(offer, forward=1)
        assert sorted(offered) == sorted(_NAMES)
        with pytest.raises(asyncio.TimeoutError):
            await rx.await_publish(timeout=0.5)
        assert await _until(lambda: all(len(v) >= 4 for v in got.values()))
        assert {o for v in got.values() for _g, o in v} == {0, 1}
        assert rx._close_err is None and pub._close_err is None


def _many(n, start=0):
    return RangeFilter(set_id=0, ranges=tuple(
        (start + 2 * i, start + 2 * i) for i in range(n)))


@requires_certs
@_TRANSPORTS
async def test_max_filter_ranges_bounds_a_request_and_its_updates(use_quic):
    # §9.1.6: past the relay's MAX_FILTER_RANGES a SUBSCRIBE gets
    # INVALID_FILTER; a REQUEST_UPDATE whose result exceeds it fails,
    # ending the subscription with UPDATE_FAILED (§9.5.1).
    limit = relay.MAX_FILTER_RANGES
    async with _relay(_port(8, 22, use_quic), use_quic) as connect:
        await _publisher(connect)
        rx = await connect()
        rx._peer_max_filter_ranges = limit + 1
        for name in _NAMES:
            with pytest.raises(MOQTRequestError) as err:
                await rx.subscribe(_NS, name, location_filter=_FROM_START,
                                   parameters={P.OBJECTID_FILTER: _many(
                                       limit + 1)}, wait_response=True)
            assert err.value.error_code == D20RequestErrorCode.INVALID_FILTER
        rx._peer_max_filter_ranges = limit
        done = {n: [] for n in _NAMES}
        oks = {n: await rx.subscribe(
            _NS, n, location_filter=_FROM_START,
            parameters={P.SUBGROUP_FILTER: _many(limit // 2)},
            on_done=done[n].append, wait_response=True) for n in _NAMES}
        # Replacing a type keeps the total; adding one exceeds it.
        await rx.request_update(oks["video"].request_id, parameters={
            P.SUBGROUP_FILTER: _many(limit // 2, start=1)},
            wait_response=True)
        with pytest.raises(MOQTRequestError) as err:
            await rx.request_update(oks["audio"].request_id, parameters={
                P.OBJECTID_FILTER: _many(limit // 2 + 1)},
                wait_response=True)
        assert err.value.error_code == D20RequestErrorCode.INVALID_FILTER
        assert await _until(lambda: done["audio"])
        assert done["audio"][0].status_code == SubscribeDoneCode.UPDATE_FAILED
        assert not done["video"]
        assert rx._close_err is None
