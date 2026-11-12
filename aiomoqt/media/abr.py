"""ABR ladder for a LOC H.264 video track, encoded with cymedia as H.264 or
AV1.

Source frames in, LOC frames per rung out. Every rung keeps the source's
group and object IDs and timestamps and opens each group on a key frame,
so equally numbered groups line up across the ladder (msf §4.2). Nothing
here knows about sessions or peers.

AbrLadder and SourceInput need cymedia: `uv pip install 'aiomoqt[abr]'`.
"""
from __future__ import annotations

import asyncio
import functools
import importlib
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .catalog import PACKAGING_LOC, Catalog, CatalogTrack, InitData
from .cmaf import asc_info
from .loc import LocFrame
from .mpegts import adts_split
from .sources import (
    av1c_codec_string, av1c_from_sequence_header, avcc_codec_string, avcc_from_sps_pps,
    obu_units, split_annexb,
)

logger = get_logger(__name__)

_SPS, _PPS, _AUD, _FILLER = 7, 8, 9, 12
_OBU_SEQUENCE_HEADER, _OBU_TEMPORAL_DELIMITER, _OBU_PADDING = 1, 2, 15
CODECS = ('h264', 'av1')


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


def loc_from_obus(tu: bytes) -> Tuple[bytes, Optional[bytes]]:
    """One AV1 temporal unit as a LOC payload, its OBUs less temporal
    delimiters and padding, plus its sequence header OBU for the decoder
    config. A key frame keeps its sequence header."""
    units = obu_units(tu)
    seq = next((u for t, u in units if t == _OBU_SEQUENCE_HEADER), None)
    return b''.join(u for t, u in units
                    if t not in (_OBU_TEMPORAL_DELIMITER, _OBU_PADDING)), seq


def codec_string(config: bytes) -> str:
    """The codec string of an avcC or av1C."""
    return av1c_codec_string(config) if config[0] == 0x81 else avcc_codec_string(config)


def ladder_catalog(source: Catalog, video: str, rungs: Sequence[Rung],
                   configs: Sequence[bytes]) -> Catalog:
    """The source catalog with each rung after `video`, in its altGroup:
    a copy of the video entry with the rung's name, size, bitrate, codec
    and decoder config (avcC or av1C)."""
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
            'codec': codec_string(cfg), 'width': rung.width,
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
    """Encodes a LOC H.264 video track into H.264 or AV1 rungs with cymedia.

    push() takes each source frame with its group and object IDs; the
    payload is length-prefixed when `config` (avcC) is given, else Annex B.
    After start(), on_frame(rung, frame, group_id, object_id, pushed_ns)
    gets each rung frame with its source frame's IDs, timestamp and
    extensions, and a key frame at each group start. pushed_ns is the
    source frame's monotonic push time, or None. configs[i] is rung i's
    avcC or av1C; wait[i] meters it from push to on_frame. A lagging rung skips the
    rest of a group, and a full engine drops whole input groups. A frame of a group
    older than one already pushed is dropped (late_dropped): decoding it would stall
    the decoder until the next key frame. A group start more than one group back is
    a restarted source and is taken.
    """

    def __init__(self, rungs: Sequence[Rung], *, config: Optional[bytes],
                 fps: Tuple[int, int], on_frame: Callable,
                 profile: str = 'nvenc', codec: str = 'h264',
                 timescale: Optional[int] = None, realtime: bool = True):
        cymedia = _cymedia("the ABR ladder")
        if not rungs:
            raise ValueError("no rungs")
        if codec not in CODECS:
            raise ValueError(f"codec {codec!r}: one of {', '.join(CODECS)}")
        self.codec = codec
        self.rungs = list(rungs)
        self.timescale = timescale
        self.on_frame = on_frame
        self.configs: List[Optional[bytes]] = [None] * len(self.rungs)
        self.wait = [DelayMeter() for _ in self.rungs]
        self.frames_in = 0
        self.input_dropped = 0
        self.late_dropped = 0
        self._graph = cymedia.ladder(
            [cymedia.Rung(r.width, r.height, r.bit_rate) for r in self.rungs],
            profile=profile, fps=fps, extradata=config, realtime=realtime,
            **({'codec': codec} if codec != 'h264' else {}))
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
        """Queue one source frame; False when it is late, the engine dropped
        it or the ladder is flushing."""
        if self._flushing:
            return False
        if group_id < self._group:
            if object_id != 0 or group_id + 1 == self._group:
                self.late_dropped += 1
                return False
            self._group = group_id - 1
            self._pending.clear()
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
            config = self._package(out.data)
            payload = config[0]
            if config[1] is not None:
                self.configs[i] = config[1]
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

    def _package(self, data: bytes) -> Tuple[bytes, Optional[bytes]]:
        """A rung frame's LOC payload, and its decoder config if it has one."""
        if self.codec == 'av1':
            payload, seq = loc_from_obus(data)
            return payload, (av1c_from_sequence_header(seq) if seq is not None else None)
        payload, sps, pps = loc_from_annexb(data)
        config = avcc_from_sps_pps(sps, pps) if sps is not None and pps is not None else None
        return payload, config

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


