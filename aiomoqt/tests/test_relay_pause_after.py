"""--pause-after: a track the relay pulls from upstream with no
forwarding subscriber for that long is paused upstream (Forward State 0);
the next subscriber resumes it. Two tracks on one publisher session,
PUBLISHed or announced, over raw QUIC and WebTransport."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._relay_wait import until_published

pytestmark = requires_certs

_PORT = 26500
_NS = "pause/ns"
_PAUSE_S = 0.3
_SIZES = {"video": 300, "audio": 100, "catalog": 50}
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_FLOWS = pytest.mark.parametrize("flow", ["publish", "announce"])
_DRAFTS = [14, 18, 22]


def _port(case, draft, flow, use_quic):
    return (_PORT + 40 * case + 4 * _DRAFTS.index(draft)
            + 2 * (flow == "announce") + (not use_quic))


def _key(name):
    return (tuple(p.encode() for p in _NS.split("/")), name.encode())


def _relayed(name):
    return relay._tracks.get(_key(name))


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._catalogs.clear()


async def _until(cond, timeout=6.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return cond()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft, flow, pause_after, names,
                 probe=False):
    """Yields viewer(): a relay pausing after `pause_after`, fed by one
    publisher session carrying `names`. viewer() opens a subscriber
    session and returns (session, objects by track name)."""
    _reset()
    relay._pause_after = pause_after
    relay._catalog_probe = probe
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    by_size = {size: name for name, size in _SIZES.items()}
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
            for name in names:
                track = PublishedTrack(pub, namespace=_NS, trackname=name,
                                       object_size=_SIZES[name],
                                       group_size=5, rate=200)
                if flow == "publish":
                    await track.publish(announce_namespace=False,
                                        publish_track=True)
                else:
                    track.attach()
            if flow == "publish":
                await until_published(_NS, names)
            if flow == "announce":
                await pub.publish_namespace(namespace=_NS,
                                            wait_response=True)

            async def viewer():
                s = await connect()
                objects = {name: [] for name in names}

                def _on_object(msg, _size, _ts, group_id, _subgroup_id):
                    name = by_size.get(len(msg.payload or b""))
                    if name in objects:
                        objects[name].append((group_id, msg.object_id))
                s.on_object_received = _on_object
                return s, objects

            yield viewer
    finally:
        relay._pause_after = relay.PAUSE_AFTER_S
        relay._catalog_probe = False
        handle.close()
        _reset()


async def _still_flowing(track, wait=0.5) -> bool:
    before = track._objects_in
    await asyncio.sleep(wait)
    return track._objects_in > before


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
@_FLOWS
@_TRANSPORTS
async def test_an_unwatched_track_pauses_and_a_viewer_resumes_it(
        draft, flow, use_quic):
    port = _port(0, draft, flow, use_quic)
    async with _relay(port, use_quic, draft, flow, _PAUSE_S,
                      ("video", "audio")) as viewer:
        rx, objects = await viewer()
        subs = {name: await rx.subscribe(_NS, name, forward=1,
                                         wait_response=True)
                for name in ("video", "audio")}
        assert await _until(lambda: all(objects.values())), objects
        video, audio = _relayed("video"), _relayed("audio")

        rx.unsubscribe(subs["video"].request_id)
        assert await _until(lambda: video.paused), "video never paused"
        await asyncio.sleep(0.3)
        assert not await _still_flowing(video), "paused track still pulled"
        seen = len(objects["audio"])
        assert await _still_flowing(audio)
        assert not audio.paused
        assert len(objects["audio"]) > seen

        rx2, objects2 = await viewer()
        await rx2.subscribe(_NS, "video", forward=1, wait_response=True)
        assert await _until(lambda: objects2["video"]), "resume got nothing"
        assert not video.paused
        assert _relayed("video") is video
        assert rx._close_err is None and rx2._close_err is None


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 22])
@_FLOWS
@_TRANSPORTS
async def test_a_probed_catalog_never_pauses(draft, flow, use_quic):
    port = _port(1, draft, flow, use_quic)
    async with _relay(port, use_quic, draft, flow, _PAUSE_S,
                      ("catalog",), probe=True) as viewer:
        assert await _until(lambda: _relayed("catalog") is not None
                            and _relayed("catalog")._objects_in > 0)
        catalog = _relayed("catalog")
        rx, objects = await viewer()
        ok = await rx.subscribe(_NS, "catalog", forward=1,
                                wait_response=True)
        assert await _until(lambda: objects["catalog"])
        rx.unsubscribe(ok.request_id)
        await asyncio.sleep(4 * _PAUSE_S)
        assert not catalog.paused
        assert await _still_flowing(catalog)


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 22])
@_FLOWS
@_TRANSPORTS
async def test_pause_after_0_never_pauses(draft, flow, use_quic):
    port = _port(2, draft, flow, use_quic)
    async with _relay(port, use_quic, draft, flow, 0,
                      ("video", "audio")) as viewer:
        rx, objects = await viewer()
        oks = [await rx.subscribe(_NS, name, forward=1, wait_response=True)
               for name in ("video", "audio")]
        assert await _until(lambda: all(objects.values())), objects
        for ok in oks:
            rx.unsubscribe(ok.request_id)
        await asyncio.sleep(4 * _PAUSE_S)
        for name in ("video", "audio"):
            assert not _relayed(name).paused
            assert await _still_flowing(_relayed(name))
