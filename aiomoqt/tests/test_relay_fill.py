"""Relay fill fetch streams (d22 §3.4): served from the cache or from an
upstream FETCH under the request's ID, opened by a REQUEST_UPDATE, never
while paused, and reset, or reported as a Timed-Out gap, when nothing
holds the range. Two tracks per session; payload sizes tell them apart."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.params import D20ParamType, LocationFilter
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import ParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 19900
_NS = "relay/fill"
_SIZES = {"video": 300, "audio": 100}
_GROUP = 5
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_FILL = D20ParamType.FILL_PARAMETERS


def _port(case, use_quic):
    return _PORT + 2 * case + (not use_quic)


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
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


class _Rx:
    """Fetch objects and End of Range markers by Request ID."""

    def __init__(self, session):
        self.session = session
        self.objects: dict = {}
        self.gaps: dict = {}
        session.on_fetch_object = (
            lambda obj, _n, _ts, rid: self.objects.setdefault(rid, [])
            .append(obj))
        session.on_fetch_gap = (
            lambda m, rid: self.gaps.setdefault(rid, []).append(
                (m.end_of_range, m.group_id, m.object_id)))

    async def done(self, request_id):
        clean = await self.session.await_fetch_done(request_id, timeout=8)
        return clean, [(o.group_id, o.object_id)
                       for o in self.objects.get(request_id, [])]

    def sizes(self, request_id):
        return {len(o.payload) for o in self.objects.get(request_id, [])}


def _relayed(name):
    return relay._tracks.get(((b"relay", b"fill"), name.encode()))


def _downstream(name, request_id):
    """The newest subscriber session's subscription `request_id`."""
    t = _relayed(name)
    return next(d for d in reversed(t.downstream)
                if d.request_id == request_id)


def _span(start, end):
    return [(g, o) for g in range(start[0], end[0] + 1) for o in range(_GROUP)
            if start <= (g, o) <= end]


async def _until(cond, tries=250):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def _publisher(connect, prefill=3):
    pub = await connect()
    for name, size in _SIZES.items():
        track = PublishedTrack(pub, namespace=_NS, trackname=name,
                               object_size=size, group_size=_GROUP, rate=100)
        if prefill:
            track.prefill(prefill)
        track.attach()
    await pub.publish_namespace(namespace=_NS, wait_response=True)
    return pub


def _sub(rx, name, fill, **kw):
    return rx.session.subscribe(
        _NS, name, location_filter=LocationFilter.next_object(),
        parameters={_FILL: fill}, wait_response=True, **kw)


@_TRANSPORTS
async def test_fill_from_upstream_then_from_the_cache(use_quic):
    # A fresh upstream holds nothing older than the live edge, so the fill
    # comes from the publisher's FETCH; a later subscriber's comes from
    # the cache. Each carries its SUBSCRIBE's Request ID.
    async with _relay(_port(0, use_quic), use_quic) as connect:
        await _publisher(connect)
        rx = _Rx(await connect())
        fills = {"video": LocationFilter.relative(2),
                 "audio": LocationFilter.absolute(0, 3, 1, 1)}
        oks = dict(zip(fills, await asyncio.gather(*(
            _sub(rx, n, {ParamType.LOCATION_FILTER: lf})
            for n, lf in fills.items()))))
        for name, ok in oks.items():
            largest = (ok.largest_group_id, ok.largest_object_id)
            start = ((largest[0] - 1, 0) if name == "video" else (0, 3))
            end = min(largest, (1, 1)) if name == "audio" else largest
            assert await rx.done(ok.request_id) == (True, _span(start, end))
            assert rx.sizes(ok.request_id) == {_SIZES[name]}
            assert relay._tracks[((b"relay", b"fill"), name.encode())] \
                ._fetch_bytes["upstream"] > 0
        # Warm cache: the current group from object 0.
        for name in _SIZES:
            t = _relayed(name)
            assert await _until(lambda t=t: t._cache and t.largest[0]
                                > t._cache[0][0] + 1 and t.largest[1] >= 1)
        rx2 = _Rx(await connect())
        oks = dict(zip(_SIZES, await asyncio.gather(*(
            _sub(rx2, n, {ParamType.LOCATION_FILTER: LocationFilter.relative(1)})
            for n in _SIZES))))
        for name, ok in oks.items():
            largest = (ok.largest_group_id, ok.largest_object_id)
            assert await rx2.done(ok.request_id) == (
                True, _span((largest[0], 0), largest))
            assert rx2.sizes(ok.request_id) == {_SIZES[name]}
            d = _downstream(name, ok.request_id)
            assert _relayed(name)._sent_streams[id(d.session)] >= 1
        assert rx.session._close_err is None
        assert rx2.session._close_err is None


