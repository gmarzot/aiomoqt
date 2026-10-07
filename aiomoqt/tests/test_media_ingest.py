"""Media ingest (PyAV): pub_media --input flags, rendition selection,
CMAF chunk timing and grouping, and pub_media --input end to end through
a relay. Fixtures are HLS ladders written by PyAV, served over local
HTTP; tests needing PyAV skip without the `media` extra."""
import asyncio
import functools
import http.server
import io
import os
import sys
import threading
from fractions import Fraction
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import MediaSubscriber
from aiomoqt.media.ingest import INSTALL_HINT, Ingest, load_av
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media

_BASE_PORT = 15950
_RUNGS = ((320, 180, 400_000), (160, 90, 150_000))
_SECONDS = 4
_FPS = 30


def _parse(monkeypatch, *argv, url='moqt://localhost:4433/'):
    monkeypatch.setattr(sys, 'argv', ['pub_media', url, *argv])
    return pub_media.parse_args()


def test_input_implies_cmaf_and_best(monkeypatch):
    args = _parse(monkeypatch, '--input', 'x.m3u8')
    assert (args.packaging, args.rendition) == ('cmaf', 'best')
    assert _parse(monkeypatch).packaging == 'loc'


@pytest.mark.parametrize('argv', [
    ('--rendition', 'all'),
    ('--input', 'x.m3u8', '--packaging', 'loc'),
    ('--input', 'x.m3u8', '--loop'),
    ('--input', 'x.m3u8', '--mp4', 'y.mp4'),
    ('--packaging', 'cmaf'),
])
def test_input_flag_conflicts(monkeypatch, argv):
    with pytest.raises(SystemExit):
        _parse(monkeypatch, *argv)


def test_load_av_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, 'av', None)
    with pytest.raises(ImportError, match=r"aiomoqt\[media\]"):
        load_av()


async def test_input_without_pyav_exits_with_the_install_hint(monkeypatch):
    args = _parse(monkeypatch, '--input', 'http://127.0.0.1:9/x.m3u8')
    monkeypatch.setitem(sys.modules, 'av', None)
    with pytest.raises(SystemExit) as e:
        await pub_media._open_ingest(args)
    assert INSTALL_HINT in str(e.value)


# -- fixtures -----------------------------------------------------------

def _write_ladder(root: str, segment_type: str) -> None:
    """A two-rendition HLS ladder (H.264 + AAC muxed per variant, 1 s
    segments, B-frames) plus its master playlist."""
    av = load_av()
    variants = []
    for idx, (w, h, bitrate) in enumerate(_RUNGS):
        name = f"r{idx}"
        ext = 'm4s' if segment_type == 'fmp4' else 'ts'
        opts = {'hls_time': '1', 'hls_playlist_type': 'vod',
                'hls_segment_type': segment_type,
                'hls_segment_filename': os.path.join(root, f"{name}_%03d.{ext}")}
        if segment_type == 'fmp4':
            opts['hls_fmp4_init_filename'] = f"{name}_init.mp4"
        out = av.open(os.path.join(root, f"{name}.m3u8"), 'w', format='hls',
                      options=opts)
        v = out.add_stream('libx264', rate=_FPS, options={
            'g': str(_FPS), 'keyint_min': str(_FPS), 'sc_threshold': '0',
            'bf': '2', 'preset': 'ultrafast'})
        v.width, v.height, v.pix_fmt, v.bit_rate = w, h, 'yuv420p', bitrate
        a = out.add_stream('aac', rate=48000, layout='mono')
        for i in range(_SECONDS * _FPS):
            f = av.VideoFrame(w, h, 'yuv420p')
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
        variants.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bitrate + 64_000},"
                        f"RESOLUTION={w}x{h}\n{name}.m3u8")
    with open(os.path.join(root, 'master.m3u8'), 'w') as fh:
        fh.write("#EXTM3U\n#EXT-X-VERSION:7\n" + "\n".join(variants) + "\n")


@pytest.fixture(scope='module')
def ladders(tmp_path_factory):
    pytest.importorskip('av')
    out = {}
    for segment_type in ('fmp4', 'mpegts'):
        root = tmp_path_factory.mktemp(segment_type)
        _write_ladder(str(root), segment_type)
        out[segment_type] = root
    return out


class _Origin:
    """Static HTTP origin over a ladder; logs request paths."""

    def __init__(self, root):
        hits = self.hits = []

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                super().do_GET()

            def log_message(self, *args):
                pass

        self._srv = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}/master.m3u8"

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture(params=['fmp4', 'mpegts'])
def origin(request, ladders):
    o = _Origin(ladders[request.param])
    yield o
    o.close()


async def _open(url, rendition):
    return await asyncio.get_running_loop().run_in_executor(
        None, functools.partial(Ingest, url, rendition))


async def _drain(ingest):
    chunks = {}
    async for c in ingest.chunks():
        chunks.setdefault(c.track, []).append(c)
    return chunks


