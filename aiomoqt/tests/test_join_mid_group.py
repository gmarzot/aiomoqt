"""A d22 Relative Start join mid-group (§3.3.1, §11.3.2): through the
relay the group comes from object 0 (the Location Filter carried
upstream, or the head fetched from upstream) or its stream is reset,
never FINed short; a warm cache replays it; a PublishedTrack resumed
mid-group sends the head from its history or resets. Two tracks on one
session, raw QUIC and WebTransport."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.params import LocationFilter
from aiomoqt.server import MOQTServer
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import _UNFILTERED, PublishedTrack
from aiomoqt.types import MOQTMessageType, ParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 20300
_NS = "join/mid"
_NAMES = ("video", "audio")
_GROUP = 10
_OBJ_S = 0.02
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case, use_quic):
    return _PORT + 2 * case + (not use_quic)


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()


class _Live(PublishedTrack):
    """A live source: objects appear on a clock whether or not anyone
    subscribes. A subscription starts at the live edge, or at object 0
    of the current group when its Location Filter starts there."""

    def start_clock(self):
        self._clock = asyncio.ensure_future(self._tick())

    async def _tick(self):
        n = 0
        while True:
            g, o = divmod(n, _GROUP)
            self._remember(g, o, self._payload(g, o))
            n += 1
            await asyncio.sleep(_OBJ_S)

    def _payload(self, g, o):
        return f"{self.trackname}-{g}-{o}".encode()

    async def generate(self, session, track_alias):
        sub = self._sub_for(session)
        g, o = self._largest
        o += 1
        if sub.window is not _UNFILTERED and sub.window[0] <= (g, 0):
            o = 0
        if o >= _GROUP:
            g, o = g + 1, 0
        while True:
            sid = await session.open_uni_stream()
            hdr = SubgroupHeader(track_alias=track_alias, group_id=g,
                                 subgroup_id=0, publisher_priority=128,
                                 extensions_present=True,
                                 prof=session._profile)
            session.stream_write(sid, hdr.serialize().data)
            for oid in range(o, _GROUP):
                while self._largest < (g, oid):
                    await asyncio.sleep(0.005)
                await session.stream_write_drain(sid, hdr.next_object(
                    payload=self._payload(g, oid), extensions=None,
                    object_id=oid).data)
            session.stream_fin(sid)
            g, o = g + 1, 0


class _Rx:
    """A subscriber session's objects and subgroup stream ends, by
    track."""

    def __init__(self, session):
        self.session = session
        self.got = {n: [] for n in _NAMES}
        self.ends = {n: {} for n in _NAMES}

    async def subscribe(self, lf=None):
        for name in _NAMES:
            ok = await self.session.subscribe(
                _NS, name, forward=1, wait_response=True,
                location_filter=lf or LocationFilter.relative(1),
                on_object=lambda m, _s, _t, g, _sg, n=name:
                self.got[n].append((g, m.object_id)) if m.payload else None)
            self.session.register_stream_end_handler(
                ok.track_alias,
                lambda g, sg, clean=True, reset_code=0, n=name:
                self.ends[n].setdefault(g, clean))

    def first_group(self, name):
        """(objects of the first group received, its stream ended clean)."""
        g = self.got[name][0][0]
        return [o for gg, o in self.got[name] if gg == g], \
            self.ends[name].get(g)

    async def through_first_group(self):
        assert await _until(lambda: all(
            self.got[n] and self.got[n][0][0] in self.ends[n]
            for n in _NAMES)), (self.got, self.ends)


async def _until(cond, tries=300):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, pub_draft, history=True):
    """Yields connect() to a relay with two _Live tracks published on a
    pub_draft session, mid-group."""
    _reset()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=(22, 18))
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect(draft=22):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            pub = await connect(pub_draft)
            tracks = []
            for name in _NAMES:
                t = _Live(pub, namespace=_NS, trackname=name,
                          fetch_history_bytes=(1 << 20) if history else 0)
                t.attach()
                t.start_clock()
                tracks.append(t)
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            try:
                yield connect, tracks
            finally:
                for t in tracks:
                    t._clock.cancel()
    finally:
        handle.close()
        _reset()


async def _mid_group(tracks, lo=3, hi=6):
    assert await _until(lambda: tracks[0]._largest is not None
                        and lo <= tracks[0]._largest[1] <= hi)


@_TRANSPORTS
async def test_relay_carries_the_location_filter_upstream(use_quic):
    # d22 upstream: the Relative Start reaches the publisher, which sends
    # the current group from object 0; a warm-cache joiner also gets it
    # from object 0.
    async with _relay(_port(0, use_quic), use_quic, 22) as (connect, tracks):
        await _mid_group(tracks)
        rx = _Rx(await connect())
        await rx.subscribe()
        await rx.through_first_group()
        for name in _NAMES:
            assert rx.first_group(name) == (list(range(_GROUP)), True), name
        await _mid_group(tracks)
        warm = _Rx(await connect())
        await warm.subscribe()
        await warm.through_first_group()
        for name in _NAMES:
            assert warm.first_group(name) == (list(range(_GROUP)), True)
        assert rx.session._close_err is None


@_TRANSPORTS
async def test_relay_backfills_the_head_from_a_pre_d20_upstream(use_quic):
    # A d18 upstream takes no Relative Start: the relay subscribes at the
    # live edge and fetches the group head.
    async with _relay(_port(1, use_quic), use_quic, 18) as (connect, tracks):
        await _mid_group(tracks)
        rx = _Rx(await connect())
        await rx.subscribe()
        await rx.through_first_group()
        for name in _NAMES:
            assert rx.first_group(name) == (list(range(_GROUP)), True), name


@_TRANSPORTS
async def test_relay_resets_a_group_it_cannot_deliver_whole(use_quic):
    # No FETCH history upstream: the partial group's stream is reset, not
    # FINed; later groups are whole.
    async with _relay(_port(2, use_quic), use_quic, 18,
                      history=False) as (connect, tracks):
        await _mid_group(tracks)
        rx = _Rx(await connect())
        await rx.subscribe()
        await rx.through_first_group()
        for name in _NAMES:
            objs, clean = rx.first_group(name)
            assert objs[0] > 0 and clean is False, (name, objs, clean)
        assert await _until(lambda: all(
            len(rx.ends[n]) >= 2 for n in _NAMES))
        for name in _NAMES:
            g = sorted(rx.ends[name])[1]
            assert rx.ends[name][g] is True
            assert [o for gg, o in rx.got[name] if gg == g] == list(
                range(_GROUP))


@_TRANSPORTS
async def test_published_track_resumed_mid_group(use_quic):
    # Forward State 0 then 1 mid-group: the resumed group comes from the
    # history from object 0 and FINs; with no history it is reset.
    peer = asyncio.get_running_loop().create_future()

    async def on_pub_ns(session, msg):
        session.publish_namepace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    port = _port(3, use_quic)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=22)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE, on_pub_ns)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=22)
        async with client.connect() as pub:
            await pub.client_session_init()
            tracks = {}
            for name, history in (("video", 1 << 20), ("audio", 0)):
                t = PublishedTrack(pub, namespace=_NS, trackname=name,
                                   object_size=64, group_size=_GROUP,
                                   rate=1 / _OBJ_S,
                                   fetch_history_bytes=history)
                t._quiet = True
                t.attach()
                tracks[name] = t
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            rx = _Rx(await asyncio.wait_for(peer, 5.0))
            await rx.subscribe()
            subs = {n: rid for rid, n in (
                (rid, m[0].track_name.decode())
                for rid, m in rx.session._subscriptions.items())}

            async def forward(value):
                for name in _NAMES:
                    await rx.session.request_update(
                        subs[name], wait_response=True,
                        parameters={ParamType.FORWARD: value})

            assert await _until(lambda: all(
                any(g >= 1 for g, _ in rx.got[n]) for n in _NAMES))
            await forward(0)
            v = tracks["video"]
            paused = v._largest[0]
            assert await _until(lambda: v._largest[0] > paused
                                and 3 <= v._largest[1] <= 6)
            n_before = {n: len(rx.got[n]) for n in _NAMES}
            await forward(1)
            resumed = {}
            assert await _until(lambda: all(
                len(rx.got[n]) > n_before[n] for n in _NAMES))
            for name in _NAMES:
                resumed[name] = rx.got[name][n_before[name]][0]
            assert await _until(lambda: all(
                resumed[n] in rx.ends[n] for n in _NAMES))
            g = resumed["video"]
            assert [o for gg, o in rx.got["video"][n_before["video"]:]
                    if gg == g] == list(range(_GROUP))
            assert rx.ends["video"][g] is True
            g = resumed["audio"]
            assert rx.got["audio"][n_before["audio"]][1] > 0
            assert rx.ends["audio"][g] is False
            assert rx.session._close_err is None
    finally:
        handle.close()
