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


def _traks(d: bytes):
    """Yield (sample_entry_type, entry_body, entry_end, trak_span)."""
    moov = _find(d, 0, len(d), b'moov')
    if moov is None:
        raise Mp4Error("no moov box (fragmented mp4 unsupported)")
    for btype, body, bend in _boxes(d, *moov):
        if btype != b'trak':
            continue
        stsd = _find(d, body, bend, b'mdia', b'minf', b'stbl', b'stsd')
        if stsd is None:
            continue
        for etype, ebody, eend in _boxes(d, stsd[0] + 8, stsd[1]):
            yield etype, ebody, eend, (body, bend)


class _TrackReader:
    """Shared ISO-BMFF sample-table machinery for one trak."""

    def __init__(self, data: bytes, entry, trak):
        d = self._data = data
        body, bend = trak
        mdhd = _find(d, body, bend, b'mdia', b'mdhd')
        version = d[mdhd[0]]
        self.timescale = struct.unpack_from(
            '>I', d, mdhd[0] + (20 if version == 1 else 12))[0]
        self._parse_entry(*entry)
        stbl = _find(d, body, bend, b'mdia', b'minf', b'stbl')

        def table(name):
            box = _find(d, *stbl, name)
            return None if box is None else (box[0] + 4, box[1])

        # stsz: sample sizes
        pos, _ = table(b'stsz')
        fixed, count = struct.unpack_from('>II', d, pos)
        self._sizes = ([fixed] * count if fixed else
                       list(struct.unpack_from(f'>{count}I', d, pos + 8)))
        # stco/co64: chunk offsets
        co = table(b'stco') or table(b'co64')
        big = _find(d, *stbl, b'stco') is None
        pos, _ = co
        n = struct.unpack_from('>I', d, pos)[0]
        self._chunk_offsets = list(struct.unpack_from(
            f'>{n}{"Q" if big else "I"}', d, pos + 4))
        # stsc: sample-to-chunk runs
        pos, _ = table(b'stsc')
        n = struct.unpack_from('>I', d, pos)[0]
        self._stsc = [struct.unpack_from('>III', d, pos + 4 + 12 * i)
                      for i in range(n)]
        # stss: sync samples (absent = all sync, e.g. audio)
        st = table(b'stss')
        if st is None:
            self._sync = None
        else:
            n = struct.unpack_from('>I', d, st[0])[0]
            self._sync = set(struct.unpack_from(f'>{n}I', d, st[0] + 4))
        # stts: decode timestamps
        pos, _ = table(b'stts')
        n = struct.unpack_from('>I', d, pos)[0]
        self._stts = [struct.unpack_from('>II', d, pos + 4 + 8 * i)
                      for i in range(n)]

    def _parse_entry(self, ebody, eend):
        raise NotImplementedError

    @property
    def avg_bitrate(self) -> Optional[int]:
        """Mean track bitrate in bps from the sample tables."""
        dur = sum(c * delta for c, delta in self._stts)
        if not dur:
            return None
        return int(sum(self._sizes) * 8 * self.timescale / dur)

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


class Mp4VideoTrack(_TrackReader):
    """Video track. H.264 (avc1/avc3): samples are 4-byte-length-
    prefixed AVC = LOC canonical payloads, avcc = decoder description.
    AV1 (av01): samples are temporal units = WebCodecs chunk payloads
    verbatim; config travels in-band, so no description is emitted."""

    def _parse_entry(self, ebody, eend):
        d = self._data
        self.width, self.height = struct.unpack_from('>HH', d, ebody + 24)
        avcc = _find(d, ebody + 78, eend, b'avcC')
        av1c = _find(d, ebody + 78, eend, b'av1C')
        self.avcc = d[avcc[0]:avcc[1]] if avcc else None
        self.av1c = d[av1c[0]:av1c[1]] if av1c else None
        if self.avcc is None and self.av1c is None:
            raise Mp4Error("no avcC/av1C in video sample entry")

    @property
    def codec_string(self) -> str:
        if self.avcc is not None:
            return avcc_codec_string(self.avcc)
        return av1c_codec_string(self.av1c)

    @property
    def config(self) -> Optional[bytes]:
        """Decoder description (WebCodecs): avcC for H.264; None for
        AV1 (registry: config OBUs ride in-band, no description)."""
        return self.avcc

    @property
    def fps(self) -> Optional[float]:
        total = sum(c for c, _ in self._stts)
        dur = sum(c * delta for c, delta in self._stts)
        return round(total * self.timescale / dur, 2) if dur else None


