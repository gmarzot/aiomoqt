"""Media sources: pcm synth determinism, minimal-BMFF reader against a
synthetic in-test MP4, Annex-B helpers."""
import struct

import pytest

from aiomoqt.media.sources import (
    Mp4AvcReader, Mp4Error, annexb_is_keyframe, pcm_tone_frames,
    split_annexb,
)


def test_pcm_tone_frames():
    frames = list(pcm_tone_frames(duration_s=0.1, samplerate=48000,
                                  channels=2, frame_ms=20))
    assert len(frames) == 5
    payload, ts = frames[1]
    # 20ms @ 48k stereo s16 = 960 samples * 2ch * 2B
    assert len(payload) == 960 * 2 * 2
    assert ts == 20_000
    # Values are bounded s16 and not all zero.
    vals = struct.unpack(f'<{len(payload) // 2}h', payload)
    assert max(vals) > 0 and min(vals) < 0
    assert max(map(abs, vals)) <= 32767 // 4 + 1


# -- synthetic mp4 ----------------------------------------------------

def _box(btype: bytes, *payload: bytes) -> bytes:
    data = b''.join(payload)
    return struct.pack('>I', 8 + len(data)) + btype + data


_SAMPLES = [b'\x00\x00\x00\x04IDR0', b'\x00\x00\x00\x02P1',
            b'\x00\x00\x00\x03P22']
_AVCC = b'\x01\x64\x00\x1f\xff\xe1\x00\x02\x67\x64'
_TIMESCALE = 90000
_DELTA = 3000  # 30 fps


def _mp4() -> bytes:
    ftyp = _box(b'ftyp', b'isom\x00\x00\x02\x00isomiso2avc1')
    mdat = _box(b'mdat', b''.join(_SAMPLES))
    chunk_offset = len(ftyp) + 8  # first sample, mdat-first layout

    entry = _box(
        b'avc1',
        b'\x00' * 24,                      # reserved/pre-defined
        struct.pack('>HH', 640, 360),      # width, height
        b'\x00' * 50,                      # resolution..pre_defined
        _box(b'avcC', _AVCC),
    )
    stbl = _box(
        b'stbl',
        _box(b'stsd', struct.pack('>II', 0, 1), entry),
        _box(b'stts', struct.pack('>II', 0, 1),
             struct.pack('>II', len(_SAMPLES), _DELTA)),
        _box(b'stss', struct.pack('>II', 0, 2), struct.pack('>II', 1, 3)),
        _box(b'stsc', struct.pack('>II', 0, 1),
             struct.pack('>III', 1, len(_SAMPLES), 1)),
        _box(b'stsz', struct.pack('>III', 0, 0, len(_SAMPLES)),
             b''.join(struct.pack('>I', len(s)) for s in _SAMPLES)),
        _box(b'stco', struct.pack('>II', 0, 1),
             struct.pack('>I', chunk_offset)),
    )
    moov = _box(
        b'moov',
        _box(b'trak', _box(
            b'mdia',
            _box(b'mdhd', struct.pack('>BxxxIIIIHH', 0, 0, 0,
                                      _TIMESCALE, 0, 0, 0)),
            _box(b'minf', stbl),
        )),
    )
    return ftyp + mdat + moov


def test_mp4_reader(tmp_path):
    path = tmp_path / "t.mp4"
    path.write_bytes(_mp4())
    r = Mp4AvcReader(str(path))
    assert r.avcc == _AVCC
    assert (r.width, r.height) == (640, 360)
    assert r.timescale == _TIMESCALE
    assert r.fps == 30.0
    samples = list(r.samples())
    assert [s.payload for s in samples] == _SAMPLES
    assert [s.key_frame for s in samples] == [True, False, True]
    assert [s.timestamp_us for s in samples] == [0, 33333, 66666]


_AV1C = bytes([0x81, (0 << 5) | 8, 0x40])  # profile 0, level 8, 10-bit


def test_av1_track_and_ivf(tmp_path):
    from aiomoqt.media.sources import (
        IvfWriter, Mp4Reader, av1c_codec_string,
    )
    assert av1c_codec_string(_AV1C) == "av01.0.08M.10"
    # Same synthetic mp4 with the sample entry swapped to av01/av1C.
    data = _mp4().replace(b'avc1', b'av01').replace(
        _box(b'avcC', _AVCC), _box(b'av1C', _AV1C)
        + b'\x00' * (len(_box(b'avcC', _AVCC)) - len(_box(b'av1C', _AV1C))))
    path = tmp_path / "t-av1.mp4"
    path.write_bytes(data)
    r = Mp4Reader(str(path))
    assert r.video.codec_string == "av01.0.08M.10"
    assert r.video.config is None  # AV1: no decoder description
    assert [s.payload for s in r.video.samples()] == _SAMPLES

    out = tmp_path / "t.ivf"
    w = IvfWriter(open(out, 'wb'), 640, 360, 24.0)
    w.add(b'obu-frame-0')
    w.add(b'obu-frame-1')
    w.close()
    d = out.read_bytes()
    assert d[:4] == b'DKIF' and d[8:12] == b'AV01'
    assert struct.unpack_from('<I', d, 24)[0] == 2  # patched frame count
    assert struct.unpack_from('<I', d, 32)[0] == len(b'obu-frame-0')


def test_mp4_reader_rejects_non_mp4(tmp_path):
    path = tmp_path / "bad.mp4"
    path.write_bytes(b'\x00' * 64)
    with pytest.raises(Mp4Error):
        Mp4AvcReader(str(path))


# -- Annex-B ----------------------------------------------------------

def test_split_annexb_and_keyframe():
    sps, pps, idr, p = b'\x67\x64', b'\x68\xee', b'\x65\x88', b'\x41\x9a'
    au_key = (b'\x00\x00\x00\x01' + sps + b'\x00\x00\x00\x01' + pps
              + b'\x00\x00\x01' + idr)
    au_p = b'\x00\x00\x00\x01' + p
    assert split_annexb(au_key) == [sps, pps, idr]
    assert annexb_is_keyframe(au_key)
    assert not annexb_is_keyframe(au_p)
    assert split_annexb(b'') == []
