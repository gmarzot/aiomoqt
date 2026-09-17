"""LOC packaging round-trip: LocTrackPublisher → LocTrackSubscriber
over loopback, all three stream mappings. The mock server drives the
publisher's generate() directly under the SUBSCRIBE_OK alias, so the
tests exercise packaging (grouping, properties, ordering), not the
PublishedTrack handshake (covered elsewhere).
"""
import asyncio
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.media import (
    LocTrackPublisher, LocTrackSubscriber, StreamMapping,
)
from aiomoqt.media.loc import (
    LOC_PROP_CODECSTRING, LOC_PROP_TIMESCALE, LOC_PROP_VIDEO_FRAME_MARKING,
    LocFrame,
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
async def test_forward_state_zero_drops_until_key_frame():
    # §5.1: no objects while Forward State is 0; on 1 the track resumes
    # at the next key frame in a fresh group.
    port = _BASE_PORT + 11
    pubs = []
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=18,
    )

    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        await asyncio.sleep(0.05)
        pub = LocTrackPublisher(session, "loc/ns", "track",
                                mapping=StreamMapping.PER_GROUP)
        pubs.append(pub)
        pub.forward = False
        await pub.send_frame(b"idr-0", key_frame=True, timestamp=1000)
        await pub.send_frame(b"p-1", key_frame=False, timestamp=1001)
        gen = asyncio.create_task(pub.generate(session, ok.track_alias))
        await asyncio.sleep(0.05)
        pub.forward = True
        await pub.send_frame(b"p-2", key_frame=False, timestamp=1002)
        await pub.send_frame(b"idr-1", key_frame=True, timestamp=1003)
        await pub.send_frame(b"p-4", key_frame=False, timestamp=1004)
        await pub.finish()
        await gen

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server = await server.serve()
    try:
        got, _ = await _subscribe_collect(port, 2)
    finally:
        server.close()
    assert sorted((g, o, f.payload) for g, o, f in got) == [
        (0, 0, b"idr-1"), (0, 1, b"p-4")]
    assert pubs[0].frames_dropped == 3


@pytest.mark.asyncio
async def test_two_peers_share_one_track_over_the_wire():
    # One track, two sessions: both peers get every object under the
    # same group and object ids, off one pass of the frame queue.
    port = _BASE_PORT + 12
    state = {}
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=18,
    )

    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        track = state.get("track")
        if track is None:
            track = LocTrackPublisher(session, "loc/ns", "track",
                                      mapping=StreamMapping.PER_GROUP)
            state["track"] = track
            state["gen"] = [asyncio.ensure_future(
                track.generate(session, ok.track_alias))]
            return
        track.add_session(session)
        state["gen"].append(asyncio.ensure_future(
            track.generate(session, ok.track_alias)))
        await asyncio.sleep(0.05)          # both lanes attached
        for i, (payload, key) in enumerate(_VIDEO_FRAMES):
            await track.send_frame(payload, key_frame=key,
                                   timestamp=1000 + i)
        await track.finish()
        await asyncio.gather(*state["gen"])

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server = await server.serve()
    try:
        (got_a, _), (got_b, _) = await asyncio.gather(
            _subscribe_collect(port, len(_VIDEO_FRAMES)),
            _subscribe_collect(port, len(_VIDEO_FRAMES)))
    finally:
        server.close()

    seen_a = sorted((g, o, f.payload) for g, o, f in got_a)
    seen_b = sorted((g, o, f.payload) for g, o, f in got_b)
    assert seen_a == seen_b
    assert seen_a == [(0, 0, b"idr-0"), (0, 1, b"p-1"), (0, 2, b"p-2"),
                      (1, 0, b"idr-1"), (1, 1, b"p-4")]
    # Both peers were served from one pass of the queue. (The harness
    # drives generate() under the SUBSCRIBE_OK alias, so the handshake
    # never marks the subscriptions SUBSCRIBED — hence the count, not
    # `demand`.)
    assert state["track"]._total_sent == len(_VIDEO_FRAMES)
    assert len(state["track"].subscriptions) == 2


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


# -- codec_string: self-describing objects for catalog-less receivers --

_D18 = SimpleNamespace(negotiated_draft=18)


@pytest.mark.asyncio
async def test_codec_string_off_by_default():
    track = LocTrackPublisher(_D18, "ns", "video", config=_AVCC)
    for key, group_start in ((True, True), (False, False)):
        exts = track._object_extensions(LocFrame(b"x", key, 5), group_start,
                                        _D18)
        assert not {LOC_PROP_CODECSTRING, LOC_PROP_TIMESCALE,
                    LOC_PROP_VIDEO_FRAME_MARKING} & exts.keys()


@pytest.mark.asyncio
async def test_codec_string_properties_on_every_object():
    video = LocTrackPublisher(_D18, "ns", "video", config=_AVCC,
                              codec_string="avc1.64001F")
    key = video._object_extensions(LocFrame(b"i", True, 5), True, _D18)
    delta = video._object_extensions(LocFrame(b"p", False, 6), False, _D18)
    for exts in (key, delta):
        assert exts[LOC_PROP_CODECSTRING] == b"avc1.64001F"
        assert exts[LOC_PROP_TIMESCALE] == 1_000_000
    assert key[LOC_PROP_VIDEO_FRAME_MARKING] == b"\xe0"    # S|E|I
    assert delta[LOC_PROP_VIDEO_FRAME_MARKING] == b"\xc0"  # S|E

    audio = LocTrackPublisher(_D18, "ns", "audio", media_kind="audio",
                              config=b"\x11\x90", codec_string="mp4a.40.2")
    exts = audio._object_extensions(LocFrame(b"a", True, 7), True, _D18)
    assert exts[LOC_PROP_CODECSTRING] == b"mp4a.40.2"
    assert LOC_PROP_VIDEO_FRAME_MARKING not in exts

    scaled = LocTrackPublisher(_D18, "ns", "video", timescale=90000,
                               codec_string="avc1.64001F")
    exts = scaled._object_extensions(LocFrame(b"p", False, 8), False, _D18)
    assert exts[LOC_PROP_TIMESCALE] == 90000


@pytest.mark.asyncio
async def test_codec_string_round_trip():
    # d18 delta-codes property types: the added ids must still serialize
    # in order and decode on the other side.
    port = _BASE_PORT + 20
    server = await _make_server(port, StreamMapping.PER_GROUP, _VIDEO_FRAMES,
                                config=_AVCC,
                                codec_string="avc1.64001F").serve()
    try:
        got, sub = await _subscribe_collect(port, len(_VIDEO_FRAMES))
    finally:
        server.close()
    assert len(got) == len(_VIDEO_FRAMES)
    assert sub.codec_string == "avc1.64001F"
    assert sub.timescale == 1_000_000
    marking = {(g, o): bytes(f.extensions[LOC_PROP_VIDEO_FRAME_MARKING])
               for g, o, f in got}
    assert marking[(0, 0)] == marking[(1, 0)] == b"\xe0"
    assert marking[(0, 1)] == marking[(0, 2)] == marking[(1, 1)] == b"\xc0"
