"""A track resumed after --pause-after starts at live: the LOC producer
keeps numbering frames while paused, the relay answers the resuming
SUBSCRIBE with the Largest from the publisher's REQUEST_OK, and the
joining FETCH (d16/d18) or current-group fill (d20+) serves nothing from
before the pause, even from a publisher whose Largest stood still, where
a fill the fence empties is reset (§3.4.1). A join that beats the
pause's REQUEST_OK still gets the open group, and a PUBLISH offer to a
SUBSCRIBE_TRACKS subscriber resumes the same way. Two LOC tracks on one
publisher session, over raw QUIC and WebTransport."""
import asyncio
import contextlib
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import LocTrackPublisher
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import MOQTRequestError, ParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._relay_wait import until_published

_PORT = 26700
_NS = "resume/ns"
_PAUSE_S = 0.3
_FPS = 100
# Video opens a group every _GOP frames; every audio frame is a group.
_GOP = {"video": 10, "audio": 1}
_DRAFTS = [16, 18, 20, 22]


def _key(name):
    return (tuple(p.encode() for p in _NS.split("/")), name.encode())


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._catalogs.clear()
    relay._resuming.clear()


async def _until(cond, timeout=6.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return cond()


async def _source(track, gop):
    """Frames at _FPS with time-based group ids, as an encoder feeds them."""
    n = 0
    while True:
        await track.send_frame(f"{track.trackname}-{n}".encode(),
                               key_frame=n % gop == 0, group_id=n // gop)
        n += 1
        await asyncio.sleep(1 / _FPS)


def _reply_largest(ok):
    if not getattr(ok, "content_exists", 0):
        return None
    return (ok.largest_group_id, ok.largest_object_id)


def _track_of(msg, names=_GOP):
    payload = bytes(msg.payload or b"")
    return next((n for n in names if n.encode() in payload), None)


class _Stuck(LocTrackPublisher):
    """A publisher whose Largest does not move while paused."""

    def _note_largest(self, group_id, object_id):
        if self.producing:
            super()._note_largest(group_id, object_id)


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft, cls, gops=_GOP):
    """Yields connect(): a relay pausing after _PAUSE_S, fed LOC tracks
    `gops` (name -> frames per group) by one publisher session."""
    _reset()
    relay._pause_after = _PAUSE_S
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic,
                                       draft=draft).serve()
    sources = []
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            pub = await connect()
            for name, gop in gops.items():
                track = cls(pub, _NS, name,
                            media_kind="audio" if gop == 1 else "video")
                await track.publish(announce_namespace=False,
                                    publish_track=True)
                sources.append(asyncio.create_task(_source(track, gop)))
            await until_published(_NS, tuple(gops))
            yield connect
    finally:
        for task in sources:
            task.cancel()
        relay._pause_after = relay.PAUSE_AFTER_S
        handle.close()
        _reset()


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("publisher", ["live", "stuck"])
async def test_a_resumed_track_starts_at_live(publisher, draft, use_quic):
    port = (_PORT + 8 * (publisher == "stuck") + 2 * _DRAFTS.index(draft)
            + (not use_quic))
    cls = LocTrackPublisher if publisher == "live" else _Stuck
    async with _relay(port, use_quic, draft, cls) as connect:
        rx = await connect()
        live = {name: [] for name in _GOP}
        rx.on_object_received = (
            lambda msg, _n, _ts, g, _sg: live.get(_track_of(msg), []).append(
                (g, msg.object_id)))
        oks = [await rx.subscribe(_NS, name, forward=1, wait_response=True)
               for name in _GOP]
        assert await _until(lambda: all(live.values())), live
        for ok in oks:
            rx.unsubscribe(ok.request_id)
        tracks = {name: relay._tracks[_key(name)] for name in _GOP}
        assert await _until(lambda: all(t.paused for t in tracks.values()))
        await asyncio.sleep(0.2)
        before = {name: t.largest for name, t in tracks.items()}
        # The source runs on while paused: video opens ~5 more groups.
        await asyncio.sleep(0.5)

        rx2 = await connect()
        live = {name: [] for name in _GOP}
        fetched = {name: [] for name in _GOP}
        rx2.on_object_received = (
            lambda msg, _n, _ts, g, _sg: live.get(_track_of(msg), []).append(
                (g, msg.object_id)))
        rx2.on_fetch_object = (
            lambda obj, _n, _ts, _rid: fetched.get(
                _track_of(obj), []).append((obj.group_id, obj.object_id)))
        for name in _GOP:
            if draft >= 20:
                # SUBSCRIBE with a current-group fill.
                ok, _ = await rx2.join(_NS, name, wait_response=True)
                if publisher == "stuck":
                    fence = tracks[name].fence
                    assert fence is not None, name
                    t0 = time.monotonic()
                    done = await rx2.await_fetch_done(ok.request_id,
                                                      timeout=3.0)
                    waited = time.monotonic() - t0
                    assert waited < 1.0, (name, done, waited)
                    # A frame produced before the resume's REQUEST_OK moves
                    # the Largest past the fence: that group is live.
                    if ok.largest_group_id <= fence:
                        assert done is False, (name, fence, done)
            else:
                ok = await rx2.subscribe(_NS, name, forward=1,
                                         wait_response=True)
                with contextlib.suppress(MOQTRequestError):
                    await rx2.joining_fetch(ok.request_id, joining_start=0,
                                            wait_response=True)
            largest = _reply_largest(ok)
            assert largest is not None and largest >= before[name], (
                name, largest, before[name])
            if publisher == "live":
                assert largest > before[name], (name, largest, before[name])
        assert await _until(lambda: all(live.values())), live
        await asyncio.sleep(0.2)
        for name in _GOP:
            stale = [loc for loc in fetched[name] + live[name]
                     if loc <= before[name]]
            assert not stale, (name, before[name], stale)
        assert rx2._close_err is None


