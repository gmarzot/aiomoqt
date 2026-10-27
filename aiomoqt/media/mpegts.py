"""MPEG-TS demux for live ingest: PAT/PMT → PES → H.264 access units
or H.265 access units and AAC or Opus frames, with the PES presentation
timestamps preserved.

One pipe (`ffmpeg … -c copy -f mpegts -`) carries both tracks with the
encoder's own timing, so A/V sync and frame pacing follow the source
rather than pipe arrival. Stream types: 0x1B (H.264 Annex-B), 0x24
(H.265 Annex-B), 0x0F
(AAC ADTS) and 0x06 registered as 'Opus' (ETSI TS 102 366 Annex D);
others are reported once and ignored.
"""
import logging
import statistics
import time
from fractions import Fraction
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from ..utils.timing import MediaTimestamps
from .sources import _ADTS_FREQ, AnnexBAssembler, HevcAnnexBAssembler

logger = logging.getLogger(__name__)

TS_PACKET = 188
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_HEVC = 0x24
STREAM_TYPE_AAC_ADTS = 0x0F
STREAM_TYPE_PRIVATE = 0x06
_OPUS_REGISTRATION = b'Opus'
_PTS_WRAP = 1 << 33
_PTS_HALF = 1 << 32
_AAC_FRAME_SAMPLES = 1024


class TsUnit(NamedTuple):
    """One access unit. `pts` is unwrapped 90 kHz; video payloads are
    LOC canonical (length-prefixed NALs), audio payloads raw AAC AUs."""
    kind: str
    payload: bytes
    key: bool
    pts: int


class TsClock:
    """PES PTS (90 kHz) → µs since the epoch.

    Anchored on the first PTS at the wall clock of its arrival, so the
    stamps carry the source's spacing with the pipe's delay folded into a
    constant offset. A unit arriving before its predicted stamp moves the
    anchor earlier: no stamp is ever later than its unit's arrival, and
    the offset settles on the least-delayed unit seen. A backward jump
    beyond `restart_s` is an encoder restart and re-anchors."""

    def __init__(self, restart_s: float = 5.0,
                 now: Callable[[], float] = time.time):
        self._now = now
        self._restart = int(restart_s * 90_000)
        self._pts0: Optional[int] = None
        self._last = 0
        self._wall0_us = 0
        self._held = False

    def hold(self) -> None:
        """Keep the current anchor: stamps stay on the PTS schedule even
        when a unit arrives before its stamp. A restart still re-anchors."""
        self._held = True

    def to_us(self, pts: int) -> int:
        now_us = int(self._now() * 1_000_000)
        if self._pts0 is None or pts < self._last - self._restart:
            self._pts0 = pts
            self._last = pts
            self._wall0_us = now_us
            return now_us
        if pts > self._last:
            self._last = pts
        stamp = self._wall0_us + (pts - self._pts0) * 1000 // 90
        if stamp > now_us and not self._held:
            self._wall0_us -= stamp - now_us
            stamp = now_us
        return stamp


class TsCadence:
    """A live source's declared fixed cadence (`fps=N[/D],gop=M`): N/D
    frames per second, a key frame every M frames.

    Video timing per draft-marzot-moq-test-tracks: TIMESCALE 1000 × fps
    and Object Multiplier 1000, or N and D for a fractional rate (30000
    and 1001 at 29.97)."""

    def __init__(self, fps: Fraction, gop: int):
        if fps <= 0 or gop < 1:
            raise ValueError("fps and gop must be positive")
        self.fps = fps
        self.gop = gop
        if fps.denominator == 1:
            self.timescale, self.frame_ticks = 1000 * fps.numerator, 1000
        else:
            self.timescale, self.frame_ticks = fps.numerator, fps.denominator

    @classmethod
    def parse(cls, text: str) -> "TsCadence":
        fields = {}
        for part in text.split(','):
            key, sep, value = part.partition('=')
            if not sep or key.strip() not in ('fps', 'gop'):
                raise ValueError(f"expected fps=N[/D],gop=M, got {text!r}")
            fields[key.strip()] = value.strip()
        if set(fields) != {'fps', 'gop'}:
            raise ValueError(f"expected fps=N[/D],gop=M, got {text!r}")
        num, _, den = fields['fps'].partition('/')
        return cls(Fraction(int(num), int(den or 1)), int(fields['gop']))

    @property
    def framerate(self) -> float:
        return round(float(self.fps), 2)

    def video_timestamps(self, wall0_us: int) -> MediaTimestamps:
        """Mapped timing for video whose first group opens at `wall0_us`."""
        om = self.frame_ticks if self.gop > 1 else 0
        return MediaTimestamps(wall0_us, self.timescale,
                               self.gop * self.frame_ticks, om)

    @staticmethod
    def audio_timestamps(wall0_us: int, samplerate: int, frame_samples: int,
                         per_group: int) -> MediaTimestamps:
        """Mapped timing for audio frames of `frame_samples`, `per_group`
        to a group, the first opening at `wall0_us`."""
        om = frame_samples if per_group > 1 else 0
        return MediaTimestamps(wall0_us, samplerate,
                               per_group * frame_samples, om)