def _read_descriptor(d: bytes, pos: int):
    """MPEG-4 descriptor: tag byte + 7-bit-continued length."""
    tag = d[pos]
    pos += 1
    size = 0
    while True:
        b = d[pos]
        pos += 1
        size = (size << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    return tag, size, pos


class Mp4AudioTrack(_TrackReader):
    """AAC (mp4a) track: samples are raw AAC access units — LOC payload
    for mp4a.40.x; asc = AudioSpecificConfig (decoder description /
    catalog init data)."""

    def _parse_entry(self, ebody, eend):
        d = self._data
        self.channels = struct.unpack_from('>H', d, ebody + 16)[0]
        self.samplerate = struct.unpack_from('>I', d, ebody + 24)[0] >> 16
        esds = _find(d, ebody + 28, eend, b'esds')
        if esds is None:
            raise Mp4Error("no esds in mp4a sample entry")
        pos = esds[0] + 4  # version/flags
        tag, _, pos = _read_descriptor(d, pos)
        if tag != 0x03:
            raise Mp4Error(f"expected ES descriptor, got {tag:#x}")
        pos += 3  # ES_ID + streamDependence/URL/OCR flags (none set)
        tag, _, pos = _read_descriptor(d, pos)
        if tag != 0x04:
            raise Mp4Error(f"expected DecoderConfig descriptor, got {tag:#x}")
        self.object_type_indication = d[pos]  # 0x40 = MPEG-4 Audio
        pos += 13
        tag, size, pos = _read_descriptor(d, pos)
        if tag != 0x05:
            raise Mp4Error("no DecoderSpecificInfo (AudioSpecificConfig)")
        self.asc = d[pos:pos + size]

    @property
    def codec_string(self) -> str:
        """RFC 6381 (e.g. mp4a.40.2) from the ASC audioObjectType."""
        return f"mp4a.40.{(self.asc[0] >> 3) & 0x1F}"


class Mp4Reader:
    """Plain (non-fragmented) MP4: first H.264 video and/or first AAC
    audio track. At least one must be present."""

    def __init__(self, path: str):
        with open(path, 'rb') as f:
            data = f.read()
        self.video: Optional[Mp4VideoTrack] = None
        self.audio: Optional[Mp4AudioTrack] = None
        for etype, ebody, eend, trak in _traks(data):
            if etype in (b'avc1', b'avc3', b'av01') and self.video is None:
                self.video = Mp4VideoTrack(data, (ebody, eend), trak)
            elif etype == b'mp4a' and self.audio is None:
                self.audio = Mp4AudioTrack(data, (ebody, eend), trak)
        if self.video is None and self.audio is None:
            raise Mp4Error("no avc1/avc3/av01 or mp4a track")


class Mp4AvcReader(Mp4VideoTrack):
    """Video-only compatibility entry point."""

    def __init__(self, path: str):
        with open(path, 'rb') as f:
            data = f.read()
        for etype, ebody, eend, trak in _traks(data):
            if etype in (b'avc1', b'avc3'):
                super().__init__(data, (ebody, eend), trak)
                return
        raise Mp4Error("no avc1/avc3 video track")


# ADTS sampling_frequency_index table (ISO 14496-3).
_ADTS_FREQ = (96000, 88200, 64000, 48000, 44100, 32000, 24000,
              22050, 16000, 12000, 11025, 8000, 7350)


def adts_frame(asc: bytes, payload: bytes) -> bytes:
    """Wrap one raw AAC AU in an ADTS header — concatenation of these
    is a playable .aac stream."""
    obj = (asc[0] >> 3) & 0x1F
    freq_idx = ((asc[0] & 7) << 1) | (asc[1] >> 7)
    chan = (asc[1] >> 3) & 0xF
    n = len(payload) + 7
    return bytes([
        0xFF, 0xF1,  # syncword, MPEG-4, layer 0, no CRC
        ((obj - 1) << 6) | (freq_idx << 2) | (chan >> 2),
        ((chan & 3) << 6) | ((n >> 11) & 0x3),
        (n >> 3) & 0xFF,
        ((n & 0x7) << 5) | 0x1F,
        0xFC,
    ]) + payload


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


def avcc_from_sps_pps(sps: bytes, pps: bytes) -> bytes:
    """Build an avcC record from raw SPS/PPS NAL units."""
    return (bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1])
            + struct.pack('>H', len(sps)) + sps
            + b'\x01' + struct.pack('>H', len(pps)) + pps)


