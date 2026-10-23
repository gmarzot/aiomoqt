"""pub_media as the moq-test-tracks reference engine: Opus and H.265
sources, object-to-subgroup and audio group mapping, the datagram size
limit, signal shutdown and --stats-json, end to end through the interop
relay into sub_media --analyze where a session is involved."""
import csv
import os
import threading
import time
from fractions import Fraction

import pytest

from aiomoqt.media.mpegts import TsDemuxer, opus_samples, opus_split
from aiomoqt.media.sources import Mp4Reader
from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_pub_media_frindell import _argv, _through_relay
from aiomoqt.tools import pub_media

_FPS = 30
_BASE_PORT = 16800


def _encode(path, fmt, *, vcodec='libx264', acodec='libopus',
            layout='stereo', seconds=3):
    """A clip as the moq-test-tracks generator makes them: no B-frames,
    a key frame every second, 20 ms Opus or AAC."""
    av = pytest.importorskip('av')
    out = av.open(str(path), 'w', format=fmt)
    if vcodec == 'libx265':
        opts = {'preset': 'ultrafast', 'x265-params':
                f'log-level=error:bframes=0:keyint={_FPS}:'
                f'min-keyint={_FPS}:scenecut=0'}
    else:
        opts = {'g': str(_FPS), 'keyint_min': str(_FPS),
                'sc_threshold': '0', 'bf': '0', 'preset': 'ultrafast'}
    v = out.add_stream(vcodec, rate=_FPS, options=opts)
    v.width, v.height, v.pix_fmt = 160, 96, 'yuv420p'
    if fmt == 'mp4' and vcodec == 'libx265':
        v.codec_tag = 'hvc1'
    a = out.add_stream(acodec, rate=48000, layout=layout)
    for i in range(seconds * _FPS):
        f = av.VideoFrame(160, 96, 'yuv420p')
        for plane, val in zip(f.planes, (16 + (i * 7) % 200, 128, 128)):
            plane.update(bytes([val]) * plane.buffer_size)
        f.pts, f.time_base = i, Fraction(1, _FPS)
        for pkt in v.encode(f):
            out.mux(pkt)
    n = a.codec_context.frame_size or 1024
    for i in range(seconds * 48000 // n):
        f = av.AudioFrame(format=a.codec_context.format.name, layout=layout,
                          samples=n)
        for p in f.planes:
            p.update(b'\x00' * p.buffer_size)
        f.sample_rate, f.pts, f.time_base = 48000, i * n, Fraction(1, 48000)
        for pkt in a.encode(f):
            out.mux(pkt)
    for s in (v, a):
        for pkt in s.encode():
            out.mux(pkt)
    out.close()
    return str(path)


def _paced_fifo(tmp_path, data: bytes, seconds: float) -> str:
    """A FIFO fed with `data` spread over `seconds`, as a live encoder
    feeds pub_media --ts."""
    path = str(tmp_path / 'live.ts')
    os.mkfifo(path)

    def _write():
        step = 188 * 7
        chunks = [data[i:i + step] for i in range(0, len(data), step)]
        try:
            with open(path, 'wb', buffering=0) as fh:
                t0 = time.monotonic()
                for k, chunk in enumerate(chunks):
                    delay = t0 + seconds * k / len(chunks) - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    fh.write(chunk)
        except (BrokenPipeError, OSError):
            pass
    threading.Thread(target=_write, daemon=True).start()
    return path


def _pub_args(monkeypatch, *argv):
    return _argv(monkeypatch, 'pub_media', 'moqt://localhost:1/', *argv)


def _summary(report):
    with open(report) as fh:
        return {r['track']: r for r in csv.DictReader(fh)
                if r['kind'] == 'summary'}


async def _analyze(monkeypatch, tmp_path, port, pub_argv, *, draft=18,
                   use_quic=True, sub_argv=()):
    """Run pub_media through the relay into sub_media --analyze; the
    per-track summary rows."""
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    ns = f'mtt/{port}'
    report = tmp_path / f'analyze-{port}.csv'
    rc = await _through_relay(
        monkeypatch, url, port, draft, use_quic, ['-N', ns, *pub_argv],
        ['-N', ns, '--analyze', '-t', '2', '-i', '1', '--report',
         str(report), *sub_argv])
    assert rc == 0
    return _summary(report)


# -- T1: Opus -----------------------------------------------------------

@pytest.mark.parametrize('layout,channels', [('mono', 1), ('stereo', 2)])
def test_mp4_opus_track_is_read(tmp_path, monkeypatch, layout, channels):
    clip = _encode(tmp_path / 'o.mp4', 'mp4', layout=layout)
    audio = Mp4Reader(clip).audio
    assert audio.codec_string == 'opus' and audio.config is None
    assert (audio.channels, audio.samplerate) == (channels, 48000)
    assert {s.duration for s in list(audio.samples())[:-1]} == {960}
    args = _pub_args(monkeypatch, '--mp4', clip)
    cat = pub_media._build_catalog(args, None, audio)
    entry = cat.find('audio')
    assert (entry.codec, entry.samplerate, entry.channelConfig) == (
        'opus', 48000, str(channels))
    assert entry.initRef is None and not cat.initDataList


@pytest.mark.parametrize('layout,channels', [('mono', 1), ('stereo', 2)])
def test_ts_opus_is_demuxed_one_unit_per_packet(tmp_path, layout,
                                                channels):
    data = open(_encode(tmp_path / 'o.ts', 'mpegts', layout=layout),
                'rb').read()
    d = TsDemuxer()
    units = []
    for i in range(0, len(data), 1316):
        units += d.feed(data[i:i + 1316])
    units += d.close()
    audio = [u for u in units if u.kind == 'audio']
    assert d.audio_codec == 'opus' and d.audio_channels == channels
    assert d.audio_samplerate == 48000 and d.audio_asc is None
    assert len(audio) >= 145
    assert {b.pts - a.pts for a, b in zip(audio, audio[1:])} == {1800}
    assert all(opus_samples(u.payload) == 960 for u in audio)
    view = pub_media._LiveAudio(d)
    assert (view.codec_string, view.channels) == ('opus', channels)


def test_opus_control_headers_are_stripped():
    pkt = bytes([0xFC]) + b'\x11' * 300
    trimmed = bytes([0x7F, 0xF0, 0xFF, len(pkt) - 255]) + b'\x01\x38' + pkt
    plain = bytes([0x7F, 0xE0, 0x05]) + bytes([0xFC]) + b'\x22' * 4
    assert opus_split(trimmed + plain) == [pkt, bytes([0xFC]) + b'\x22' * 4]
    assert opus_samples(bytes([0xFC])) == 960       # CELT FB 20 ms
    assert opus_samples(bytes([0x0B, 0x03])) == 2880  # SILK 60 ms, 2 frames


@requires_certs
@pytest.mark.parametrize('source,packaging,layout,use_quic', [
    ('mp4', 'loc', 'mono', True),
    ('mp4', 'cmaf', 'stereo', False),
    ('mp4', 'locmaf', 'mono', True),
    ('ts', 'loc', 'stereo', True),
    ('ts', 'loc', 'mono', False),
])
async def test_opus_reaches_analyze_at_50_objects_per_second(
        tmp_path, monkeypatch, capsys, source, packaging, layout, use_quic):
    port = _BASE_PORT + 2 * ['mp4-loc', 'mp4-cmaf', 'mp4-locmaf', 'ts-loc'
                             ].index(f'{source}-{packaging}') + (not use_quic)
    if source == 'mp4':
        clip = _encode(tmp_path / 'o.mp4', 'mp4', layout=layout)
        pub = ['--mp4', clip, '--loop', '--packaging', packaging]
    else:
        data = open(_encode(tmp_path / 'o.ts', 'mpegts', layout=layout,
                            seconds=6), 'rb').read()
        pub = ['--ts', _paced_fifo(tmp_path, data, 6)]
    rows = await _analyze(monkeypatch, tmp_path, port,
                          [*pub, '--timestamp-mapping'], use_quic=use_quic,
                          sub_argv=['--show-catalog'])
    catalog = capsys.readouterr().err
    assert '"codec": "opus"' in catalog
    assert f'"channelConfig": "{1 if layout == "mono" else 2}"' in catalog
    audio = rows['audio']
    assert int(audio['objects']) > 50, audio
    assert 40 <= float(audio['obj_rate']) <= 60, audio
    # LOCMAF audio defaults to ~1 s groups.
    assert float(audio['gop_p50']) == (50 if packaging == 'locmaf' else 1)
    assert int(rows['video']['objects']) > 0