class TsRates:
    """Catalog values a transport stream does not declare, measured from
    its units: video frame rate from the PTS spacing, and payload bitrate
    per kind. Video bitrate spans whole GOPs (key frame to key frame)
    once `window_s` of media has passed, or any span after five windows;
    audio spans `window_s`. Values stay None until measured."""

    def __init__(self, window_s: float = 2.0):
        self.window = int(window_s * 90_000)
        self.fps: Optional[float] = None
        self.bps: Dict[str, Optional[int]] = {'video': None, 'audio': None}
        self._first: Dict[str, int] = {}
        self._bytes: Dict[str, int] = {'video': 0, 'audio': 0}
        self._video_pts: List[int] = []

    def add(self, u: TsUnit) -> bool:
        """Count a unit; True when it completed a measurement."""
        new = False
        if u.kind == 'video' and self.fps is None:
            self._video_pts.append(u.pts)
            if len(self._video_pts) > 16:
                steps = [b - a for a, b in zip(self._video_pts,
                                               self._video_pts[1:])
                         if b > a]
                if steps:
                    self.fps = round(90_000 / statistics.median(steps), 2)
                    new = True
        if self.bps[u.kind] is not None:
            return new
        first = self._first.get(u.kind)
        if first is None:
            if u.kind == 'audio' or u.key:
                self._first[u.kind] = u.pts
                self._bytes[u.kind] = len(u.payload)
            return new
        span = u.pts - first
        whole = u.kind == 'audio' or u.key or span >= 5 * self.window
        if span >= self.window and whole:
            self.bps[u.kind] = self._bytes[u.kind] * 8 * 90_000 // span
            return True
        self._bytes[u.kind] += len(u.payload)
        return new


def adts_split(data: bytes) -> List[Tuple[bytes, int, int, bytes]]:
    """Split a run of ADTS frames into (asc, samplerate, channels, au).
    A truncated trailing frame is dropped."""
    out = []
    pos = 0
    n = len(data)
    while pos + 7 <= n:
        if data[pos] != 0xFF or (data[pos + 1] & 0xF6) != 0xF0:
            pos += 1
            continue
        protection_absent = data[pos + 1] & 1
        profile = (data[pos + 2] >> 6) & 3
        freq_idx = (data[pos + 2] >> 2) & 0xF
        chan = ((data[pos + 2] & 1) << 2) | (data[pos + 3] >> 6)
        length = (((data[pos + 3] & 3) << 11) | (data[pos + 4] << 3)
                  | (data[pos + 5] >> 5))
        hlen = 7 if protection_absent else 9
        if length < hlen or pos + length > n:
            break
        obj = profile + 1
        asc = bytes([(obj << 3) | (freq_idx >> 1),
                     ((freq_idx & 1) << 7) | (chan << 3)])
        rate = _ADTS_FREQ[freq_idx] if freq_idx < len(_ADTS_FREQ) else 48000
        out.append((asc, rate, chan, data[pos + hlen:pos + length]))
        pos += length
    return out


def opus_split(data: bytes) -> List[bytes]:
    """Opus packets out of a PES payload of opus_control_header-framed
    access units; trim and extension fields are skipped. A truncated
    trailing unit is dropped."""
    out = []
    pos = 0
    n = len(data)
    while pos + 2 <= n:
        hdr = (data[pos] << 8) | data[pos + 1]
        if hdr & 0xFFE0 != 0x7FE0:
            break
        pos += 2
        size = 0
        while pos < n:
            b = data[pos]
            pos += 1
            size += b
            if b != 0xFF:
                break
        pos += 2 * bool(hdr & 0x10) + 2 * bool(hdr & 0x08)
        if hdr & 0x04:
            if pos >= n:
                break
            pos += 1 + data[pos]
        if pos + size > n:
            break
        out.append(data[pos:pos + size])
        pos += size
    return out


