"""MSF broadcast loopback: MediaPublisher (catalog + video + audio on
one session) → MediaSubscriber. Covers catalog-first ordering, per-track
demux on the publisher, initRef config resolution, delta updates, and
multi-track survival of per-track SUBSCRIBE_DONE.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.media import (
    Catalog, CatalogTrack, DeltaOp, InitData,
    LocTrackPublisher, MediaPublisher, MediaSubscriber, StreamMapping,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14800
_NS = "demo/loopback"
_AVCC = b"\x01\x64\x00\x1f\xff\xe1demo"
_N_VIDEO = 4  # groups: [K p] [K p]
_N_AUDIO = 3


def _catalog():
    return Catalog(
        generatedAt=1_753_000_000_000,
        tracks=[
            CatalogTrack(name="video", packaging="loc", isLive=True,
                         role="video", renderGroup=1, codec="avc1.640028",
                         width=640, height=360, framerate=30,
                         bitrate=1_000_000, initRef="v0"),
            CatalogTrack(name="audio", packaging="loc", isLive=True,
                         role="audio", renderGroup=1, codec="pcm-s16",
                         samplerate=48000, channelConfig="2",
                         bitrate=1_536_000),
        ],
        initDataList=[InitData.from_bytes("v0", _AVCC)],
    )


async def _feed(pub: MediaPublisher):
    video = pub._by_name["video"]
    audio = pub._by_name["audio"]
    for i in range(_N_VIDEO):
        await video.send_frame(b"v-%d" % i, key_frame=(i % 2 == 0),
                               timestamp=i * 33_333)
    for i in range(_N_AUDIO):
        await audio.send_frame(b"a-%d" % i, key_frame=True,
                               timestamp=i * 20_000)
    await video.finish()
    await audio.finish()


def _server(port, catalog, *, delta=None):
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=18,
    )
    pubs = {}

    async def _on_subscribe(session, msg):
        pub = pubs.get(id(session))
        if pub is None:
            pub = MediaPublisher(session, _NS, catalog)
            pub.add_track(LocTrackPublisher(session, _NS, "video"))
            pub.add_track(LocTrackPublisher(
                session, _NS, "audio", mapping=StreamMapping.DATAGRAM))
            pubs[id(session)] = pub
            if delta is not None:
                await pub.catalog_track.publish_delta(delta)
            await pub.catalog_track.finish()
            asyncio.ensure_future(_feed(pub))
        await pub._demux_subscribe(session, msg)

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    return server


async def _run_subscriber(port, **kwargs):
    frames = {}
    client = MOQTClient("localhost", port, path="/", use_quic=True,
                        verify_tls=False, supported_drafts=18)
    async with client.connect() as session:
        await session.client_session_init()
        sub = MediaSubscriber(
            session, _NS,
            on_frame=lambda name, f, gid, oid:
                frames.setdefault(name, []).append((gid, oid, f)),
            **kwargs)
        await sub.start()
        for _ in range(200):
            if (len(frames.get("video", [])) >= _N_VIDEO
                    and len(frames.get("audio", [])) >= _N_AUDIO):
                break
            await asyncio.sleep(0.02)
    return sub, frames


@pytest.mark.asyncio
async def test_broadcast_round_trip():
    port = _BASE_PORT + 1
    server = await _server(port, _catalog()).serve()
    try:
        sub, frames = await _run_subscriber(port)
    finally:
        server.close()
    # Catalog described both tracks; both subscribed and delivered.
    assert set(sub.tracks) == {"video", "audio"}
    assert [f.payload for _, _, f in sorted(frames["video"])] == [
        b"v-0", b"v-1", b"v-2", b"v-3"]
    assert [(g, o) for g, o, _ in sorted(frames["video"])] == [
        (0, 0), (0, 1), (1, 0), (1, 1)]
    assert sorted(f.payload for _, _, f in frames["audio"]) == [
        b"a-0", b"a-1", b"a-2"]
    # Video config resolved from the catalog initRef (§5.2.13) — the
    # wire carried none (publisher had no config set).
    assert sub.tracks["video"].config == _AVCC
    assert sub.catalog.find("video").codec == "avc1.640028"


@pytest.mark.asyncio
async def test_demux_routes_d16_update_and_serves_catalog_fetch():
    # REQUEST_UPDATE references the original request via
    # existing_request_id; a joining FETCH of the catalog is served
    # with the current complete catalog on a fetch stream.
    from types import SimpleNamespace
    from aiomoqt.messages import RequestUpdate
    from aiomoqt.context import profile_for

    writes = []

    async def _open_uni():
        return 42

    session = SimpleNamespace(
        fetch_ok=lambda request_id: writes.append(('ok', request_id)),
        open_uni_stream=_open_uni,
        stream_write=lambda sid, data, end_stream=False:
            writes.append(('w', sid, bytes(data), end_stream)),
        _profile=profile_for(18),
    )
    pub = MediaPublisher(session, _NS, _catalog())
    pub.catalog_track.request_id = 7
    routed = []

    async def _on_update(s, m):
        routed.append(m.existing_request_id)
    pub.catalog_track._on_request_update = _on_update

    await pub._demux_update(session, RequestUpdate(
        request_id=99, existing_request_id=7,
        parameters={}))
    assert routed == [7]

    fetch = SimpleNamespace(track_name=None, joining_request_id=7,
                            request_id=3)
    await pub._demux_fetch(session, fetch)
    assert writes[0] == ('ok', 3)
    assert writes[1][1] == 42                      # FetchHeader
    payload = writes[2][2]
    assert b'"tracks"' in payload and writes[2][3]  # catalog + FIN


@pytest.mark.asyncio
async def test_broadcast_delta_update():
    port = _BASE_PORT + 2
    delta = Catalog.delta([DeltaOp("clone", [CatalogTrack(
        parentName="video", name="video-2", bitrate=500_000)])])
    server = await _server(port, _catalog(), delta=delta).serve()
    seen = []
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            sub = MediaSubscriber(
                session, _NS,
                on_catalog=lambda c: seen.append(len(c.tracks)),
                track_filter=lambda t: t.name == "audio")
            await sub.start()
            for _ in range(200):
                if len(seen) >= 2:
                    break
                await asyncio.sleep(0.02)
    finally:
        server.close()
    # Independent catalog (2 tracks) then the applied clone delta (3).
    assert seen[:2] == [2, 3]
    assert sub.catalog.find("video-2").codec == "avc1.640028"
    assert sub.catalog.find("video-2").bitrate == 500_000
    # track_filter honored: only audio was subscribed.
    assert set(sub.tracks) == {"audio"}
