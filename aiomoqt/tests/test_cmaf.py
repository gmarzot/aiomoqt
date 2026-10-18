"""CMAF chunker: init-segment structure, chunk framing, tfdt/mfhd
accumulation — against the synthetic in-test MP4."""
import struct

import pytest

from aiomoqt.media.cmaf import (
    CmafChunker, _box, _full, chunk_decode_time, init_codec_string,
    set_chunk_timing, strip_edit_lists,
)
from aiomoqt.media.sources import Mp4AvcReader, Mp4Error, _boxes, _find

from .test_media_sources import _AVCC, _mp4


def _reader(tmp_path):
    path = tmp_path / "t.mp4"
    path.write_bytes(_mp4())
    return Mp4AvcReader(str(path))


def test_init_segment_structure(tmp_path):
    ck = CmafChunker(_reader(tmp_path))
    seg = ck.init_segment()
    tops = [t for t, _, _ in _boxes(seg, 0, len(seg))]
    assert tops == [b'ftyp', b'moov']
    assert _find(seg, 0, len(seg), b'moov', b'mvex', b'trex') is not None
    hdlr = _find(seg, 0, len(seg), b'moov', b'trak', b'mdia', b'hdlr')
    assert seg[hdlr[0] + 8:hdlr[0] + 12] == b'vide'
    stsd = _find(seg, 0, len(seg), b'moov', b'trak', b'mdia', b'minf',
                 b'stbl', b'stsd')
    # source avc1 sample entry embedded verbatim (incl. avcC)
    assert _AVCC in seg[stsd[0]:stsd[1]]
    mdhd = _find(seg, 0, len(seg), b'moov', b'trak', b'mdia', b'mdhd')
    assert struct.unpack_from('>I', seg, mdhd[0] + 12)[0] == ck.timescale


def test_chunk_framing_and_accumulation(tmp_path):
    ck = CmafChunker(_reader(tmp_path))
    samples = list(_reader(tmp_path).samples())
    chunks = [ck.chunk(s.payload, s.duration, s.key_frame)
              for s in samples]
    for i, (chunk, s) in enumerate(zip(chunks, samples)):
        tops = list(_boxes(chunk, 0, len(chunk)))
        assert [t for t, _, _ in tops] == [b'moof', b'mdat']
        mdat = tops[1]
        assert chunk[mdat[1]:mdat[2]] == s.payload
        mfhd = _find(chunk, 0, len(chunk), b'moof', b'mfhd')
        assert struct.unpack_from('>I', chunk, mfhd[0] + 4)[0] == i + 1
        tfdt = _find(chunk, 0, len(chunk), b'moof', b'traf', b'tfdt')
        assert (struct.unpack_from('>Q', chunk, tfdt[0] + 4)[0]
                == sum(x.duration for x in samples[:i]))
        trun = _find(chunk, 0, len(chunk), b'moof', b'traf', b'trun')
        count, offset, dur, size, flags = struct.unpack_from(
            '>IiIII', chunk, trun[0] + 4)
        assert (count, dur, size) == (1, s.duration, len(s.payload))
        # data_offset lands exactly on the mdat payload
        assert chunk[offset:offset + size] == s.payload
        assert bool(flags == 0x02000000) == s.key_frame


def test_decode_time_override(tmp_path):
    ck = CmafChunker(_reader(tmp_path))
    ck.chunk(b'x', 3000)
    chunk = ck.chunk(b'y', 3000, decode_time=90_000)
    tfdt = _find(chunk, 0, len(chunk), b'moof', b'traf', b'tfdt')
    assert struct.unpack_from('>Q', chunk, tfdt[0] + 4)[0] == 90_000


def _chunk_with_cto(cto: int) -> bytearray:
    """One-sample chunk whose trun (version 1) carries data offset,
    duration, size, flags and composition offset."""
    trun = _full(b'trun', 1, 0x000F01,
                 struct.pack('>IiIIIi', 1, 0, 512, 1, 0, cto))
    moof = _box(b'moof', _full(b'mfhd', 0, 0, struct.pack('>I', 1)),
                _box(b'traf', _full(b'tfhd', 0, 0x020000, struct.pack('>I', 1)),
                     _full(b'tfdt', 1, 0, struct.pack('>Q', 0)), trun))
    return bytearray(moof + _box(b'mdat', b'x'))


def _cto(chunk) -> int:
    trun = _find(chunk, 0, len(chunk), b'moof', b'traf', b'trun')
    return struct.unpack_from('>i', chunk, trun[1] - 4)[0]


def test_set_chunk_timing():
    chunk = _chunk_with_cto(-7)
    set_chunk_timing(chunk, 1 << 40, 3000)
    assert chunk_decode_time(bytes(chunk)) == 1 << 40
    assert _cto(chunk) == 3000
    set_chunk_timing(chunk, 5)
    assert (chunk_decode_time(bytes(chunk)), _cto(chunk)) == (5, 3000)


def test_set_chunk_timing_without_cto_field(tmp_path):
    chunk = bytearray(CmafChunker(_reader(tmp_path)).chunk(b'x', 3000))
    set_chunk_timing(chunk, 42, 0)
    assert chunk_decode_time(bytes(chunk)) == 42
    with pytest.raises(Mp4Error):
        set_chunk_timing(chunk, 42, 1)


def test_strip_edit_lists(tmp_path):
    init = CmafChunker(_reader(tmp_path)).init_segment()
    assert strip_edit_lists(init) == init
    moov = _find(init, 0, len(init), b'moov')
    trak = _find(init, *moov, b'trak')
    edts = _box(b'edts', _full(b'elst', 0, 0, struct.pack('>IIiI', 1, 0, 1024,
                                                          0x10000)))
    pos = trak[0]  # edts first inside trak
    patched = bytearray(init[:pos] + edts + init[pos:])
    for span in (moov, trak):  # grow both containers by the inserted box
        size = struct.unpack_from('>I', patched, span[0] - 8)[0]
        struct.pack_into('>I', patched, span[0] - 8, size + len(edts))
    assert strip_edit_lists(bytes(patched)) == init


def test_init_codec_string(tmp_path):
    init = CmafChunker(_reader(tmp_path)).init_segment()
    assert init_codec_string(init) == "avc1.64001F"
    assert init_codec_string(init.replace(b'avc1', b'avc3', 1)) == "avc3.64001F"
    assert init_codec_string(b'') is None
    assert init_codec_string(init[:40]) is None
