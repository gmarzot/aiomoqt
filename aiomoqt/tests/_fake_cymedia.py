"""A stand-in for the part of cymedia's Python API that aiomoqt uses, so the
ABR ladder and abr_ingest run in tests without FFmpeg.

Each push() becomes one Annex-B output per branch at once: SPS, PPS and an
IDR slice at a group start, else a P slice; each carries its branch and
object numbers."""
import os
from typing import List, NamedTuple

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


def set_ffmpeg_log_level(level: int) -> None:
    pass


def have_encoder(name: str) -> bool:
    return True


class Graph:
    def __init__(self, nbranches: int, realtime: bool = True):
        self.nbranches = nbranches
        self.realtime = realtime
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
            nal = bytes([0x65 if start else 0x41, b, obj & 0xFF]) + bytes(data[:8])
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


def ladder(rungs, *, profile='nvenc', fps=(60, 1), realtime=True, **graph_kw) -> Graph:
    return Graph(len(list(rungs)), realtime=realtime)
