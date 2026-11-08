"""ABR ladder for a LOC H.264 video track, encoded with cymedia.

Source frames in, LOC frames per rung out. Every rung keeps the source's
group and object IDs and timestamps and opens each group on a key frame,
so equally numbered groups line up across the ladder (msf §4.2). Nothing
here knows about sessions or peers.

AbrLadder needs cymedia: `uv pip install git+https://github.com/gmarzot/cymedia`.
"""
from __future__ import annotations

import asyncio
import importlib
import struct
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .catalog import Catalog, CatalogTrack, InitData
from .loc import LocFrame
from .sources import avcc_codec_string, avcc_from_sps_pps, split_annexb

_SPS, _PPS, _AUD, _FILLER = 7, 8, 9, 12


@dataclass(frozen=True)
class Rung:
    width: int
    height: int
    bit_rate: int

    @property
    def name(self) -> str:
        return f"video_{self.height}p"

    @classmethod
    def parse(cls, spec: str) -> "Rung":
        """'WxH:BPS', the rate with an optional k or M suffix."""
        try:
            size, rate = spec.split(':')
            w, h = (int(x) for x in size.lower().split('x'))
            mult = {'k': 1_000, 'm': 1_000_000}.get(rate[-1:].lower(), 1)
            bps = int(float(rate[:-1] if mult > 1 else rate) * mult)
        except ValueError:
            raise ValueError(f"rung {spec!r}: expected WxH:BPS, "
                             f"e.g. 1280x720:3.75M") from None
        if w <= 0 or h <= 0 or w % 2 or h % 2 or bps <= 0:
            raise ValueError(f"rung {spec!r}: needs even, positive "
                             f"dimensions and a positive rate")
        return cls(w, h, bps)


DEFAULT_RUNGS = (
    Rung(2560, 1440, 13_500_000), Rung(1920, 1080, 6_750_000),
    Rung(1280, 720, 3_750_000), Rung(960, 540, 2_250_000),
    Rung(640, 360, 900_000), Rung(320, 180, 300_000),
)


def rungs_below(height: int, rungs: Sequence[Rung] = DEFAULT_RUNGS
                ) -> List[Rung]:
    """The rungs shorter than the source."""
    return [r for r in rungs if r.height < height]


def frame_rate(framerate: float) -> Tuple[int, int]:
    """A catalog framerate as a fraction, 29.97 as 30000/1001."""
    if not framerate or framerate <= 0:
        raise ValueError(f"framerate {framerate!r}")
    f = Fraction(framerate).limit_denominator(1001)
    if f.denominator not in (1, 1001):
        ntsc = Fraction(round(framerate * 1.001) * 1000, 1001)
        if abs(float(ntsc) - framerate) < 0.01:
            f = ntsc
    return f.numerator, f.denominator


def loc_from_annexb(au: bytes) -> Tuple[bytes, Optional[bytes],
                                         Optional[bytes]]:
    """One Annex-B access unit as a LOC payload in mp4-sample form (4-byte
    length prefixes, no parameter sets, AUD or filler), plus its SPS and
    PPS for the decoder config."""
    sps = pps = None
    parts = []
    for nal in split_annexb(au):
        t = nal[0] & 0x1F
        if t == _SPS:
            sps = nal
        elif t == _PPS:
            pps = nal
        elif t not in (_AUD, _FILLER):
            parts.append(struct.pack('>I', len(nal)) + nal)
    return b''.join(parts), sps, pps


def ladder_catalog(source: Catalog, video: str, rungs: Sequence[Rung],
                   configs: Sequence[bytes]) -> Catalog:
    """The source catalog with each rung after `video`, in its altGroup:
    a copy of the video entry with the rung's name, size, bitrate, codec
    and decoder config."""
    cat = Catalog.from_json(source.to_json())
    src = cat.find(video)
    if src is None:
        raise ValueError(f"catalog has no track {video!r}")
    if src.altGroup is None:
        src.altGroup = 1 + max((t.altGroup or 0 for t in cat.tracks),
                               default=0)
    taken = {i.id for i in cat.initDataList or ()}
    entries, init = [], []
    for i, (rung, cfg) in enumerate(zip(rungs, configs)):
        ref = next(r for r in (f"abr{i}", *(f"abr{i}_{n}" for n in
                                            range(1, 100)))
                   if r not in taken)
        entries.append(CatalogTrack.from_dict({
            **src.to_dict(), 'name': rung.name, 'label': f"{rung.height}p",
            'codec': avcc_codec_string(cfg), 'width': rung.width,
            'height': rung.height, 'bitrate': rung.bit_rate,
            'initRef': ref}))
        init.append(InitData.from_bytes(ref, cfg))
    at = cat.tracks.index(src) + 1
    cat.tracks[at:at] = entries
    cat.initDataList = (cat.initDataList or []) + init
    return cat


class DelayMeter:
    """The most recent delays, in ns."""

    def __init__(self, cap: int = 8192):
        self._d: deque = deque(maxlen=cap)

    def add(self, ns: int) -> None:
        self._d.append(ns)

    def take(self) -> List[int]:
        """The delays since the last take()."""
        d = list(self._d)
        self._d.clear()
        return d


