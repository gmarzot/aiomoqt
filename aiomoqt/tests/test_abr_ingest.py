"""ABR ladder and abr_ingest end to end: a LOC source dials the ingester,
which publishes it and its ladder to a relay. Every test runs on a fake
cymedia everywhere, and on cymedia's CPU profile where cymedia is installed."""
import asyncio
import functools
import shutil
import struct
import subprocess
import sys
import time
from collections import defaultdict

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media.abr import AbrLadder, Rung, codec_string
from aiomoqt.media.broadcast import MediaPublisher, MediaSubscriber
from aiomoqt.media.catalog import Catalog, CatalogTrack, InitData
from aiomoqt.media.loc import LocFrame, LocTrackPublisher
from aiomoqt.media.sources import (
    AnnexBAssembler, avcc_codec_string, avcc_from_sps_pps, obu_units,
)
from aiomoqt.tests import _fake_cymedia
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port
from aiomoqt.tests._relay_wait import until_published

pytestmark = requires_certs

FPS = 30
GOP = 15
SECONDS = 3
RUNGS = [Rung(160, 120, 200_000), Rung(96, 72, 100_000)]


@functools.lru_cache(maxsize=1)
def _real_clip():
    """avcC and (LOC payload, key) frames of a 320x240 H.264 clip with a key
    frame every GOP frames."""
    raw = subprocess.run(
        ['ffmpeg', '-v', 'error', '-f', 'lavfi',
         '-i', f'testsrc2=size=320x240:rate={FPS}', '-t', str(SECONDS),
         '-c:v', 'libx264', '-g', str(GOP), '-keyint_min', str(GOP),
         '-sc_threshold', '0', '-bf', '0', '-pix_fmt', 'yuv420p',
         '-f', 'h264', '-'],
        capture_output=True, check=True).stdout
    asm = AnnexBAssembler()
    frames = asm.feed(raw) + asm.close()
    assert asm.config is not None and frames[0][1]
    return asm.config, tuple(frames)


def _fake_clip():
    config = avcc_from_sps_pps(_fake_cymedia.sps(9), _fake_cymedia.PPS)
    frames = [(struct.pack('>I', 4) + bytes([0x65 if n % GOP == 0 else 0x41,
                                             n & 0xFF, 0, 0]), n % GOP == 0)
              for n in range(SECONDS * FPS)]
    return config, frames


@pytest.fixture(params=['fake', 'cymedia'])
def impl(request, monkeypatch):
    """The cymedia the ladder will use, put in place."""
    if request.param == 'fake':
        monkeypatch.setitem(sys.modules, 'cymedia', _fake_cymedia)
        return _fake_cymedia
    cymedia = pytest.importorskip('cymedia')
    if shutil.which('ffmpeg') is None:
        pytest.skip('the source clip needs the ffmpeg CLI')
    cymedia.set_ffmpeg_log_level(16)
    return cymedia


def _needs(impl, codec):
    """Skip unless the real cymedia has a software encoder for `codec`."""
    if impl is _fake_cymedia:
        return
    if codec not in getattr(impl, 'CODECS', ('h264',)):
        pytest.skip(f'this cymedia has no {codec} ladders')
    names = ('libaom-av1',) if codec == 'av1' else ('libopenh264', 'libx264')
    if not any(impl.have_encoder(n) for n in names):
        pytest.skip(f'no software {codec} encoder in FFmpeg')


@pytest.fixture
def clip(impl):
    return _fake_clip() if impl is _fake_cymedia else _real_clip()


def _rung_payload_ok(codec, payload, key):
    """mp4-sample form: H.264 length-prefixed with no parameter sets, AV1
    without temporal delimiters or padding and a sequence header on key
    frames."""
    if codec == 'av1':
        kinds = [t for t, _ in obu_units(payload)]
        return 2 not in kinds and 15 not in kinds and (1 in kinds) == key
    return payload[:4] != b'\x00\x00\x00\x01' and (payload[4] & 0x1F) not in (7, 8)


@pytest.mark.parametrize('codec', ['h264', 'av1'])
async def test_ladder_keeps_source_ids_and_opens_groups_on_key_frames(impl, clip, codec):
    _needs(impl, codec)
    config, frames = clip
    got = defaultdict(list)

    def on_frame(i, frame, group_id, object_id, pushed_ns):
        got[i].append((group_id, object_id, frame.key_frame,
                       frame.timestamp, frame.payload))

    ladder = AbrLadder(RUNGS, config=config, fps=(FPS, 1), profile='cpu',
                       codec=codec, on_frame=on_frame, realtime=False)
    ladder.start()
    sent = []
    try:
        group, obj = -1, 0
        for n, (payload, key) in enumerate(frames):
            if key:
                group, obj = group + 1, 0
            ts = 1_000_000 + n * 33_333
            assert ladder.push(LocFrame(payload, key_frame=key, timestamp=ts),
                               group, obj)
            sent.append((group, obj, ts))
            obj += 1
        assert await ladder.flush(10)
        assert ladder.ready.is_set()
    finally:
        ladder.close()
    assert len(sent) == SECONDS * FPS
    for i in range(len(RUNGS)):
        assert [(g, o, ts) for g, o, _, ts, _ in got[i]] == sent
        assert all(key == (o == 0) for _, o, key, _, _ in got[i])
        assert all(_rung_payload_ok(codec, p, key) for _, _, key, _, p in got[i])
        prefix = 'av01.' if codec == 'av1' else 'avc1.'
        assert codec_string(ladder.configs[i]).startswith(prefix)
        assert ladder.wait[i].take()