def opus_samples(packet: bytes) -> int:
    """Duration of an Opus packet in 48 kHz samples, from its TOC byte
    (RFC 6716 §3.1)."""
    if not packet:
        return 0
    toc = packet[0]
    config = toc >> 3
    if config < 12:
        frame = (480, 960, 1920, 2880)[config & 3]
    elif config < 16:
        frame = (480, 960)[config & 1]
    else:
        frame = (120, 240, 480, 960)[config & 3]
    code = toc & 3
    if code == 0:
        return frame
    if code < 3:
        return 2 * frame
    return frame * (packet[1] & 0x3F if len(packet) > 1 else 0)


def _opus_channels(descriptors: bytes) -> Optional[int]:
    """Channel count from a DVB extension descriptor (0x7F, tag 0x80);
    None when the PMT carries none."""
    i = 0
    while i + 2 <= len(descriptors):
        tag, size = descriptors[i], descriptors[i + 1]
        body = descriptors[i + 2:i + 2 + size]
        if tag == 0x7F and len(body) >= 2 and body[0] == 0x80:
            code = body[1]
            return 2 if code == 0 else code if code <= 8 else None
        i += 2 + size
    return None


def _registered(descriptors: bytes, fourcc: bytes) -> bool:
    """True when a registration descriptor (0x05) names `fourcc`."""
    i = 0
    while i + 2 <= len(descriptors):
        tag, size = descriptors[i], descriptors[i + 1]
        if tag == 0x05 and descriptors[i + 2:i + 6] == fourcc:
            return True
        i += 2 + size
    return False


def _af_stuffed(pkt: bytes, pusi: bool) -> bool:
    """True when the adaptation field only pads the packet, which a muxer
    does to the last packet of a PES (ISO 13818-1 §2.4.3.5): stuffing
    bytes, or a field carrying nothing (indicator flags mark a PES start)."""
    af_len = pkt[4]
    if af_len == 0:
        return True
    end = 5 + af_len
    flags = pkt[5]
    i = 6 + 6 * bool(flags & 0x10) + 6 * bool(flags & 0x08) + bool(flags & 0x04)
    if flags & 0x02 and i < end:
        i += 1 + pkt[i]
    if flags & 0x01 and i < end:
        i += 1 + pkt[i]
    return i < end or flags == 0 or (not pusi and not flags & 0x1F)


def _pts_at(b: bytes, i: int) -> int:
    return ((((b[i] >> 1) & 7) << 30) | (b[i + 1] << 22)
            | (((b[i + 2] >> 1) & 0x7F) << 15) | (b[i + 3] << 7) | (b[i + 4] >> 1))


