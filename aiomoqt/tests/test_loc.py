"""LOC packaging round-trip: LocTrackPublisher → LocTrackSubscriber
over loopback, all three stream mappings. The mock server drives the
publisher's generate() directly under the SUBSCRIBE_OK alias, so the
tests exercise packaging (grouping, properties, ordering), not the
PublishedTrack handshake (covered elsewhere).
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.media import (
    LocTrackPublisher, LocTrackSubscriber, StreamMapping,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14780
_AVCC = b"\x01\x64\x00\x1f\xff\xe1"  # placeholder extradata

# frames: (payload, key_frame) — two groups: 3 + 2 frames.
_VIDEO_FRAMES = [(b"idr-0", True), (b"p-1", False), (b"p-2", False),
                 (b"idr-1", True), (b"p-4", False)]


def _make_server(port, mapping, frames, **pub_kwargs):
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=18,
    )

    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        await asyncio.sleep(0.05)
        pub = LocTrackPublisher(session, "loc/ns", "track",
                                mapping=mapping, **pub_kwargs)
        for i, (payload, key) in enumerate(frames):
            await pub.send_frame(payload, key_frame=key, timestamp=1000 + i)
        await pub.finish()
        await pub.generate(session, ok.track_alias)

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    return server


async def _subscribe_collect(port, n_frames):
    got = []
    client = MOQTClient("localhost", port, path="/", use_quic=True,
                        verify_tls=False, supported_drafts=18)
    async with client.connect() as session:
        await session.client_session_init()
        sub = LocTrackSubscriber(
            session, "loc/ns", "track",
            on_frame=lambda f, gid, oid: got.append((gid, oid, f)))
        await sub.subscribe()
        for _ in range(200):
            if len(got) >= n_frames:
                break
            await asyncio.sleep(0.02)
    return got, sub


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", list(StreamMapping),
                         ids=lambda m: m.value)
async def test_video_round_trip(mapping):
    port = _BASE_PORT + list(StreamMapping).index(mapping)
    server = await _make_server(port, mapping, _VIDEO_FRAMES,
                                config=_AVCC, timescale=90000).serve()
    try:
        got, sub = await _subscribe_collect(port, len(_VIDEO_FRAMES))
    finally:
        server.close()
    assert len(got) == len(_VIDEO_FRAMES)
    # Grouping: rotate on key frames — (group, object) sequence.
    assert [(g, o) for g, o, _ in sorted(got[:3])] == [
        (0, 0), (0, 1), (0, 2)]
    assert [(g, o) for g, o, _ in sorted(got[3:])] == [(1, 0), (1, 1)]
    by_id = {(g, o): f for g, o, f in got}
    assert by_id[(0, 0)].payload == b"idr-0" and by_id[(0, 0)].key_frame
    assert by_id[(1, 1)].payload == b"p-4" and not by_id[(1, 1)].key_frame
    # Properties: timestamps round-trip; config + timescale captured
    # from the group-start object.
    assert by_id[(0, 1)].timestamp == 1001
    assert sub.config == _AVCC
    assert sub.timescale == 90000


@pytest.mark.asyncio
async def test_audio_one_object_per_group():
    # LOC §4.1: every chunk key_frame=True ⇒ one object per group.
    port = _BASE_PORT + 10
    frames = [(b"a%d" % i, True) for i in range(4)]
    server = await _make_server(port, StreamMapping.DATAGRAM,
                                frames).serve()
    try:
        got, _ = await _subscribe_collect(port, len(frames))
    finally:
        server.close()
    assert sorted((g, o) for g, o, _ in got) == [
        (0, 0), (1, 0), (2, 0), (3, 0)]


@pytest.mark.asyncio
async def test_config_seeded_from_catalog():
    # No VIDEO_CONFIG on the wire — set_config (catalog initRef path)
    # provides it and the wire never overwrites it with absence.
    port = _BASE_PORT + 11
    server = await _make_server(port, StreamMapping.PER_GROUP,
                                _VIDEO_FRAMES[:3]).serve()
    try:
        got = []
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            sub = LocTrackSubscriber(
                session, "loc/ns", "track",
                on_frame=lambda f, gid, oid: got.append(f))
            sub.set_config(b"catalog-extradata")
            await sub.subscribe()
            for _ in range(200):
                if len(got) >= 3:
                    break
                await asyncio.sleep(0.02)
        assert sub.config == b"catalog-extradata"
    finally:
        server.close()
