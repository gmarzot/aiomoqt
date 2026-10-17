"""CMAF packaging for CMSF (draft-ietf-moq-cmsf) — init segments and
moof+mdat chunks built from progressive-mp4 sample tables.

Mapping (cmsf §3.3/§3.4): each MOQT Object is one CMAF chunk
(moof+mdat, single track, one sample — lowest latency); Groups begin
at a SAP-1 chunk and align with fragment boundaries. The CMAF header
(ftyp+moov) rides the catalog initDataList; tfdt carries cumulative
decode time in the track timescale.
"""
from __future__ import annotations

import struct
from typing import Optional

from .sources import (
    Mp4Error, _boxes, _find, av1c_codec_string, avcc_codec_string, esds_asc,
    hvcc_codec_string,
)


def _box(btype: bytes, *payload: bytes) -> bytes:
    data = b''.join(payload)
    return struct.pack('>I4s', 8 + len(data), btype) + data


def _full(btype: bytes, version: int, flags: int, *payload: bytes) -> bytes:
    return _box(btype, struct.pack('>B3s', version,
                                   flags.to_bytes(3, 'big')), *payload)


_UNITY_MATRIX = struct.pack('>9i', 0x10000, 0, 0, 0, 0x10000, 0,
                            0, 0, 0x40000000)

# sample_flags (ISO 14496-12 §8.8.3.1)
_FLAG_SYNC = 0x02000000      # sample_depends_on=2 (I-frame)
_FLAG_NON_SYNC = 0x01010000  # depends_on=1, is_non_sync_sample=1


class CmafChunker:
    """Builds the CMAF header and per-sample chunks for one track.

    `track` is an Mp4VideoTrack/Mp4AudioTrack; the source sample entry
    (avc1/av01/mp4a box) is embedded verbatim in the header's stsd.
    """

    TRACK_ID = 1

    def __init__(self, track):
        self.track = track
        self.timescale = track.timescale
        self._seq = 0
        self._dts = 0

    # -- CMAF header (init segment) -----------------------------------

    def init_segment(self) -> bytes:
        t = self.track
        video = getattr(t, 'width', None) is not None
        ftyp = _box(b'ftyp', b'iso6', struct.pack('>I', 1),
                    b'iso6cmfc')
        stbl = _box(
            b'stbl',
            _full(b'stsd', 0, 0, struct.pack('>I', 1),
                  t.sample_entry_bytes),
            _full(b'stts', 0, 0, struct.pack('>I', 0)),
            _full(b'stsc', 0, 0, struct.pack('>I', 0)),
            _full(b'stsz', 0, 0, struct.pack('>II', 0, 0)),
            _full(b'stco', 0, 0, struct.pack('>I', 0)),
        )
        if video:
            mhd = _full(b'vmhd', 0, 1, struct.pack('>4H', 0, 0, 0, 0))
        else:
            mhd = _full(b'smhd', 0, 0, struct.pack('>HH', 0, 0))
        minf = _box(
            b'minf', mhd,
            _box(b'dinf', _full(b'dref', 0, 0, struct.pack('>I', 1),
                                _full(b'url ', 0, 1))),
            stbl,
        )
        handler = b'vide' if video else b'soun'
        mdia = _box(
            b'mdia',
            _full(b'mdhd', 0, 0,
                  struct.pack('>IIIIHH', 0, 0, self.timescale, 0,
                              0x55C4, 0)),  # language "und"
            _full(b'hdlr', 0, 0, struct.pack('>I4s12x', 0, handler),
                  b'aiomoqt\x00'),
            minf,
        )
        if video:
            dims = struct.pack('>II', t.width << 16, t.height << 16)
            volume = 0
        else:
            dims = struct.pack('>II', 0, 0)
            volume = 0x0100
        tkhd = _full(
            b'tkhd', 0, 3,
            struct.pack('>IIIII', 0, 0, self.TRACK_ID, 0, 0),
            struct.pack('>IIHHHH', 0, 0, 0, 0, volume, 0),
            _UNITY_MATRIX, dims,
        )
        trex = _full(b'trex', 0, 0,
                     struct.pack('>IIIII', self.TRACK_ID, 1, 0, 0, 0))
        moov = _box(
            b'moov',
            _full(b'mvhd', 0, 0,
                  struct.pack('>IIII', 0, 0, self.timescale, 0),
                  struct.pack('>IHH8x', 0x00010000, 0x0100, 0),
                  _UNITY_MATRIX, b'\x00' * 24,
                  struct.pack('>I', self.TRACK_ID + 1)),
            _box(b'trak', tkhd, mdia),
            _box(b'mvex', trex),
        )
        return ftyp + moov

    # -- chunks --------------------------------------------------------

    def chunk(self, payload: bytes, duration: int,
              key_frame: bool = True,
              decode_time: Optional[int] = None) -> bytes:
        """One CMAF chunk: moof(mfhd,traf(tfhd,tfdt,trun)) + mdat.

        duration is in track-timescale units; decode_time overrides the
        running tfdt (timescale units) when the source skips.
        """
        self._seq += 1
        if decode_time is not None:
            self._dts = decode_time
        mfhd = _full(b'mfhd', 0, 0, struct.pack('>I', self._seq))
        tfhd = _full(b'tfhd', 0, 0x020000,  # default-base-is-moof
                     struct.pack('>I', self.TRACK_ID))
        tfdt = _full(b'tfdt', 1, 0, struct.pack('>Q', self._dts))
        flags = _FLAG_SYNC if key_frame else _FLAG_NON_SYNC
        # trun: data-offset | sample-duration | sample-size | sample-flags
        # sizes are fixed for one sample, so data_offset is computable:
        # moof(8) + mfhd(16) + traf(8) + tfhd(16) + tfdt(20) + trun(32)
        # + mdat header(8) = 108
        trun = _full(b'trun', 0, 0x000701,
                     struct.pack('>IiIII', 1, 108, duration,
                                 len(payload), flags))
        moof = _box(b'moof', mfhd, _box(b'traf', tfhd, tfdt, trun))
        assert len(moof) == 100
        self._dts += duration
        return moof + _box(b'mdat', payload)