def quantiles_ms(ns: Sequence[int]) -> Optional[Tuple[float, float, float]]:
    """(p50, p99, max) in ms; None when empty."""
    if not ns:
        return None
    s = sorted(ns)
    pick = lambda q: s[min(len(s) - 1, int(len(s) * q))] / 1e6  # noqa: E731
    return pick(0.5), pick(0.99), s[-1] / 1e6


class AbrLadder:
    """Encodes a LOC H.264 video track into rungs with cymedia.

    push() takes each source frame with its group and object IDs; the
    payload is length-prefixed when `config` (avcC) is given, else Annex B.
    After start(), on_frame(rung, frame, group_id, object_id, pushed_ns)
    gets each rung frame with its source frame's IDs, timestamp and
    extensions, and a key frame at each group start. pushed_ns is the
    source frame's monotonic push time, or None. configs[i] is rung i's
    avcC; wait[i] meters it from push to on_frame. A lagging rung skips the
    rest of a group, and a full engine drops whole input groups.
    """

    def __init__(self, rungs: Sequence[Rung], *, config: Optional[bytes],
                 fps: Tuple[int, int], on_frame: Callable,
                 profile: str = 'nvenc', timescale: Optional[int] = None,
                 realtime: bool = True):
        try:
            cymedia = importlib.import_module("cymedia")
        except ImportError as e:
            raise ImportError("the ABR ladder needs cymedia: uv pip install "
                              "git+https://github.com/gmarzot/cymedia") from e
        if not rungs:
            raise ValueError("no rungs")
        self.rungs = list(rungs)
        self.timescale = timescale
        self.on_frame = on_frame
        self.configs: List[Optional[bytes]] = [None] * len(self.rungs)
        self.wait = [DelayMeter() for _ in self.rungs]
        self.frames_in = 0
        self.input_dropped = 0
        self._graph = cymedia.ladder(
            [cymedia.Rung(r.width, r.height, r.bit_rate) for r in self.rungs],
            profile=profile, fps=fps, extradata=config, realtime=realtime)
        # (group, object) -> [timestamp, extensions, push ns, rungs to go]
        self._pending: Dict[Tuple[int, int], list] = {}
        self._group = -1
        self._engine_seen = [0] * len(self.rungs)
        self._ready = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._flushing = False

    @property
    def ready(self) -> asyncio.Event:
        """Set once every rung has its decoder config."""
        return self._ready

    def rung_configs(self) -> List[bytes]:
        """Every rung's avcC; raises before `ready`."""
        configs = [c for c in self.configs if c is not None]
        if len(configs) != len(self.rungs):
            raise RuntimeError("a rung has no decoder config yet")
        return configs

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._loop = loop or asyncio.get_running_loop()
        self._loop.add_reader(self._graph.fileno(), self._drain)

    def push(self, frame: LocFrame, group_id: int, object_id: int) -> bool:
        """Queue one source frame; False when the engine dropped it or is
        flushing."""
        if self._flushing:
            return False
        ts = frame.timestamp
        pts = (0 if ts is None
               else ts * 1_000_000 // self.timescale if self.timescale
               else ts)
        if group_id > self._group:
            self._group = group_id
            for k in [k for k in self._pending if k[0] < group_id - 1]:
                del self._pending[k]
        key = (group_id, object_id)
        self._pending[key] = [ts, frame.extensions, time.monotonic_ns(),
                              len(self.rungs)]
        if not self._graph.push(frame.payload, group_id, object_id, pts,
                                object_id == 0):
            del self._pending[key]
            self.input_dropped += 1
            return False
        self.frames_in += 1
        return True

    def _drain(self) -> None:
        for out in self._graph.read(0):
            i = out.branch
            payload, sps, pps = loc_from_annexb(out.data)
            if sps is not None and pps is not None:
                self.configs[i] = avcc_from_sps_pps(sps, pps)
                if not self._ready.is_set() and all(self.configs):
                    self._ready.set()
            key = (out.group, out.object)
            p = self._pending.get(key)
            ts, exts, pushed = out.pts_us, None, None
            if p is not None:
                ts, exts, pushed = p[0], p[1], p[2]
                self.wait[i].add(time.monotonic_ns() - pushed)
                p[3] -= 1
                if p[3] == 0:
                    del self._pending[key]
            self.on_frame(i, LocFrame(payload, key_frame=out.group_start,
                                      timestamp=ts, extensions=exts,
                                      group_id=out.group),
                          out.group, out.object, pushed)

    def engine_delays(self, i: int) -> List[int]:
        """Rung i's push-to-output engine delays since the last call, ns."""
        done = self._graph.stats()['branches'][i]['frames_out']
        new = done - self._engine_seen[i]
        self._engine_seen[i] = done
        return self._graph.delays(i)[-new:] if new > 0 else []

    def stats(self) -> dict:
        return self._graph.stats()

    async def flush(self, timeout: float = 5.0) -> bool:
        """End input and deliver every frame still in the engine."""
        self._flushing = True
        loop = self._loop or asyncio.get_running_loop()
        done = await loop.run_in_executor(None, self._graph.flush, timeout)
        self._drain()
        return done

    def close(self) -> None:
        if self._loop is not None:
            self._loop.remove_reader(self._graph.fileno())
            self._loop = None
        self._graph.close()