def _source_catalog(config: bytes) -> Catalog:
    return Catalog(
        tracks=[
            CatalogTrack(name='video', packaging='loc', isLive=True,
                         role='video', renderGroup=1,
                         codec=avcc_codec_string(config), width=320,
                         height=240, framerate=FPS, bitrate=500_000,
                         initRef='v0'),
            CatalogTrack(name='audio', packaging='loc', isLive=True,
                         role='audio', renderGroup=1, codec='pcm-s16',
                         samplerate=48000, channelConfig='1',
                         bitrate=768_000)],
        initDataList=[InitData.from_bytes('v0', config)])


async def _feed(video, audio, frames):
    start = time.time()
    for n, (payload, key) in enumerate(frames):
        ts = int((start + n / FPS) * 1e6)
        await video.send_frame(payload, key_frame=key, timestamp=ts)
        await audio.send_frame(bytes(64), key_frame=True, timestamp=ts)
        await asyncio.sleep(1 / FPS)
    await video.finish()
    await audio.finish()


@pytest.mark.parametrize('codec, use_quic', [('h264', True), ('h264', False), ('av1', True)],
                         ids=['h264-quic', 'h264-wt', 'av1-quic'])
async def test_ingester_publishes_source_and_ladder_in_one_catalog(
        impl, clip, codec, use_quic):
    _needs(impl, codec)
    from aiomoqt.tools import abr_ingest
    from aiomoqt.tools import moq_interop_relay as relay
    relay._announced.clear()
    relay._tracks.clear()
    config, frames = clip
    ns = 'abr/test'
    relay_port, ingest_port = free_udp_port(), free_udp_port()
    scheme = 'moqt' if use_quic else 'https'
    server = relay._build_server('localhost', relay_port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    args = abr_ingest.parse_args([
        '-Q' if use_quic else '-W', '-p', str(ingest_port),
        '--cert', CERT, '--key', KEY, '-N', ns, '--profile', 'cpu', '--codec', codec,
        '--rung', '160x120:200k', '--rung', '96x72:100k',
        '-k', '--draft', '18', '-t', '30', '-i', '1',
        f'{scheme}://localhost:{relay_port}/'])
    ingest = asyncio.ensure_future(abr_ingest.run(args))
    got = defaultdict(dict)
    try:
        await asyncio.sleep(0.5)
        pub_client = MOQTClient('localhost', ingest_port, path='/',
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=18)
        async with pub_client.connect() as pub_session:
            await pub_session.client_session_init()
            pub = MediaPublisher(pub_session, ns, _source_catalog(config))
            video = pub.add_track(LocTrackPublisher(pub_session, ns, 'video',
                                                    config=config))
            audio = pub.add_track(LocTrackPublisher(pub_session, ns, 'audio',
                                                    media_kind='audio'))
            await pub.start(announce_namespace=True, publish_track=False)
            feeder = asyncio.ensure_future(_feed(video, audio, frames))
            await until_published(ns, ['catalog', 'video', 'audio',
                                       'video_120p', 'video_72p'], timeout=15)
            sub_client = MOQTClient('localhost', relay_port, path='/',
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
            async with sub_client.connect() as sub_session:
                await sub_session.client_session_init()

                def on_frame(name, frame, group_id, object_id):
                    got[name][(group_id, object_id)] = (frame.key_frame,
                                                        frame.timestamp)
                sub = MediaSubscriber(sub_session, ns, on_frame=on_frame)
                catalog = await sub.start(timeout=5)
                await feeder
                await asyncio.sleep(1.0)
        assert await asyncio.wait_for(ingest, 10) == 0
    finally:
        if not ingest.done():
            ingest.cancel()
        handle.close()
    assert [t.name for t in catalog.tracks] == ['video', 'video_120p',
                                                'video_72p', 'audio']
    prefix = 'av01.' if codec == 'av1' else 'avc1.'
    assert all(catalog.find(n).codec.startswith(prefix) for n in ('video_120p', 'video_72p'))
    alt = {t.altGroup for t in catalog.tracks if t.role == 'video'}
    assert len(alt) == 1 and None not in alt
    assert got['audio'] and got['video']
    for name in ('video_120p', 'video_72p'):
        rung = got[name]
        assert len(rung) >= FPS, f"{name}: {len(rung)} frames"
        for (g, o), (key, ts) in rung.items():
            assert key == (o == 0)
            if (g, o) in got['video']:
                assert got['video'][(g, o)][1] == ts
        # the subscriptions may start up to a group apart
        assert len(set(rung) & set(got['video'])) >= len(rung) - GOP