class TsDemuxer:
    """Incremental transport-stream demuxer. feed() returns the access
    units completed by the chunk; close() flushes the last PES at EOF.

    Video: each PES is fed through an AnnexBAssembler (H.264) or
    HevcAnnexBAssembler (H.265, `video_codec` 'hevc') and closed, so the
    units it held take the PES PTS (one AU per PES in practice);
    parameter sets land in `video.config`. Audio: ADTS frames are split, the
    AudioSpecificConfig derived from the header, and each frame's PTS
    advanced by 1024 samples within the PES; Opus packets are split from
    their control headers (`audio_asc` stays None) and advanced by their
    TOC duration. `audio_codec` is 'aac' or 'opus'.

    `config_changed` is raised (and left for the caller to clear) when a
    later SPS/PPS, ADTS header or Opus channel count differs from the
    first."""

    def __init__(self, clock: Optional[TsClock] = None):
        self.clock = clock or TsClock()
        self.video: AnnexBAssembler = AnnexBAssembler()
        self.video_codec: Optional[str] = None
        self.video_pid: Optional[int] = None
        self.audio_pid: Optional[int] = None
        self.audio_codec: Optional[str] = None
        self._opus_channels: Optional[int] = None
        self.audio_asc: Optional[bytes] = None
        self.audio_samplerate: Optional[int] = None
        self.audio_channels: Optional[int] = None
        self.pmt_seen = False
        self.unsupported: Set[int] = set()
        self.config_changed = False
        self.cc_errors = 0
        self._buf = bytearray()
        self._pmt_pid: Optional[int] = None
        self._pes: Dict[int, bytearray] = {}
        self._cc: Dict[int, int] = {}
        self._skip: Set[int] = set()
        self._last_pts: Dict[int, Tuple[int, int]] = {}
        self._video_config: Optional[bytes] = None
        # Emit a video PES at its stuffed last packet, not the next PUSI.
        self.eager_video = True
        self._ended: Set[int] = set()
        # 48 kHz samples per audio frame, from the first frame.
        self.audio_frame_samples: Optional[int] = None

    @property
    def has_audio(self) -> Optional[bool]:
        """None until the PMT is seen."""
        return None if not self.pmt_seen else self.audio_pid is not None

    def to_us(self, pts: int) -> int:
        return self.clock.to_us(pts)

    # ── packets ─────────────────────────────────────────────────────

    def feed(self, chunk: bytes) -> List[TsUnit]:
        self._buf += chunk
        out: List[TsUnit] = []
        buf = self._buf
        n = len(buf)
        pos = 0
        while n - pos >= TS_PACKET:
            if buf[pos] != 0x47 or (n - pos >= 2 * TS_PACKET
                                     and buf[pos + TS_PACKET] != 0x47):
                pos += 1
                continue
            self._packet(bytes(buf[pos:pos + TS_PACKET]), out)
            pos += TS_PACKET
        del buf[:pos]
        return out

    def close(self) -> List[TsUnit]:
        out: List[TsUnit] = []
        for pid, pes in list(self._pes.items()):
            self._emit_pes(pid, pes, out)
        self._pes.clear()
        self._buf = bytearray()
        return out

    def _packet(self, pkt: bytes, out: List[TsUnit]) -> None:
        if pkt[1] & 0x80:
            return  # transport_error_indicator
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid == 0x1FFF:
            return
        pusi = bool(pkt[1] & 0x40)
        afc = (pkt[3] >> 4) & 3
        cc = pkt[3] & 0xF
        if not afc & 1:
            return  # adaptation field only: no payload, CC not advanced
        off = 4
        stuffed = False
        if afc & 2:
            off += 1 + pkt[4]
            stuffed = _af_stuffed(pkt, pusi)
        if off >= TS_PACKET:
            return
        payload = pkt[off:]
        last = self._cc.get(pid)
        if last is not None:
            if cc == last:
                return  # duplicate packet
            if cc != (last + 1) & 0xF:
                self.cc_errors += 1
                if self._pes.pop(pid, None) is not None:
                    self._skip.add(pid)
                if self.cc_errors <= 3:
                    logger.warning(f"mpegts: continuity gap on pid {pid}")
        self._cc[pid] = cc
        if pid == 0:
            self._pat(payload, pusi)
        elif pid == self._pmt_pid:
            self._pmt(payload, pusi)
        elif pid in (self.video_pid, self.audio_pid):
            self._es(pid, payload, pusi, out, stuffed)

    # ── PSI ─────────────────────────────────────────────────────────

    @staticmethod
    def _section(payload: bytes, pusi: bool) -> Optional[bytes]:
        if not pusi or not payload:
            return None
        sec = payload[1 + payload[0]:]
        if len(sec) < 8:
            return None
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        end = min(3 + length, len(sec))
        return sec[:end]

    def _pat(self, payload: bytes, pusi: bool) -> None:
        sec = self._section(payload, pusi)
        if sec is None or sec[0] != 0x00:
            return
        for i in range(8, len(sec) - 4, 4):
            program = (sec[i] << 8) | sec[i + 1]
            pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
            if program != 0:
                self._pmt_pid = pid
                return

    def _pmt(self, payload: bytes, pusi: bool) -> None:
        sec = self._section(payload, pusi)
        if sec is None or sec[0] != 0x02:
            return
        info_len = ((sec[10] & 0x0F) << 8) | sec[11]
        i = 12 + info_len
        end = len(sec) - 4
        video = audio = None
        while i + 5 <= end:
            stype = sec[i]
            pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
            es_len = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
            desc = sec[i + 5:i + 5 + es_len]
            i += 5 + es_len
            if stype in (STREAM_TYPE_H264, STREAM_TYPE_HEVC) and video is None:
                video = pid
                codec = 'hevc' if stype == STREAM_TYPE_HEVC else 'h264'
                if codec != self.video_codec:
                    self.video_codec = codec
                    self.video = (HevcAnnexBAssembler() if codec == 'hevc'
                                  else AnnexBAssembler())
            elif stype == STREAM_TYPE_AAC_ADTS and audio is None:
                audio = pid
                self.audio_codec = 'aac'
            elif (stype == STREAM_TYPE_PRIVATE and audio is None
                  and _registered(desc, _OPUS_REGISTRATION)):
                audio = pid
                self.audio_codec = 'opus'
                self._opus_channels = _opus_channels(desc)
            elif stype not in self.unsupported:
                self.unsupported.add(stype)
                logger.warning(f"mpegts: stream type {stype:#x} on pid "
                               f"{pid} ignored")
        self.video_pid, self.audio_pid = video, audio
        self.pmt_seen = True

    # ── PES ─────────────────────────────────────────────────────────

    def _es(self, pid: int, payload: bytes, pusi: bool,
            out: List[TsUnit], stuffed: bool = False) -> None:
        if pusi:
            prev = self._pes.pop(pid, None)
            if prev is not None:
                self._emit_pes(pid, prev, out)
            self._skip.discard(pid)
            self._ended.discard(pid)
            self._pes[pid] = bytearray(payload)
        elif pid in self._ended:
            # A stuffed packet that did not end its PES: the unit already
            # sent was cut short; wait for PUSI from now on.
            self._ended.discard(pid)
            self._skip.add(pid)
            self.eager_video = False
            logger.warning("mpegts: video PES continued past a stuffed "
                           "packet; units now complete at the next PES")
            return
        elif pid in self._skip or pid not in self._pes:
            return
        else:
            self._pes[pid] += payload
        # A bounded PES (audio) is complete as soon as its length is in;
        # an unbounded one (video) at the packet stuffed to end it.
        pes = self._pes[pid]
        if len(pes) >= 6:
            length = (pes[4] << 8) | pes[5]
            if length and len(pes) >= 6 + length:
                self._emit_pes(pid, self._pes.pop(pid), out)
            elif (not length and stuffed and self.eager_video
                  and pid == self.video_pid):
                self._emit_pes(pid, self._pes.pop(pid), out)
                self._ended.add(pid)

    def _unwrap(self, pid: int, pts: int) -> int:
        last, base = self._last_pts.get(pid, (None, 0))
        if last is not None and pts < last - _PTS_HALF:
            base += _PTS_WRAP
        self._last_pts[pid] = (pts, base)
        return base + pts

    def _emit_pes(self, pid: int, pes: bytearray, out: List[TsUnit]) -> None:
        if len(pes) < 9 or pes[0] != 0 or pes[1] != 0 or pes[2] != 1:
            return
        hlen = pes[8]
        pts: Optional[int] = None
        if pes[7] & 0x80 and len(pes) >= 14:
            pts = self._unwrap(pid, _pts_at(pes, 9))
        elif pid in self._last_pts:
            last, base = self._last_pts[pid]
            pts = base + last
        data = bytes(pes[9 + hlen:])
        if not data or pts is None:
            return
        if pid == self.video_pid:
            self._emit_video(data, pts, out)
        else:
            self._emit_audio(data, pts, out)

    def _emit_video(self, data: bytes, pts: int, out: List[TsUnit]) -> None:
        units = self.video.feed(data) + self.video.close()
        for payload, key in units:
            out.append(TsUnit('video', payload, key, pts))
        cfg = self.video.config
        if cfg is not None and cfg != self._video_config:
            if self._video_config is not None:
                self.config_changed = True
            self._video_config = cfg

    def _emit_audio(self, data: bytes, pts: int, out: List[TsUnit]) -> None:
        if self.audio_codec == 'opus':
            self._emit_opus(data, pts, out)
            return
        for i, (asc, rate, chan, au) in enumerate(adts_split(data)):
            if asc != self.audio_asc:
                if self.audio_asc is not None:
                    self.config_changed = True
                self.audio_asc = asc
                self.audio_samplerate = rate
                self.audio_channels = chan
            if self.audio_frame_samples is None:
                self.audio_frame_samples = _AAC_FRAME_SAMPLES
            out.append(TsUnit('audio', au, True,
                              pts + i * _AAC_FRAME_SAMPLES * 90_000 // rate))

    def _emit_opus(self, data: bytes, pts: int, out: List[TsUnit]) -> None:
        samples = 0
        for pkt in opus_split(data):
            if not pkt:
                continue
            channels = self._opus_channels or (2 if pkt[0] & 0x04 else 1)
            if channels != self.audio_channels:
                if self.audio_channels is not None:
                    self.config_changed = True
                self.audio_samplerate = 48000
                self.audio_channels = channels
            if self.audio_frame_samples is None:
                self.audio_frame_samples = opus_samples(pkt)
            out.append(TsUnit('audio', pkt, True,
                              pts + samples * 90_000 // 48_000))
            samples += opus_samples(pkt)
