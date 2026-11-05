"""LOC payloads in the clear are the media alone (loc-04 §3.1.3, Secure
Objects -01 §3.3): nothing is prepended on send or stripped on receive."""
import asyncio
import io
import re
import sys
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import (
    LocmafTrackPublisher, LocmafTrackSubscriber, LocTrackPublisher,
    LocTrackSubscriber, StreamMapping,
)
from aiomoqt.media.cmaf import CmafChunker
from aiomoqt.media.locmaf import (
    GroupState, LocmafDecoder, LocmafEncoder, TrackContext,
)
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_media_locmaf import clip  # noqa: F401
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media, sub_media
from aiomoqt.types import MOQTMessageType

_BASE_PORT = 16720
_D18 = SimpleNamespace(negotiated_draft=18)
# An AV1 sequence header OBU opens with 0x0A, the block type.
_AV1_SEQ = b"\x0a\x0b\x00\x00\x00\x02\xaf"


def _pub(**kw):
    return LocTrackPublisher(_D18, "loc/ns", "track", **kw)


def _receive(sub, payload):
    """Frame payloads delivered for one received object, and what
    on_arrival saw."""
    got, seen = [], []
    sub.on_frame = lambda f, g, o: got.append(f.payload)
    sub.on_arrival = lambda m, rx, g, sg, f: seen.append(f)
    sub._on_object(SimpleNamespace(extensions={}, payload=payload,
                                   object_id=0, group_id=0),
                   len(payload), 0, 0, 0)
    return got, seen


# -- send / receive hooks -----------------------------------------------

def test_package_sends_the_frame():
    assert _pub()._package(b"idr", 0, 0) == b"idr"
    assert _pub()._package(_AV1_SEQ, 0, 0) == _AV1_SEQ


def test_payload_is_the_frame_whatever_it_opens_with():
    sub = LocTrackSubscriber(None, "loc/ns", "track")
    for payload in (_AV1_SEQ, b"\x0a\x00idr", b"frame"):
        assert _receive(sub, payload)[0] == [payload]
    assert sub.objects_rejected == 0


def test_zero_length_payload_passes_through():
    assert _pub()._package(b"", 0, 0) == b""
    sub = LocTrackSubscriber(None, "loc/ns", "track")
    assert _receive(sub, b"")[0] == [b""]
    assert sub.objects_rejected == 0


def test_locmaf_objects_carry_no_block():
    chunker = CmafChunker(SimpleNamespace(
        timescale=90000, width=64, height=64, sample_entry_bytes=bytes(8)))
    init = chunker.init_segment()
    chunk = chunker.chunk(b"K" * 64, 3000, True)
    want = LocmafEncoder(TrackContext.from_init(init)).encode(
        chunk, GroupState(), 0)
    rebuilt = LocmafDecoder(init).push(0, 0, want)

    assert _pub()._package(chunk, 0, 0) == chunk
    pub = LocmafTrackPublisher(_D18, "loc/ns", "v", init=init)
    assert pub._package(chunk, 0, 0) == want
    sub = LocmafTrackSubscriber(None, "loc/ns", "v", init=init)
    assert _receive(sub, want)[0] == [rebuilt]
    assert sub.objects_rejected == 0


# -- over the wire ------------------------------------------------------

_FRAMES = [(_AV1_SEQ, True), (b"p-1", False), (b"\x0a", False),
           (b"idr-1", True)]
_TRACKS = ("video", "audio")


def _server(port, use_quic, gens):
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=use_quic, supported_drafts=18)

    async def _on_subscribe(session, msg):
        name = msg.track_name.decode()
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        await asyncio.sleep(0.05)
        pub = LocTrackPublisher(session, "loc/ns", name,
                                mapping=StreamMapping.PER_GROUP)
        for i, (payload, key) in enumerate(_FRAMES):
            await pub.send_frame(payload, key_frame=key, timestamp=1000 + i)
        await pub.finish()
        gens.append(asyncio.ensure_future(
            pub.generate(session, ok.track_alias)))

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    return server


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_tracks_on_one_session_deliver_payloads_verbatim(use_quic):
    port = _BASE_PORT + (0 if use_quic else 1)
    gens = []
    server = await _server(port, use_quic, gens).serve()
    got = {name: [] for name in _TRACKS}
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            subs = {}
            for name in _TRACKS:
                subs[name] = LocTrackSubscriber(
                    session, "loc/ns", name,
                    on_frame=(lambda f, g, o, _n=name:
                              got[_n].append(f.payload)))
                await subs[name].subscribe()

            def _done():
                return all(len(got[n]) + subs[n].objects_rejected
                           >= len(_FRAMES) for n in _TRACKS)
            for _ in range(250):
                if _done():
                    break
                await asyncio.sleep(0.02)
    finally:
        for g in gens:
            g.cancel()
        server.close()
    frames = [p for p, _ in _FRAMES]
    for name in _TRACKS:
        assert got[name] == frames, name
        assert subs[name].objects_rejected == 0, name


# -- pub_media → relay → sub_media ---------------------------------------

def _summary(text):
    return {m[0]: (int(m[1]), int(m[2])) for m in re.findall(
        r"^  (\S+): (\d+) frames, (\d+) rejected$", text, re.MULTILINE)}


def _argv(monkeypatch, tool, *argv):
    monkeypatch.setattr(sys, 'argv', [tool, *argv])
    return (pub_media if tool == 'pub_media' else sub_media).parse_args()


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_pub_media_loc_through_a_relay(clip, tmp_path,  # noqa: F811
                                             monkeypatch, capsys, use_quic):
    av = pytest.importorskip('av')
    ns = 'loc/e2e'
    port = _BASE_PORT + 2 + (0 if use_quic else 1)
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    published = []

    class _Recording(LocTrackPublisher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            published.append(self)
    monkeypatch.setattr(pub_media, 'LocTrackPublisher', _Recording)

    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server('localhost', port, CERT, KEY,
                                       use_quic=use_quic, draft=18).serve()
    pub_args = _argv(monkeypatch, 'pub_media', url, '-N', ns, '--mp4', clip,
                     '--pub-ns', '-k', '--draft', '18', '-t', '4',
                     '--stats', '0', '--catalog-interval', '0.5')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-N', ns, '--out',
                     str(tmp_path), '-k', '--draft', '18', '-t', '2')
    pub = asyncio.create_task(pub_media.run(pub_args))
    try:
        for _ in range(200):
            if relay._announced or pub.done():
                break
            await asyncio.sleep(0.05)
        assert relay._announced, "publisher never announced"
        await sub_media.run(sub_args)
        await asyncio.wait_for(pub, 30)
    finally:
        if not pub.done():
            pub.cancel()
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()

    summary = _summary(capsys.readouterr().err)
    assert set(summary) == {'video', 'audio'}
    assert {t.trackname for t in published} == {'video', 'audio'}
    for name, (frames, rejected) in summary.items():
        assert frames > 0 and rejected == 0, (name, frames, rejected)
    data = (tmp_path / 'video.h264').read_bytes()
    with av.open(io.BytesIO(data), format='h264') as c:
        assert sum(1 for _ in c.decode(c.streams[0])) > 0
