"""Pure-python media sources for the LOC demo pipeline.

- pcm_tone_frames: synthesized pcm-s16 audio (a WebCodecs registry
  codec — no encoder needed).
- Mp4AvcReader: minimal ISO-BMFF reader for one H.264 video track.
  MP4 samples are 4-byte-length-prefixed AVC — byte-identical to LOC's
  "canonical" payload form (loc-02 §2.1.3) — and the avcC box contents
  are the VIDEO_CONFIG extradata. No decode, no re-encode, no deps.
- split_annexb / annexb_is_keyframe: helpers for Annex-B input
  (e.g. an ffmpeg pipe), LOC §2.1.4 form.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple


# -- audio ------------------------------------------------------------

def pcm_tone_frames(duration_s: float = 5.0, freq: float = 440.0,
                    samplerate: int = 48000, channels: int = 2,
                    frame_ms: int = 20,
                    amplitude: float = 0.25) -> Iterator[Tuple[bytes, int]]:
    """Yield (payload, timestamp_us) pcm-s16 interleaved sine frames."""
    spf = samplerate * frame_ms // 1000
    peak = int(32767 * amplitude)
    n_frames = int(duration_s * 1000 / frame_ms)
    for f in range(n_frames):
        base = f * spf
        pcm = bytearray()
        for i in range(spf):
            v = int(peak * math.sin(
                2 * math.pi * freq * (base + i) / samplerate))
            pcm += struct.pack('<h', v) * channels
        yield bytes(pcm), f * frame_ms * 1000


# -- mp4 --------------------------------------------------------------

@dataclass
class VideoSample:
    payload: bytes      # length-prefixed AVC, LOC canonical form
    key_frame: bool
    timestamp_us: int


class Mp4Error(ValueError):
    pass


def _boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, btype = struct.unpack_from('>I4s', data, pos)
        if size == 1:
            size = struct.unpack_from('>Q', data, pos + 8)[0]
        if size < 8 or pos + size > end:
            raise Mp4Error(f"bad box size {size} at {pos}")
        yield btype, pos + 8, pos + size
        pos += size


def _find(data, start, end, *path):
    for btype, body, bend in _boxes(data, start, end):
        if btype == path[0]:
            if len(path) == 1:
                return body, bend
            return _find(data, body, bend, *path[1:])
    return None


class Mp4AvcReader:
    """One H.264 (avc1/avc3) video track out of a plain MP4.

    Exposes avcc (VIDEO_CONFIG extradata), width/height, timescale,
    and samples() yielding VideoSample in stored (decode) order.
    Fragmented MP4 (moof) is not supported.
    """

    def __init__(self, path: str):
        with open(path, 'rb') as f:
            self._data = f.read()
        d = self._data
        moov = _find(d, 0, len(d), b'moov')
        if moov is None:
            raise Mp4Error("no moov box (fragmented mp4 unsupported)")
        trak = self._video_trak(*moov)
        if trak is None:
            raise Mp4Error("no avc1/avc3 video track")
        self._parse_trak(*trak)

    def _video_trak(self, moov_body, moov_end):
        d = self._data
        for btype, body, bend in _boxes(d, moov_body, moov_end):
            if btype != b'trak':
                continue
            stsd = _find(d, body, bend, b'mdia', b'minf', b'stbl', b'stsd')
            if stsd is None:
                continue
            for etype, ebody, eend in _boxes(d, stsd[0] + 8, stsd[1]):
                if etype in (b'avc1', b'avc3'):
                    return body, bend
        return None

    def _parse_trak(self, body, bend):
        d = self._data
        mdhd = _find(d, body, bend, b'mdia', b'mdhd')
        version = d[mdhd[0]]
        self.timescale = struct.unpack_from(
            '>I', d, mdhd[0] + (20 if version == 1 else 12))[0]
        stbl = _find(d, body, bend, b'mdia', b'minf', b'stbl')
        stsd = _find(d, *stbl, b'stsd')
        for etype, ebody, eend in _boxes(d, stsd[0] + 8, stsd[1]):
            if etype in (b'avc1', b'avc3'):
                self.width, self.height = struct.unpack_from(
                    '>HH', d, ebody + 24)
                avcc = _find(d, ebody + 78, eend, b'avcC')
                if avcc is None:
                    raise Mp4Error("no avcC in sample entry")
                self.avcc = d[avcc[0]:avcc[1]]
                break

        def table(name, *, full=True):
            box = _find(d, *stbl, name)
            return None if box is None else (box[0] + (4 if full else 0),
                                             box[1])

        # stsz: sample sizes
        pos, end = table(b'stsz')
        fixed, count = struct.unpack_from('>II', d, pos)
        self._sizes = ([fixed] * count if fixed else
                       list(struct.unpack_from(f'>{count}I', d, pos + 8)))
        # stco/co64: chunk offsets
        co = table(b'stco') or table(b'co64')
        big = _find(d, *stbl, b'stco') is None
        pos, end = co
        n = struct.unpack_from('>I', d, pos)[0]
        self._chunk_offsets = list(struct.unpack_from(
            f'>{n}{"Q" if big else "I"}', d, pos + 4))
        # stsc: sample-to-chunk runs
        pos, end = table(b'stsc')
        n = struct.unpack_from('>I', d, pos)[0]
        self._stsc = [struct.unpack_from('>III', d, pos + 4 + 12 * i)
                      for i in range(n)]
        # stss: sync samples (absent = all sync)
        st = table(b'stss')
        if st is None:
            self._sync = None
        else:
            n = struct.unpack_from('>I', d, st[0])[0]
            self._sync = set(struct.unpack_from(f'>{n}I', d, st[0] + 4))
        # stts: decode timestamps
        pos, end = table(b'stts')
        n = struct.unpack_from('>I', d, pos)[0]
        self._stts = [struct.unpack_from('>II', d, pos + 4 + 8 * i)
                      for i in range(n)]

    @property
    def fps(self) -> Optional[float]:
        total = sum(c for c, _ in self._stts)
        dur = sum(c * delta for c, delta in self._stts)
        return round(total * self.timescale / dur, 2) if dur else None

    def _offsets(self) -> List[int]:
        """Absolute file offset per sample via stsc/stco."""
        runs = self._stsc
        offsets = []
        sample = 0
        for i, (first_chunk, per_chunk, _sdi) in enumerate(runs):
            last_chunk = (runs[i + 1][0] - 1 if i + 1 < len(runs)
                          else len(self._chunk_offsets))
            for chunk in range(first_chunk, last_chunk + 1):
                pos = self._chunk_offsets[chunk - 1]
                for _ in range(per_chunk):
                    if sample >= len(self._sizes):
                        return offsets
                    offsets.append(pos)
                    pos += self._sizes[sample]
                    sample += 1
        return offsets

    def samples(self) -> Iterator[VideoSample]:
        offsets = self._offsets()
        ts_iter = (delta for count, delta in self._stts
                   for _ in range(count))
        t = 0
        for i, (off, size) in enumerate(zip(offsets, self._sizes)):
            yield VideoSample(
                payload=self._data[off:off + size],
                key_frame=(self._sync is None or (i + 1) in self._sync),
                timestamp_us=t * 1_000_000 // self.timescale,
            )
            t += next(ts_iter)


# -- Annex-B ----------------------------------------------------------

def split_annexb(au: bytes) -> List[bytes]:
    """Split one Annex-B access unit into NAL units (no start codes)."""
    nals = []
    i = au.find(b'\x00\x00\x01')
    while i != -1:
        start = i + 3
        j = au.find(b'\x00\x00\x01', start)
        end = j - 1 if j != -1 and j > 0 and au[j - 1] == 0 else (
            j if j != -1 else len(au))
        nals.append(au[start:end])
        i = j
    return [n for n in nals if n]


def annexb_is_keyframe(au: bytes) -> bool:
    """True when the access unit contains an IDR slice (NAL type 5)."""
    return any((n[0] & 0x1F) == 5 for n in split_annexb(au) if n)


def lp_to_annexb(sample: bytes) -> bytes:
    """LOC canonical (4-byte length prefixes) → Annex-B start codes."""
    out = bytearray()
    pos = 0
    while pos + 4 <= len(sample):
        n = struct.unpack_from('>I', sample, pos)[0]
        pos += 4
        out += b'\x00\x00\x00\x01' + sample[pos:pos + n]
        pos += n
    return bytes(out)


def avcc_param_sets(avcc: bytes) -> bytes:
    """SPS/PPS out of an avcC box body, as Annex-B — prepend at
    keyframes to make a raw .h264 elementary stream decodable."""
    out = bytearray()
    pos = 5
    n_sps = avcc[pos] & 0x1F
    pos += 1
    for _ in range(n_sps):
        n = struct.unpack_from('>H', avcc, pos)[0]
        out += b'\x00\x00\x00\x01' + avcc[pos + 2:pos + 2 + n]
        pos += 2 + n
    n_pps = avcc[pos]
    pos += 1
    for _ in range(n_pps):
        n = struct.unpack_from('>H', avcc, pos)[0]
        out += b'\x00\x00\x00\x01' + avcc[pos + 2:pos + 2 + n]
        pos += 2 + n
    return bytes(out)


def avcc_codec_string(avcc: bytes) -> str:
    """WebCodecs/RFC 6381 codec string from avcC (e.g. avc1.42E01E)."""
    return f"avc1.{avcc[1]:02X}{avcc[2]:02X}{avcc[3]:02X}"
