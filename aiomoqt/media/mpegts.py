"""MPEG-TS demux for live ingest: PAT/PMT → PES → H.264 access units
and AAC frames, with the PES presentation timestamps preserved.

One pipe (`ffmpeg … -c copy -f mpegts -`) carries both tracks with the
encoder's own timing, so A/V sync and frame pacing follow the source
rather than pipe arrival. Stream types: 0x1B (H.264 Annex-B) and 0x0F
(AAC ADTS); others are reported once and ignored.
"""
import logging
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from .sources import _ADTS_FREQ, AnnexBAssembler

logger = logging.getLogger(__name__)

TS_PACKET = 188
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_AAC_ADTS = 0x0F
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
    constant offset. A backward jump beyond `restart_s` is an encoder
    restart and re-anchors."""

    def __init__(self, restart_s: float = 5.0,
                 now: Callable[[], float] = time.time):
        self._now = now
        self._restart = int(restart_s * 90_000)
        self._pts0: Optional[int] = None
        self._last = 0
        self._wall0_us = 0

    def to_us(self, pts: int) -> int:
        if self._pts0 is None or pts < self._last - self._restart:
            self._pts0 = pts
            self._last = pts
            self._wall0_us = int(self._now() * 1_000_000)
        elif pts > self._last:
            self._last = pts
        return self._wall0_us + (pts - self._pts0) * 1000 // 90


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


def _pts_at(b: bytes, i: int) -> int:
    return ((((b[i] >> 1) & 7) << 30) | (b[i + 1] << 22)
            | (((b[i + 2] >> 1) & 0x7F) << 15) | (b[i + 3] << 7) | (b[i + 4] >> 1))


class TsDemuxer:
    """Incremental transport-stream demuxer. feed() returns the access
    units completed by the chunk; close() flushes the last PES at EOF.

    Video: each PES is fed through an AnnexBAssembler and closed, so the
    units it held take the PES PTS (one AU per PES in practice); SPS/PPS
    land in `video.config`. Audio: ADTS frames are split, the
    AudioSpecificConfig derived from the header, and each frame's PTS
    advanced by 1024 samples within the PES.

    `config_changed` is raised (and left for the caller to clear) when a
    later SPS/PPS or ADTS header differs from the first."""

    def __init__(self, clock: Optional[TsClock] = None):
        self.clock = clock or TsClock()
        self.video = AnnexBAssembler()
        self.video_pid: Optional[int] = None
        self.audio_pid: Optional[int] = None
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
        if afc & 2:
            off += 1 + pkt[4]
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
            self._es(pid, payload, pusi, out)

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
            i += 5 + es_len
            if stype == STREAM_TYPE_H264 and video is None:
                video = pid
            elif stype == STREAM_TYPE_AAC_ADTS and audio is None:
                audio = pid
            elif stype not in self.unsupported:
                self.unsupported.add(stype)
                logger.warning(f"mpegts: stream type {stype:#x} on pid "
                               f"{pid} ignored")
        self.video_pid, self.audio_pid = video, audio
        self.pmt_seen = True

    # ── PES ─────────────────────────────────────────────────────────

    def _es(self, pid: int, payload: bytes, pusi: bool,
            out: List[TsUnit]) -> None:
        if pusi:
            prev = self._pes.pop(pid, None)
            if prev is not None:
                self._emit_pes(pid, prev, out)
            self._skip.discard(pid)
            self._pes[pid] = bytearray(payload)
        elif pid in self._skip or pid not in self._pes:
            return
        else:
            self._pes[pid] += payload
        # A bounded PES (audio) is complete as soon as its length is in.
        pes = self._pes[pid]
        if len(pes) >= 6:
            length = (pes[4] << 8) | pes[5]
            if length and len(pes) >= 6 + length:
                self._emit_pes(pid, self._pes.pop(pid), out)

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
        for i, (asc, rate, chan, au) in enumerate(adts_split(data)):
            if asc != self.audio_asc:
                if self.audio_asc is not None:
                    self.config_changed = True
                self.audio_asc = asc
                self.audio_samplerate = rate
                self.audio_channels = chan
            out.append(TsUnit('audio', au, True,
                              pts + i * _AAC_FRAME_SAMPLES * 90_000 // rate))