def _source_pts(url, ingest):
    """Source pts (seconds) per ingested track in decode order, video
    from its first key frame."""
    av = load_av()
    names = {t.stream.index: t.name for t in ingest.tracks}
    out = {n: [] for n in names.values()}
    with av.open(url) as c:
        for pkt in c.demux(*[c.streams[i] for i in names]):
            name = names[pkt.stream.index]
            if pkt.dts is None or (name != 'audio' and not out[name]
                                   and not pkt.is_keyframe):
                continue
            out[name].append(pkt.pts * Fraction(pkt.time_base))
    return out


# -- ingest -------------------------------------------------------------

async def test_best_fetches_only_the_top_rendition(origin):
    ingest = await _open(origin.url, 'best')
    origin.hits.clear()
    chunks = await _drain(ingest)
    video, audio = ingest.tracks
    assert (video.name, audio.name) == ('video', 'audio')
    assert (video.width, video.height) == _RUNGS[0][:2]
    assert video.codec.startswith('avc1.') and audio.codec == 'mp4a.40.2'
    assert {h.lstrip('/').split('_')[0] for h in origin.hits} == {'r0'}
    assert len(chunks['video']) == _SECONDS * _FPS


async def test_all_keeps_groups_time_aligned_across_renditions(origin):
    ingest = await _open(origin.url, 'all')
    chunks = await _drain(ingest)
    assert [t.name for t in ingest.tracks] == ['video-180p', 'video-90p', 'audio']
    starts = [[(c.group_id, c.time_us) for c in chunks[n] if c.key]
              for n in ('video-180p', 'video-90p')]
    assert starts[0] == starts[1]
    assert [g for g, _ in starts[0]] == list(range(_SECONDS))
    audio = chunks['audio']
    assert all(c.key for c in audio)
    assert [c.group_id for c in audio] == list(range(len(audio)))


async def test_chunks_carry_the_source_timeline(origin):
    """init + chunks demux to exactly the source pts on every track: the
    same timeline for audio and video, B-frame offsets intact."""
    av = load_av()
    ingest = await _open(origin.url, 'best')
    expected = _source_pts(origin.url, ingest)
    chunks = await _drain(ingest)
    for track in ingest.tracks:
        assert b'edts' not in track.init
        data = track.init + b''.join(c.payload for c in chunks[track.name])
        with av.open(io.BytesIO(data)) as c:
            s = c.streams[0]
            pts = [p.pts * Fraction(p.time_base) for p in c.demux(s)
                   if p.dts is not None]
        with av.open(io.BytesIO(data)) as c:
            decoded = sum(1 for _ in c.decode(c.streams[0]))
        assert pts == expected[track.name], track.name
        assert decoded >= len(pts) - 2, track.name  # decoder may hold priming


async def test_ingest_catalog_alternate_group(origin):
    args = SimpleNamespace(target_latency=None)
    for rendition, alt in (('best', None), ('all', 1)):
        ingest = await _open(origin.url, rendition)
        cat = pub_media._build_ingest_catalog(args, ingest)
        ingest.close()
        assert cat.validate() == []
        for t in ingest.tracks:
            entry = cat.find(t.name)
            assert entry.packaging == 'cmaf' and entry.codec == t.codec
            assert cat.resolve_init(entry) == t.init
            assert entry.altGroup == (alt if t.kind == 'video' else None)


# -- end to end ---------------------------------------------------------

@requires_certs
async def test_pub_media_input_through_a_relay(ladders, monkeypatch):
    av = load_av()
    port = _BASE_PORT + 1
    ns = 'ingest/e2e'
    origin = _Origin(ladders['fmp4'])
    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server('localhost', port, CERT, KEY,
                                       use_quic=True, draft=18).serve()
    args = _parse(monkeypatch, '-N', ns, '--input', origin.url,
                  '--rendition', 'all', '--pub-ns', '-k', '-t', str(_SECONDS),
                  '--stats', '0', '--catalog-interval', '0.5',
                  url=f'moqt://localhost:{port}/')
    got = {}
    pub = asyncio.create_task(pub_media.run(args))
    try:
        for _ in range(200):
            if relay._announced or pub.done():
                break
            await asyncio.sleep(0.05)
        assert relay._announced, "publisher never announced"
        client = MOQTClient('localhost', port, path='/', use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            sub = MediaSubscriber(
                session, ns,
                on_frame=lambda name, f, gid, oid:
                    got.setdefault(name, []).append((gid, oid, f.payload)))
            catalog = await sub.start()
            await asyncio.wait_for(pub, 30)
    finally:
        if not pub.done():
            pub.cancel()
        handle.close()
        origin.close()
        relay._announced.clear()
        relay._tracks.clear()
    videos = [t for t in catalog.tracks if t.role == 'video']
    assert [t.altGroup for t in videos] == [1, 1]
    groups = {}
    for t in videos:
        objs = got.get(t.name, [])
        groups[t.name] = {g for g, o, _ in objs if o == 0}
        assert groups[t.name], f"no complete group on {t.name}"
        first = min(groups[t.name])
        data = catalog.resolve_init(t) + b''.join(
            p for g, _, p in sorted(objs) if g >= first)
        with av.open(io.BytesIO(data)) as c:
            assert sum(1 for _ in c.decode(c.streams[0])) > 0
    assert set.intersection(*groups.values())
    assert got.get('audio')
