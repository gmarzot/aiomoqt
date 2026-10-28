"""A track resumed after --pause-after starts at live: the LOC producer
keeps numbering frames while paused, the relay answers the resuming
SUBSCRIBE with the Largest from the publisher's REQUEST_OK, and the
joining FETCH (d16/d18) or current-group fill (d20+) serves nothing from
before the pause, even from a publisher whose Largest stood still, where
a fill the fence empties is reset (§3.4.1). Two LOC tracks on one
publisher session, over raw QUIC and WebTransport."""
import asyncio
import contextlib
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import LocTrackPublisher
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import MOQTRequestError

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


def _track_of(msg):
    payload = bytes(msg.payload or b"")
    return next((n for n in _GOP if n.encode() in payload), None)


class _Stuck(LocTrackPublisher):
    """A publisher whose Largest does not move while paused."""

    def _note_largest(self, group_id, object_id):
        if self.producing:
            super()._note_largest(group_id, object_id)


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft, cls):
    """Yields connect(): a relay pausing after _PAUSE_S, fed LOC video and
    audio by one publisher session."""
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
            for name, gop in _GOP.items():
                track = cls(pub, _NS, name,
                            media_kind="audio" if gop == 1 else "video")
                await track.publish(announce_namespace=False,
                                    publish_track=True)
                sources.append(asyncio.create_task(_source(track, gop)))
            await until_published(_NS, tuple(_GOP))
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
        fenced_fills = 0
        for name in _GOP:
            if draft >= 20:
                # SUBSCRIBE with a current-group fill.
                ok, _ = await rx2.join(_NS, name, wait_response=True)
                if publisher == "stuck":
                    # The relay's fence can trail the publisher's Largest
                    # by an object in flight at the pause.
                    fence = tracks[name].fence
                    fenced = (fence is not None
                              and ok.largest_group_id <= fence)
                    t0 = time.monotonic()
                    done = await rx2.await_fetch_done(ok.request_id,
                                                      timeout=3.0)
                    waited = time.monotonic() - t0
                    assert done != fenced and waited < 1.0, (
                        name, fence, done, waited)
                    fenced_fills += fenced
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
        if draft >= 20 and publisher == "stuck":
            assert fenced_fills, "no fill was fenced"
        assert await _until(lambda: all(live.values())), live
        await asyncio.sleep(0.2)
        for name in _GOP:
            stale = [loc for loc in fetched[name] + live[name]
                     if loc <= before[name]]
            assert not stale, (name, before[name], stale)
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