def _rbsp(nal: bytes) -> bytes:
    """NAL payload with emulation-prevention bytes removed."""
    return nal.replace(b'\x00\x00\x03', b'\x00\x00')


class _BitReader:
    def __init__(self, data: bytes):
        self._d = data
        self._pos = 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self._d[self._pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return v

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _skip_scaling_list(r: _BitReader, size: int) -> None:
    nxt = 8
    for _ in range(size):
        if nxt != 0:
            nxt = (nxt + r.se() + 256) % 256


_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128,
                  138, 139, 134, 135}


def sps_dimensions(sps: bytes) -> Tuple[int, int]:
    """Coded (width, height) from a raw SPS NAL unit."""
    r = _BitReader(_rbsp(sps[1:]))
    profile = r.u(8)
    r.u(16)  # constraint flags + level
    r.ue()   # sps id
    chroma = 1
    if profile in _HIGH_PROFILES:
        chroma = r.ue()
        if chroma == 3:
            r.u(1)
        r.ue()
        r.ue()
        r.u(1)
        if r.u(1):
            for i in range(8 if chroma != 3 else 12):
                if r.u(1):
                    _skip_scaling_list(r, 16 if i < 6 else 64)
    r.ue()
    poc_type = r.ue()
    if poc_type == 0:
        r.ue()
    elif poc_type == 1:
        r.u(1)
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()
    r.u(1)
    width = (r.ue() + 1) * 16
    h_units = r.ue() + 1
    frame_mbs_only = r.u(1)
    height = (2 - frame_mbs_only) * h_units * 16
    if not frame_mbs_only:
        r.u(1)
    r.u(1)
    if r.u(1):  # frame cropping
        crop_l, crop_r, crop_t, crop_b = r.ue(), r.ue(), r.ue(), r.ue()
        unit_x = 2 if chroma in (1, 2) else 1
        unit_y = (2 if chroma == 1 else 1) * (2 - frame_mbs_only)
        width -= (crop_l + crop_r) * unit_x
        height -= (crop_t + crop_b) * unit_y
    return width, height


