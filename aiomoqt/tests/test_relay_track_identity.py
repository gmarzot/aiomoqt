"""A stale track's end never removes a newer track registered under the
same name: after a newer publisher takes both names, the old tracks'
end-of-track, upstream PUBLISH_DONE and malformed reset leave the new
tracks in the table and their viewers receiving. Two LOC tracks on one
publisher session, over raw QUIC and WebTransport."""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import LocTrackPublisher
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import StreamResetCode, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._relay_wait import until_published

_PORT = 25700
_NS = "identity/ns"
_FPS = 100
_GOP = {"cam": 10, "mic": 1}
_DRAFTS = [18, 22]
_PATHS = ["end", "publish_done", "malformed"]


def _key(name):
    return (tuple(p.encode() for p in _NS.split("/")), name.encode())


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._catalogs.clear()


async def _until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return cond()


async def _source(track, gop):
    n = 0
    while True:
        await track.send_frame(f"{track.trackname}-{n}".encode(),
                               key_frame=n % gop == 0, group_id=n // gop)
        n += 1
        await asyncio.sleep(1 / _FPS)


def _track_of(msg):
    payload = bytes(msg.payload or b"")
    return next((n for n in _GOP if n.encode() in payload), None)


def _drive(path, old, key):
    if path == "end":
        old.note_upstream_done(SubscribeDoneCode.TRACK_ENDED, "ended", key, 0)
    elif path == "publish_done":
        # A late PUBLISH_DONE for a track already closed.
        old.close()
        relay._upstream_done(old, key, SimpleNamespace(
            status_code=SubscribeDoneCode.TRACK_ENDED, reason="ended",
            stream_count=0))
    else:
        g = old.largest[0] if old.largest else 0
        old.on_stream_end(g, 0, clean=False,
                          reset_code=StreamResetCode.MALFORMED_TRACK)


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_stale_track_end_keeps_the_newer_track(path, draft,
                                                       use_quic):
    port = (_PORT + 4 * _PATHS.index(path) + 2 * _DRAFTS.index(draft)
            + (not use_quic))
    _reset()
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

            async def publish(pub):
                for name, gop in _GOP.items():
                    track = LocTrackPublisher(
                        pub, _NS, name,
                        media_kind="audio" if gop == 1 else "video")
                    await track.publish(announce_namespace=False,
                                        publish_track=True)
                    sources.append(asyncio.create_task(_source(track, gop)))
                await until_published(_NS, tuple(_GOP))

            async def view():
                rx = await connect()
                got = {name: 0 for name in _GOP}

                def on_object(msg, *_a):
                    name = _track_of(msg)
                    if name is not None:
                        got[name] += 1
                rx.on_object_received = on_object
                for name in _GOP:
                    await rx.subscribe(_NS, name, forward=1,
                                       wait_response=True)
                assert await _until(lambda: all(got.values())), got
                return got

            await publish(await connect())
            await view()
            old = {name: relay._tracks[_key(name)] for name in _GOP}
            # A newer publisher takes both names while the old tracks run.
            for name in _GOP:
                del relay._tracks[_key(name)]
            await publish(await connect())
            new = {name: relay._tracks[_key(name)] for name in _GOP}
            assert all(new[n] is not old[n] for n in _GOP)
            got = await view()

            for name in _GOP:
                _drive(path, old[name], _key(name))
            assert await _until(lambda: all(
                t.finished and t.task is None for t in old.values())), path
            for name in _GOP:
                assert old[name].publish_alias is None, name
                assert relay._tracks.get(_key(name)) is new[name], name
            before = dict(got)
            assert await _until(lambda: all(
                got[n] > before[n] + 5 for n in _GOP)), (before, got)
    finally:
        for task in sources:
            task.cancel()
        handle.close()
        _reset()
