"""CMAF chunker: init-segment structure, chunk framing, tfdt/mfhd
accumulation — against the synthetic in-test MP4."""
import struct

from aiomoqt.media.cmaf import CmafChunker
from aiomoqt.media.sources import Mp4AvcReader, _boxes, _find

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
