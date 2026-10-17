"""LOCMAF end to end: pub_media --packaging locmaf through the interop
relay into sub_media, over raw QUIC and WebTransport, both tracks on one
session. Optional peer runs against a local moqlivemock build (set
MOQLIVEMOCK_DIR to its checkout, with bin/mlmpub and bin/mlmsub) skip
without it."""
import asyncio
import io
import os
import re
import struct
import subprocess
import sys
from fractions import Fraction

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import LocmafTrackPublisher, MediaSubscriber
from aiomoqt.media.locmaf import TrackContext, build_chunk, parse_chunk
from aiomoqt.media.sources import Mp4Reader
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media, sub_media

pytestmark = requires_certs

_BASE_PORT = 16500
_SECONDS = 4
_FPS = 30


@pytest.fixture(scope='module')
def clip(tmp_path_factory):
    """H.264 without B-frames (1 s GOP) and AAC, as pub_media --mp4 wants."""
    av = pytest.importorskip('av')
    path = str(tmp_path_factory.mktemp('locmaf') / 'clip.mp4')
    out = av.open(path, 'w')
    v = out.add_stream('libx264', rate=_FPS, options={
        'g': str(_FPS), 'keyint_min': str(_FPS), 'sc_threshold': '0',
        'bf': '0', 'preset': 'ultrafast'})
    v.width, v.height, v.pix_fmt = 160, 96, 'yuv420p'
    a = out.add_stream('aac', rate=48000, layout='mono')
    for i in range(_SECONDS * _FPS):
        f = av.VideoFrame(160, 96, 'yuv420p')
        for plane, val in zip(f.planes, (16 + (i * 7) % 200, 128, 128)):
            plane.update(bytes([val]) * plane.buffer_size)
        f.pts, f.time_base = i, Fraction(1, _FPS)
        for pkt in v.encode(f):
            out.mux(pkt)
    for i in range(_SECONDS * 48000 // 1024):
        f = av.AudioFrame(format='fltp', layout='mono', samples=1024)
        f.planes[0].update(b'\x00' * f.planes[0].buffer_size)
        f.sample_rate, f.pts, f.time_base = 48000, i * 1024, Fraction(1, 48000)
        for pkt in a.encode(f):
            out.mux(pkt)
    for stream in (v, a):
        for pkt in stream.encode():
            out.mux(pkt)
    out.close()
    return path


def _source_samples(path):
    """{track: {decode time: sample bytes}} in track timescale units, the
    timeline CmafChunker numbers its chunks on."""
    reader = Mp4Reader(path)
    out = {}
    for name, track in (('video', reader.video), ('audio', reader.audio)):
        dts, samples = 0, {}
        for s in track.samples():
            samples[dts] = bytes(s.payload)
            dts += s.duration
        out[name] = samples
    return out


def _chunks(data: bytes):
    """(init, [chunk]) of an fMP4 file written chunk by chunk."""
    pos, init, chunks = 0, b'', []
    while pos < len(data):
        size, btype = struct.unpack_from('>I4s', data, pos)
        if btype in (b'ftyp', b'moov'):
            init += data[pos:pos + size]
        elif btype == b'moof':
            mdat = struct.unpack_from('>I', data, pos + size)[0]
            chunks.append(data[pos:pos + size + mdat])
            size += mdat
        pos += size
    return init, chunks


def _summary(text):
    return {m[0]: (int(m[1]), int(m[2])) for m in re.findall(
        r"^  (\S+): (\d+) frames, (\d+) rejected$", text, re.MULTILINE)}


def _argv(monkeypatch, tool, *argv):
    monkeypatch.setattr(sys, 'argv', [tool, *argv])
    return (pub_media if tool == 'pub_media' else sub_media).parse_args()


@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_pub_media_locmaf_through_a_relay(clip, tmp_path, monkeypatch,
                                                capsys, use_quic):
    av = pytest.importorskip('av')
    ns = 'locmaf/e2e'
    port = _BASE_PORT + (0 if use_quic else 1)
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    published = []

    class _Recording(LocmafTrackPublisher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            published.append(self)
    monkeypatch.setattr(pub_media, 'LocmafTrackPublisher', _Recording)

    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server('localhost', port, CERT, KEY,
                                       use_quic=use_quic, draft=18).serve()
    pub_args = _argv(monkeypatch, 'pub_media', url, '-N', ns, '--mp4', clip,
                     '--packaging', 'locmaf', '--pub-ns', '-k', '--draft',
                     '18', '-t', str(_SECONDS), '--stats', '0',
                     '--catalog-interval', '0.5')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-N', ns, '--packaging',
                     'locmaf', '--out', str(tmp_path), '-k', '--draft', '18',
                     '-t', '2')
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
    for name, (frames, rejected) in summary.items():
        assert frames > 0 and rejected == 0, (name, frames, rejected)
    assert {t.trackname for t in published} == {'video', 'audio'}
    assert all(t.raw_fallbacks == 0 for t in published)

    source = _source_samples(clip)
    for name in ('video', 'audio'):
        data = (tmp_path / f'{name}.mp4').read_bytes()
        init, chunks = _chunks(data)
        ctx = TrackContext.from_init(init)
        assert len(chunks) == summary[name][0]
        for chunk in chunks:
            eff = parse_chunk(chunk, ctx)
            assert build_chunk(eff, ctx) == chunk      # canonical as received
            dts = eff.bmdt
            for size, dur in zip(eff.sizes, eff.durations):
                assert source[name][dts] == eff.mdat[:size], (name, dts)
                eff.mdat, dts = eff.mdat[size:], dts + dur
        with av.open(io.BytesIO(data)) as c:
            assert sum(1 for _ in c.decode(c.streams[0])) > 0, name


# -- local moqlivemock peer (optional) ---------------------------------

_MLM = os.environ.get('MOQLIVEMOCK_DIR')
_MLM_BIN = os.path.join(_MLM or '', 'bin')
requires_mlm = pytest.mark.skipif(
    not (_MLM and os.access(os.path.join(_MLM_BIN, 'mlmpub'), os.X_OK)
         and os.access(os.path.join(_MLM_BIN, 'mlmsub'), os.X_OK)),
    reason='set MOQLIVEMOCK_DIR to a moqlivemock checkout with bin/mlmpub '
           'and bin/mlmsub')
_KID = '00112233445566778899aabbccddeeff'


async def _connect(port, tries=50):
    """A d14 raw-QUIC session to a local peer, retried while it starts.
    d14: moqtransport 0.10.0 parses a d16 FETCH with the d14 layout."""
    for _ in range(tries):
        client = MOQTClient('localhost', port, path='/', use_quic=True,
                            verify_tls=False, supported_drafts=14)
        ctx = client.connect()
        try:
            session = await ctx.__aenter__()
            await session.client_session_init()
            return ctx, session
        except Exception:
            await asyncio.sleep(0.2)
    raise AssertionError(f"no peer on port {port}")


@requires_mlm
@pytest.mark.parametrize('namespace', ['cmsf\\/clear', 'cmsf\\/eccp-cbcs'],
                         ids=['clear', 'cbcs'])
async def test_moqlivemock_locmaf_twins_rebuild_their_cmaf_chunks(
        tmp_path, namespace):
    """Every X_locmaf object rebuilds to the canonical form of the
    same-numbered object of its CMAF twin X, encrypted (senc, saiz, saio)
    and audio batched four samples per object included."""
    port = _BASE_PORT + 20 + (namespace.endswith('cbcs'))
    proc = subprocess.Popen(
        [os.path.join(_MLM_BIN, 'mlmpub'), '-addr', f'127.0.0.1:{port}',
         '-cert', CERT, '-key', KEY,
         '-asset', os.path.join(_MLM, 'assets', 'test10s'),
         '-audiobatch', '4', '-scheme', 'cbcs', '-kid', _KID,
         '-cenckey', _KID, '-iv', _KID, '-laurl', 'http://localhost/ck'],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    suffix = '_eccp' if namespace.endswith('cbcs') else ''
    pairs = ('video_400kbps_avc' + suffix,
             'audio_monotonic_128kbps_aac' + suffix)
    got = {}
    try:
        ctx, session = await _connect(port)
        try:
            # The catalog names the one-element namespace unescaped.
            sub = MediaSubscriber(
                session, namespace,
                on_catalog=lambda c: [setattr(t, 'namespace', None)
                                      for t in c.tracks],
                track_filter=lambda t: t.name.removesuffix('_locmaf') in pairs,
                on_frame=lambda n, f, g, o:
                    got.setdefault(n, {}).__setitem__((g, o), f.payload))
            catalog = await sub.start()
            await asyncio.sleep(4)
        finally:
            await ctx.__aexit__(None, None, None)
    finally:
        proc.terminate()
        proc.wait(10)
    for name in pairs:
        twin = name + '_locmaf'
        assert sub.tracks[twin].objects_rejected == 0, twin
        tctx = TrackContext.from_init(catalog.resolve_init(catalog.find(name)))
        both = set(got.get(name, {})) & set(got.get(twin, {}))
        assert len(both) >= 3, (name, len(both))
        for key in both:
            eff = parse_chunk(got[name][key], tctx)
            assert eff is not None, (name, key)
            assert build_chunk(eff, tctx) == got[twin][key], (name, key)
            if suffix and name.startswith('video'):     # senc/saiz/saio rebuilt
                assert b'senc' in got[twin][key], (name, key)


@requires_mlm
async def test_mlmsub_plays_pub_media_locmaf(clip, tmp_path, monkeypatch):
    """mlmsub (Eyevinn's LOCMAF receiver) muxes pub_media's LOCMAF into a
    playable mp4. d14: moqtransport 0.10.0 stops after one object at d16."""
    av = pytest.importorskip('av')
    port = _BASE_PORT + 30
    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server('localhost', port, CERT, KEY,
                                       use_quic=True, draft=14).serve()
    pub_args = _argv(monkeypatch, 'pub_media', f'moqt://localhost:{port}/',
                     '-N', 'locmafpeer', '--mp4', clip, '--packaging',
                     'locmaf', '--pub-ns', '-k', '--draft', '14', '-t',
                     str(_SECONDS), '--stats', '0', '--catalog-interval', '0.5')
    pub = asyncio.create_task(pub_media.run(pub_args))
    mux = tmp_path / 'mux.mp4'
    try:
        for _ in range(200):
            if relay._announced or pub.done():
                break
            await asyncio.sleep(0.05)
        proc = await asyncio.create_subprocess_exec(
            os.path.join(_MLM_BIN, 'mlmsub'), '-addr', f'localhost:{port}',
            '-draft', '14', '-namespace', 'locmafpeer', '-catalog-mode',
            'subscribe', '-videoname', 'video', '-audioname', 'audio',
            '-muxout', str(mux), '-duration', str(_SECONDS + 2),
            cwd=tmp_path, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), _SECONDS + 20)
        await asyncio.wait_for(pub, 30)
    finally:
        if not pub.done():
            pub.cancel()
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()
    decoded = {}
    with av.open(str(mux)) as c:
        for packet in c.demux():
            for _ in packet.decode():
                decoded[packet.stream.type] = decoded.get(packet.stream.type, 0) + 1
    assert decoded.get('video', 0) > 0 and decoded.get('audio', 0) > 0