# -- readers ---------------------------------------------------------

def _full_box_field(data: bytes, path: tuple, v0: tuple, v1: tuple):
    """One field of the full box at `path`: v0/v1 are (offset, format)
    after version+flags for box version 0 / 1. None if absent or short."""
    try:
        span = _find(data, 0, len(data), *path)
    except Mp4Error:
        return None
    if span is None or span[1] - span[0] < 4:
        return None
    off, fmt = v1 if data[span[0]] == 1 else v0
    pos = span[0] + 4 + off
    if pos + struct.calcsize(fmt) > span[1]:
        return None
    return struct.unpack_from(fmt, data, pos)[0]


def chunk_decode_time(chunk: bytes) -> Optional[int]:
    """Decode time of a CMAF chunk's first sample (moof tfdt), in track
    timescale units; None if the chunk has no tfdt."""
    return _full_box_field(chunk, (b'moof', b'traf', b'tfdt'),
                           (0, '>I'), (0, '>Q'))


def set_chunk_timing(chunk: bytearray, decode_time: int,
                     cto: Optional[int] = None) -> None:
    """Rewrite a CMAF chunk's tfdt and, when given, its first sample's
    composition offset in place (track timescale units)."""
    traf = _find(chunk, 0, len(chunk), b'moof', b'traf')
    tfdt = _find(chunk, *traf, b'tfdt') if traf else None
    if tfdt is None:
        raise Mp4Error("chunk has no tfdt")
    if chunk[tfdt[0]] == 1:
        struct.pack_into('>Q', chunk, tfdt[0] + 4, decode_time)
    elif decode_time < 1 << 32:
        struct.pack_into('>I', chunk, tfdt[0] + 4, decode_time)
    else:
        raise Mp4Error("decode time does not fit a version 0 tfdt")
    if cto is None:
        return
    trun = _find(chunk, *traf, b'trun')
    if trun is None:
        raise Mp4Error("chunk has no trun")
    version = chunk[trun[0]]
    flags = int.from_bytes(chunk[trun[0] + 1:trun[0] + 4], 'big')
    if not flags & 0x800:
        if cto:
            raise Mp4Error("trun carries no composition offsets")
        return
    if cto < 0 and version == 0:
        raise Mp4Error("negative composition offset in a version 0 trun")
    # sample_count, then data_offset / first_sample_flags if present, then
    # the first sample's duration / size / flags fields that precede cto
    pos = trun[0] + 8 + 4 * bin(flags & 0x5).count('1')
    pos += 4 * bin(flags & 0x700).count('1')
    struct.pack_into('>i' if version else '>I', chunk, pos, cto)


def strip_edit_lists(init: bytes) -> bytes:
    """The CMAF header without any trak's edts, so chunk timing alone
    defines presentation time."""
    out = bytearray()
    for btype, body, end in _boxes(init, 0, len(init)):
        if btype != b'moov':
            out += init[body - 8:end]
            continue
        moov = bytearray()
        for ctype, cbody, cend in _boxes(init, body, end):
            if ctype == b'trak':
                moov += _box(b'trak', *(init[tb - 8:te] for tt, tb, te
                                        in _boxes(init, cbody, cend)
                                        if tt != b'edts'))
            else:
                moov += init[cbody - 8:cend]
        out += _box(b'moov', bytes(moov))
    return bytes(out)


def init_timescale(init: bytes) -> Optional[int]:
    """Track timescale from a CMAF header's mdhd; None if absent."""
    return _full_box_field(init, (b'moov', b'trak', b'mdia', b'mdhd'),
                           (8, '>I'), (16, '>I'))


def init_codec_string(init: bytes) -> Optional[str]:
    """RFC 6381 codec string of a CMAF header's sample entry (avc1/avc3,
    hvc1/hev1, av01, mp4a, Opus); None if absent or unrecognized."""
    try:
        stsd = _find(init, 0, len(init), b'moov', b'trak', b'mdia', b'minf',
                     b'stbl', b'stsd')
        if stsd is None:
            return None
        entry = next(_boxes(init, stsd[0] + 8, stsd[1]), None)
        if entry is None:
            return None
        etype, body, end = entry
        kind = etype.decode('latin-1')
        if etype in (b'avc1', b'avc3', b'hvc1', b'hev1', b'av01'):
            for ctype, cbody, cend in _boxes(init, body + 78, end):
                cfg = init[cbody:cend]
                if ctype == b'avcC':
                    return kind + avcc_codec_string(cfg)[4:]
                if ctype == b'hvcC':
                    return hvcc_codec_string(cfg, kind)
                if ctype == b'av1C':
                    return av1c_codec_string(cfg)
        elif etype == b'mp4a':
            esds = _find(init, body + 28, end, b'esds')
            if esds is not None:
                oti, asc = esds_asc(init, esds[0])
                if oti == 0x40 and asc:
                    aot = asc[0] >> 3
                    if aot == 31 and len(asc) > 1:  # escape: 6 more bits
                        aot = 32 + (((asc[0] & 7) << 3) | (asc[1] >> 5))
                    return f"mp4a.40.{aot}"
        elif etype == b'Opus':
            return 'opus'
    except (Mp4Error, IndexError, struct.error):
        return None
    return None