def _cymedia(what: str):
    try:
        return importlib.import_module("cymedia")
    except ImportError as e:
        raise ImportError(f"{what} needs cymedia: "
                          "uv pip install 'aiomoqt[abr]'") from e


@dataclass
class InputTrack:
    """A SourceInput track: decoder config (avcC, AudioSpecificConfig or
    None), codec string and timescale (None: microseconds)."""
    config: Optional[bytes]
    codec_string: str
    timescale: Optional[int] = None
    properties: Dict[int, Any] = field(default_factory=dict)


class SourceInput:
    """What cymedia's Source reads (a file, or a live SRT, RTMP, RTSP or
    HLS input) as a LOC broadcast: `catalog` of its H.264 video and AAC or
    Opus audio, `tracks`, and after start(on_frame), on_frame(name, frame,
    group_id, object_id) per frame. Video keeps cymedia's group and object
    numbers; each audio frame is a group. Timestamps are the input's, in
    microseconds since the Unix epoch from the time its first packet was
    read. AAC loses ADTS headers. open() returns once the first video key
    frame and the audio's decoder config have been read."""

    def __init__(self, src):
        v, a = src.video, src.audio
        if v is None:
            raise ValueError(f"{src.url}: no video stream")
        if v.codec != 'h264' or not v.extradata:
            raise ValueError(f"{src.url}: video is {v.codec}, not H.264")
        self.src = src
        self.tracks: Dict[str, InputTrack] = {
            'video': InputTrack(v.extradata, avcc_codec_string(v.extradata))}
        self.catalog: Catalog  # set by open()
        self.ended: asyncio.Future = asyncio.get_running_loop().create_future()
        self._audio = a.codec if a is not None and a.codec in ('aac', 'opus') else None
        if a is not None and self._audio is None:
            logger.warning(f"SourceInput: {src.url}: {a.codec} audio left out")
        self._adts = self._audio == 'aac' and not a.extradata
        if self._audio == 'aac' and a.extradata:
            self._set_audio(a.extradata, *asc_info(a.extradata)[:2])
        elif self._audio == 'opus':
            self.tracks['audio'] = InputTrack(None, 'opus')
            self._audio_format = (a.sample_rate or 48000, a.channels)
        self._audio_group = 0
        self._t0: Optional[int] = None
        self._keyed = False
        self._primed = asyncio.Event()
        self._held: list = []
        self.on_frame: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    async def open(cls, url: str, options: Optional[Dict[str, str]] = None,
                   *, timeout: float = 10.0) -> "SourceInput":
        cymedia = _cymedia("SourceInput")
        loop = asyncio.get_running_loop()
        src = await loop.run_in_executor(
            None, functools.partial(cymedia.Source, url, options, timeout=timeout))
        inp = None
        try:
            inp = cls(src)
            src.start(passthrough=True)
            inp._loop = loop
            loop.add_reader(src.fileno(), inp._drain)
            await asyncio.wait_for(inp._primed.wait(), timeout)
            if not inp._keyed:
                raise ValueError(f"{url}: ended before a video key frame"
                                 f"{f': {src.error}' if src.error else ''}")
        except BaseException:
            if inp is not None:
                inp.close()
            else:
                src.close()
            raise
        inp.catalog = inp._catalog()
        return inp

    def start(self, on_frame: Callable) -> None:
        """Deliver the frames read so far, then each as it is read."""
        held, self._held = self._held, []
        self.on_frame = on_frame
        for frame in held:
            on_frame(*frame)

    def properties(self, name: str) -> Dict[int, Any]:
        return {}

    async def async_closed(self) -> None:
        """Returns when the input ends or fails."""
        await asyncio.shield(self.ended)

    def close(self) -> None:
        if self._loop is not None:
            self._loop.remove_reader(self.src.fileno())
            self._loop = None
        self.src.close()
        if not self.ended.done():
            self.ended.set_result(None)

    def _set_audio(self, asc: bytes, rate: int, channels: int) -> None:
        self.tracks['audio'] = InputTrack(asc, f"mp4a.40.{asc[0] >> 3}")
        self._audio_format = (rate, channels)

    def _emit(self, name: str, frame: LocFrame, group_id: int,
              object_id: int) -> None:
        on_frame = self.on_frame
        if on_frame is None:
            self._held.append((name, frame, group_id, object_id))
        else:
            on_frame(name, frame, group_id, object_id)

    def _drain(self) -> None:
        for p in self.src.read(0):
            if self._t0 is None:
                self._t0 = time.time_ns() // 1000
            ts = self._t0 + p.pts_us
            if p.stream == 'video':
                self._keyed = self._keyed or p.key
                self._emit('video', LocFrame(p.data, key_frame=p.key, timestamp=ts),
                           p.group, p.object)
            elif self._adts:
                for asc, rate, channels, au in adts_split(p.data):
                    if 'audio' not in self.tracks:
                        self._set_audio(asc, rate, channels)
                    self._audio_frame(au, ts)
                    ts += 1024 * 1_000_000 // (rate or 48000)
            elif self._audio is not None:
                self._audio_frame(p.data, ts)
        if self.src.ended:
            self._primed.set()
            if not self.ended.done():
                self.ended.set_result(self.src.error)
        elif self._keyed and (self._audio is None or 'audio' in self.tracks):
            self._primed.set()

    def _audio_frame(self, data: bytes, ts: int) -> None:
        self._emit('audio', LocFrame(data, key_frame=True, timestamp=ts),
                   self._audio_group, 0)
        self._audio_group += 1

    def _catalog(self) -> Catalog:
        v = self.src.video
        tracks = [CatalogTrack(
            name='video', packaging=PACKAGING_LOC, isLive=True, role='video',
            renderGroup=1, codec=self.tracks['video'].codec_string,
            width=v.width or None, height=v.height or None,
            framerate=v.fps[0] / v.fps[1] if v.fps else None,
            bitrate=v.bit_rate or None, initRef='v0')]
        init = [InitData.from_bytes('v0', v.extradata)]
        audio = self.tracks.get('audio')
        if audio is not None:
            rate, channels = self._audio_format
            tracks.append(CatalogTrack(
                name='audio', packaging=PACKAGING_LOC, isLive=True, role='audio',
                renderGroup=1, codec=audio.codec_string, samplerate=rate or None,
                channelConfig=str(channels) if channels else None,
                bitrate=self.src.audio.bit_rate or None,
                initRef='a0' if audio.config else None))
            if audio.config:
                init.append(InitData.from_bytes('a0', audio.config))
        return Catalog(generatedAt=int(time.time() * 1000), tracks=tracks,
                       initDataList=init)
