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
