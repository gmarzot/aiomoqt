"""A stand-in for the part of cymedia's Python API that aiomoqt uses, so the
ABR ladder and abr_ingest run in tests without FFmpeg.

Each push() becomes one output per branch at once, carrying its branch and
object numbers. H.264: Annex B, SPS, PPS and an IDR slice at a group start,
else a P slice. AV1: a temporal delimiter, a sequence header at a group
start, then a frame OBU.

Source('fake:...') plays SOURCE_S seconds of length-prefixed H.264 at
SOURCE_FPS with a key frame every SOURCE_GOP frames, and ADTS AAC-LC
48 kHz stereo, each packet at its timestamp."""
import os
import threading
import time
from typing import List, NamedTuple, Optional

_SC = b'\x00\x00\x00\x01'


class Output(NamedTuple):
    branch: int
    group: int
    object: int
    pts_us: int
    key: bool
    group_start: bool
    data: bytes


class Rung(NamedTuple):
    width: int
    height: int
    bit_rate: int


def sps(branch: int) -> bytes:
    return bytes([0x67, 0x42, 0xC0, 0x1E, branch])


PPS = bytes([0x68, 0xCE, 0x3C, 0x80])
# av1_nvenc's sequence header for 1280x720, 8-bit 4:2:0, level 4.0.
AV1_SEQUENCE_HEADER = bytes.fromhex("0a0b00000042a67fd9e010cc02")
_AV1_TD = b'\x12\x00'


def set_ffmpeg_log_level(level: int) -> None:
    pass


def have_encoder(name: str) -> bool:
    return True


class Graph:
    def __init__(self, nbranches: int, realtime: bool = True, codec: str = 'h264'):
        self.nbranches = nbranches
        self.realtime = realtime
        self.codec = codec
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        self._queued: List[Output] = []
        self._last_group = None
        self._flushing = False
        self._closed = False
        self._out = [0] * nbranches
        self._delays: List[List[int]] = [[] for _ in range(nbranches)]
        self.packets_in = 0

    def push(self, data, group, obj, pts_us, key, block=None) -> bool:
        if self._closed or self._flushing:
            raise RuntimeError("graph is flushing or failed")
        start = bool(key) and group != self._last_group
        if key:
            self._last_group = group
        self.packets_in += 1
        for b in range(self.nbranches):
            body = bytes([b, obj & 0xFF]) + bytes(data[:8])
            if self.codec == 'av1':
                frame = bytes([0x32, len(body)]) + body
                au = _AV1_TD + (AV1_SEQUENCE_HEADER if start else b'') + frame
            else:
                nal = bytes([0x65 if start else 0x41]) + body
                au = (_SC + sps(b) + _SC + PPS if start else b'') + _SC + nal
            self._queued.append(Output(b, group, obj, pts_us, start, start, au))
            self._out[b] += 1
            self._delays[b].append(1_000_000)
        os.write(self._w, b'.')
        return True

    def read(self, timeout=None) -> List[Output]:
        try:
            os.read(self._r, 65536)
        except BlockingIOError:
            pass
        out, self._queued = self._queued, []
        return out

    def fileno(self) -> int:
        return self._r

    def flush(self, timeout=None) -> bool:
        self._flushing = True
        return True

    def stats(self) -> dict:
        return dict(packets_in=self.packets_in, packets_dropped=0,
                    frames_decoded=self.packets_in, decode_errors=0,
                    branches=[dict(frames_in=n, frames_out=n, frames_dropped=0,
                                   groups_abandoned=0, errors=0)
                              for n in self._out])

    def delays(self, branch: int) -> List[int]:
        return list(self._delays[branch])

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._r)
            os.close(self._w)


def ladder(rungs, *, profile='nvenc', codec='h264', fps=(60, 1), realtime=True,
           **graph_kw) -> Graph:
    return Graph(len(list(rungs)), realtime=realtime, codec=codec)


class StreamInfo(NamedTuple):
    codec: str
    extradata: Optional[bytes] = None
    length_prefixed: bool = False
    width: int = 0
    height: int = 0
    fps: Optional[tuple] = None
    sample_rate: int = 0
    channels: int = 0
    bit_rate: int = 0


class Packet(NamedTuple):
    stream: str
    group: int
    object: int
    pts_us: int
    key: bool
    data: bytes


SOURCE_S, SOURCE_FPS, SOURCE_GOP = 4, 30, 15
AAC_PAYLOAD = 16


def _avcc() -> bytes:
    s = sps(9)
    return (bytes([1, s[1], s[2], s[3], 0xFF, 0xE1]) + len(s).to_bytes(2, 'big') + s
            + bytes([1]) + len(PPS).to_bytes(2, 'big') + PPS)


def _adts(payload: bytes) -> bytes:
    n = 7 + len(payload)  # AAC-LC, 48 kHz, 2 channels, no CRC
    return bytes([0xFF, 0xF1, 0x4C, 0x80 | (n >> 11), (n >> 3) & 0xFF,
                  ((n & 7) << 5) | 0x1F, 0xFC]) + payload


class Source:
    def __init__(self, url, options=None, *, timeout=None):
        if not str(url).startswith('fake:'):
            raise OSError(f"{url}: No such file or directory")
        self.url = url
        self.video = StreamInfo('h264', _avcc(), True, 320, 240, (SOURCE_FPS, 1))
        self.audio = StreamInfo('aac', None, sample_rate=48000, channels=2)
        self.ended = False
        self.error = None
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        self._queued: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _packets(self) -> list:
        out = []
        group = -1
        for n in range(SOURCE_S * SOURCE_FPS):
            key = n % SOURCE_GOP == 0
            group += key
            data = (4).to_bytes(4, 'big') + bytes([0x65 if key else 0x41, n & 0xFF, 0, 0])
            out.append(Packet('video', group, n % SOURCE_GOP, n * 1_000_000 // SOURCE_FPS,
                              key, data))
        for n in range(SOURCE_S * 48000 // 1024):
            out.append(Packet('audio', n, 0, n * 1024 * 1_000_000 // 48000, True,
                              _adts(bytes([n & 0xFF]) * AAC_PAYLOAD)))
        return sorted(out, key=lambda p: p.pts_us)

    def start(self, graph=None, *, passthrough=True, pace=None):
        threading.Thread(target=self._play, daemon=True).start()

    def _play(self):
        t0 = time.monotonic()
        for p in [*self._packets(), None]:
            if p is not None:
                wait = t0 + p.pts_us / 1e6 - time.monotonic()
                if wait > 0 and self._stop.wait(wait):
                    return
            with self._lock:
                self._queued.append(p)
            try:
                os.write(self._w, b'.')
            except OSError:
                return

    def read(self, timeout=None) -> List[Packet]:
        try:
            os.read(self._r, 65536)
        except (BlockingIOError, OSError):
            pass
        with self._lock:
            got, self._queued = self._queued, []
        if None in got:
            self.ended = True
        return [p for p in got if p is not None]

    def fileno(self) -> int:
        return self._r

    def close(self):
        if not self._stop.is_set():
            self._stop.set()
            os.close(self._r)
            os.close(self._w)