_PAUSE_REPLY_S = 0.1
# One group spans the whole test: the join lands in the paused group.
_LONG_GOP = {"cam": 10_000, "screen": 10_000}


class _SlowPause(LocTrackPublisher):
    """Takes a forward=0 REQUEST_UPDATE _PAUSE_REPLY_S late; later updates
    wait behind it, in order."""

    _order = None

    async def _on_request_update(self, session, msg):
        if self._order is None:
            self._order = asyncio.Lock()
        async with self._order:
            forward = (msg.parameters or {}).get(ParamType.FORWARD)
            await asyncio.sleep(_PAUSE_REPLY_S if forward == 0 else 0.03)
            await super()._on_request_update(session, msg)


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_join_beating_the_pause_reply_gets_the_open_group(
        draft, use_quic):
    port = _PORT + 40 + 2 * _DRAFTS.index(draft) + (not use_quic)
    async with _relay(port, use_quic, draft, _SlowPause,
                      gops=_LONG_GOP) as connect:
        rx = await connect()
        got = {name: [] for name in _LONG_GOP}
        rx.on_object_received = (
            lambda msg, _n, _ts, g, _sg: got.get(
                _track_of(msg, _LONG_GOP), []).append(g))
        oks = [await rx.subscribe(_NS, name, forward=1, wait_response=True)
               for name in _LONG_GOP]
        assert await _until(lambda: all(got.values())), got
        for ok in oks:
            rx.unsubscribe(ok.request_id)
        tracks = {name: relay._tracks[_key(name)] for name in _LONG_GOP}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 6.0
        while (not all(t.paused for t in tracks.values())
               and loop.time() < deadline):
            await asyncio.sleep(0.005)
        # Inside the gap: forward=0 sent, its REQUEST_OK not yet back.
        assert all(t.lowered and t.pause_largest is None
                   for t in tracks.values()), tracks

        rx2 = await connect()
        objs = {name: [] for name in _LONG_GOP}
        rx2.on_object_received = (
            lambda msg, _n, _ts, g, _sg: objs.get(
                _track_of(msg, _LONG_GOP), []).append((g, msg.object_id)))
        rx2.on_fetch_object = (
            lambda obj, _n, _ts, _rid: objs.get(
                _track_of(obj, _LONG_GOP), []).append(
                    (obj.group_id, obj.object_id)))
        joins = await asyncio.gather(*(
            rx2.join(_NS, name, wait_response=True) for name in _LONG_GOP))
        for name, (ok, fetch_ok) in zip(_LONG_GOP, joins):
            rid = ok.request_id if draft >= 20 else fetch_ok.request_id
            done = await rx2.await_fetch_done(rid, timeout=3.0)
            assert done is True, (name, tracks[name].fence, done)
            head = (ok.largest_group_id, 0)
            assert await _until(lambda: head in objs[name], 2.0), (
                name, head, tracks[name].fence, objs[name][:5])
        assert rx2._close_err is None


class _Gated(LocTrackPublisher):
    """`producing` set by the test instead of by subscribers."""
    live = True

    @property
    def producing(self):
        return self.live


