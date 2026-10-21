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