class AnnexBAssembler:
    """Incremental Annex-B byte stream → access units.

    feed() returns completed (payload, key_frame) pairs with payloads
    in LOC canonical form (4-byte length prefixes). SPS/PPS are
    captured for `config` (avcC) and excluded from payloads, matching
    mp4-sample form; AUD NALs delimit and are dropped. close() flushes
    the final unterminated access unit at EOF.
    """

    def __init__(self):
        self._buf = bytearray()
        self._au: List[bytes] = []
        self._au_has_vcl = False
        self.sps: Optional[bytes] = None
        self.pps: Optional[bytes] = None

    @property
    def config(self) -> Optional[bytes]:
        if self.sps and self.pps:
            return avcc_from_sps_pps(self.sps, self.pps)
        return None

    def feed(self, chunk: bytes) -> List[Tuple[bytes, bool]]:
        self._buf += chunk
        out: List[Tuple[bytes, bool]] = []
        for nal in self._extract_nals():
            self._push(nal, out)
        return out

    def close(self) -> List[Tuple[bytes, bool]]:
        out: List[Tuple[bytes, bool]] = []
        buf = bytes(self._buf)
        self._buf = bytearray()
        i = buf.find(b'\x00\x00\x01')
        if i != -1:
            nal = buf[i + 3:].rstrip(b'\x00')
            if nal:
                self._push(nal, out)
        self._flush(out)
        return out

    def _extract_nals(self) -> List[bytes]:
        # A NAL is complete only once the next start code arrives; the
        # trailing partial NAL stays buffered.
        buf = self._buf
        out: List[bytes] = []
        i = buf.find(b'\x00\x00\x01')
        if i == -1:
            if len(buf) > 2:
                del buf[:-2]
            return out
        while True:
            j = buf.find(b'\x00\x00\x01', i + 3)
            if j == -1:
                del buf[:i]
                return out
            nal = bytes(buf[i + 3:j]).rstrip(b'\x00')
            if nal:
                out.append(nal)
            i = j

    def _flush(self, out: List[Tuple[bytes, bool]]) -> None:
        if self._au:
            payload = b''.join(struct.pack('>I', len(n)) + n
                               for n in self._au)
            key = any((n[0] & 0x1F) == 5 for n in self._au)
            out.append((payload, key))
        self._au = []
        self._au_has_vcl = False

    def _push(self, nal: bytes, out: List[Tuple[bytes, bool]]) -> None:
        t = nal[0] & 0x1F
        if t in (7, 8):  # SPS/PPS → config, start a new access unit
            if self._au_has_vcl:
                self._flush(out)
            if t == 7:
                self.sps = nal
            else:
                self.pps = nal
            return
        if t == 9:  # AUD delimits and is dropped
            self._flush(out)
            return
        if t in (1, 5):
            # first_mb_in_slice == 0 (ue(v) leading '1' bit) marks a
            # new primary coded picture.
            first_mb0 = len(nal) > 1 and (nal[1] & 0x80) != 0
            if self._au_has_vcl and first_mb0:
                self._flush(out)
            self._au.append(nal)
            self._au_has_vcl = True
            return
        # SEI and other non-VCL NALs precede their access unit's slices
        if self._au_has_vcl:
            self._flush(out)
        self._au.append(nal)


def av1c_codec_string(av1c: bytes) -> str:
    """WebCodecs/ISOBMFF codec string from av1C (e.g. av01.0.08M.08)."""
    profile = av1c[1] >> 5
    level = av1c[1] & 0x1F
    tier = 'H' if av1c[2] & 0x80 else 'M'
    high, twelve = av1c[2] & 0x40, av1c[2] & 0x20
    depth = 12 if twelve else (10 if high else 8)
    return f"av01.{profile}.{level:02d}{tier}.{depth:02d}"


class IvfWriter:
    """IVF container for received AV1 temporal units — the minimal
    ffplay-playable dump format (frame count patched on close)."""

    def __init__(self, fh, width: int, height: int, fps: float = 30.0):
        self._fh = fh
        self._n = 0
        num, den = (int(round(fps * 1000)), 1000) if fps else (30, 1)
        fh.write(struct.pack('<4sHH4sHHIIQ', b'DKIF', 0, 32, b'AV01',
                             width or 0, height or 0, num, den, 0))

    def add(self, payload: bytes) -> None:
        self._fh.write(struct.pack('<IQ', len(payload), self._n))
        self._fh.write(payload)
        self._n += 1

    def close(self) -> None:
        if self._fh.seekable():
            self._fh.seek(24)
            self._fh.write(struct.pack('<I', self._n))
        self._fh.close()