@_TRANSPORTS
async def test_request_update_fill_and_no_fill_while_paused(use_quic):
    async with _relay(_port(1, use_quic), use_quic) as connect:
        await _publisher(connect)
        rx = _Rx(await connect())
        oks = {n: await rx.session.subscribe(
            _NS, n, location_filter=LocationFilter.next_object(),
            wait_response=True) for n in _SIZES}
        fill = {_FILL: {ParamType.LOCATION_FILTER: LocationFilter.absolute(
            1, 0, 1)}}
        updates = dict(zip(oks, await asyncio.gather(*(
            rx.session.request_update(ok.request_id, parameters=fill,
                                      wait_response=True)
            for ok in oks.values()))))
        for name, upd in updates.items():
            assert await rx.done(upd.request_id) == (
                True, _span((1, 0), (1, _GROUP - 1)))
            assert rx.sizes(upd.request_id) == {_SIZES[name]}
        # Paused: FILL_PARAMETERS opens no fill stream (§3.4.1).
        rx2 = _Rx(await connect())
        paused = {n: await _sub(rx2, n, {}, forward=0) for n in _SIZES}
        await asyncio.sleep(0.3)
        for name, ok in paused.items():
            d = _downstream(name, ok.request_id)
            assert not d.fills
            assert relay._tracks[((b"relay", b"fill"), name.encode())] \
                ._sent_streams.get(id(d.session), 0) == 0
        assert not rx2.objects
        assert rx.session._close_err is None


class _Late(PublishedTrack):
    """PUBLISHes objects 3 and 4 of group 2 only."""

    written = False

    async def generate(self, session, track_alias):
        if self.written:
            return
        self.written = True
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(track_alias=track_alias, group_id=2,
                             subgroup_id=0, publisher_priority=128,
                             extensions_present=True, prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        for oid in (3, 4):
            await session.stream_write_drain(sid, hdr.next_object(
                payload=b"x" * _SIZES[self.trackname], extensions=None,
                object_id=oid).data)


@_TRANSPORTS
async def test_unheld_fill_resets_or_reports_a_timed_out_gap(use_quic):
    # Nothing announced to FETCH from and the cache starts mid-group: the
    # fill is reset (§3.4.1); under FILL_TIMEOUT 0 the missing head is a
    # Timed-Out gap and the held objects follow (§9.20.5).
    async with _relay(_port(2, use_quic), use_quic) as connect:
        pub = await connect()
        for name in _SIZES:
            await _Late(pub, namespace=_NS, trackname=name).publish(
                publish_track=True, forward=1)
        assert await _until(lambda: all(
            _relayed(n) is not None and _relayed(n).largest == (2, 4)
            for n in _SIZES))
        rx = _Rx(await connect())
        current = {ParamType.LOCATION_FILTER: LocationFilter.relative(1)}
        reset = await _sub(rx, "video", current)
        gap = await _sub(rx, "audio", {**current, D20ParamType.FILL_TIMEOUT: 0})
        assert await rx.done(reset.request_id) == (False, [])
        assert await rx.done(gap.request_id) == (True, [(2, 3), (2, 4)])
        assert rx.gaps[gap.request_id] == [(0x20C, 2, 2)]
        assert rx.sizes(gap.request_id) == {_SIZES["audio"]}
        assert rx.session._close_err is None