class _Out:
    def __init__(self):
        self.written = []

    def reset_group(self):
        pass

    async def write(self, group_id, object_id, payload, **_kw):
        self.written.append((group_id, object_id, payload))


@pytest.mark.asyncio
async def test_loc_largest_moves_while_paused():
    track = _Gated(None, "loc/ns", "video")
    out = _Out()
    produce = asyncio.create_task(track.produce(out))

    async def feed(frames):
        for payload, key, gid in frames:
            await track.send_frame(payload, key_frame=key, group_id=gid)
        await _until(track._frames.empty, 1.0)
        await asyncio.sleep(0)

    await feed([(b"k0", True, 0), (b"p", False, None)])
    assert track._largest == (0, 1)
    track.live = False
    await feed([(b"p", False, None), (b"k3", True, 3), (b"p", False, None),
                (b"k", True, None), (b"p", False, None)])
    assert track._largest == (4, 1)
    assert len(out.written) == 2, out.written
    track.live = True
    # A key frame naming a group already numbered while paused opens the
    # next one.
    await feed([(b"p", False, None), (b"k", True, 2), (b"p", False, None)])
    await track.finish()
    await produce
    assert out.written[2:] == [(5, 0, b"\x0a\x00k"), (5, 1, b"\x0a\x00p")]
    assert track._largest == (5, 1)


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_paused_track_offered_by_publish_starts_at_live(
        draft, use_quic):
    # A SUBSCRIBE_TRACKS subscriber offered a paused track: the relay
    # resumes it before the PUBLISH, which carries the live Largest, and
    # the subscription takes the fence: its joining FETCH (d18) serves
    # nothing from before the pause.
    port = _PORT + 60 + 2 * (draft == 22) + (not use_quic)
    async with _relay(port, use_quic, (draft,),
                      LocTrackPublisher) as connect:
        rx = await connect()
        live = {name: [] for name in _GOP}
        rx.on_object_received = (
            lambda msg, _n, _ts, g, _sg: live.get(_track_of(msg), []).append(
                (g, msg.object_id)))
        oks = [await rx.subscribe(_NS, name, forward=1, wait_response=True)
               for name in _GOP]
        assert await _until(lambda: all(live.values())), live
        for ok in oks:
            rx.unsubscribe(ok.request_id)
        tracks = {name: relay._tracks[_key(name)] for name in _GOP}
        assert await _until(lambda: all(t.paused for t in tracks.values()))
        await asyncio.sleep(0.2)
        before = {name: t.largest for name, t in tracks.items()}
        await asyncio.sleep(0.5)

        rx2 = await connect()
        got = {name: [] for name in _GOP}
        fetched = {name: [] for name in _GOP}
        rx2.on_fetch_object = (
            lambda obj, _n, _ts, _rid: fetched.get(
                _track_of(obj), []).append((obj.group_id, obj.object_id)))
        offers = {}
        await rx2.subscribe_tracks(namespace=_NS)
        for _ in _GOP:
            offer = await rx2.await_publish(timeout=5.0)
            name = offer.track_name.decode()
            largest = (offer.largest_group_id, offer.largest_object_id)
            assert largest > before[name], (name, largest, before[name])
            rx2._track_aliases[offer.track_alias] = offer.request_id
            rx2.register_object_handler(
                offer.track_alias,
                lambda m, _n, _ts, g, _sg, n=name: got[n].append(
                    (g, m.object_id)))
            rx2.publish_ok(offer, forward=1)
            offers[name] = offer
        assert await _until(lambda: all(
            len(t.downstream) == 1 for t in tracks.values()))
        for name, t in tracks.items():
            assert t.fence is not None and t.downstream[0].fence == t.fence, (
                name, t.fence, t.downstream[0].fence)
        assert await _until(lambda: all(got.values())), got
        if draft < 20:
            for offer in offers.values():
                with contextlib.suppress(MOQTRequestError):
                    await rx2.joining_fetch(offer.request_id,
                                            joining_start=0,
                                            wait_response=True)
        await asyncio.sleep(0.2)
        # From d20 PUBLISH_OK carries no filter: the subscription is
        # unfiltered and replays the cache, so only fetches are checked.
        for name in _GOP:
            seen = fetched[name] + (got[name] if draft < 20 else [])
            stale = [loc for loc in seen if loc <= before[name]]
            assert not stale, (name, before[name], stale)
        assert rx2._close_err is None
