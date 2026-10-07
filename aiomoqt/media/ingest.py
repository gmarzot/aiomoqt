"""Media ingest via PyAV: anything FFmpeg opens (HLS, DASH, files, ...)
re-fragmented per track into CMAF chunks for CMSF publishing.

Each selected input stream gets its own mp4 muxer writing one sample per
moof+mdat (cmsf §3.3) into memory. Every chunk's tfdt and composition
offset are then set from the source packet, so all tracks share the
input's timeline exactly. Groups open at key frames, numbered by key
frames since the track's first one, so renditions with aligned GOPs keep
equally numbered groups time-aligned (msf §4.2).

Needs the `media` extra (PyAV); load_av() raises ImportError naming it.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import struct
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import AsyncIterator, Deque, Dict, List, Optional, Tuple

from ..utils.logger import get_logger
from .cmaf import (
    init_codec_string, init_timescale, set_chunk_timing, strip_edit_lists,
)

logger = get_logger(__name__)

INSTALL_HINT = "pip install 'aiomoqt[media]'"
RENDITIONS = ('best', 'all')

# delay_moov: the header waits for the first packet, which carries the
# codec config for ADTS AAC and Annex-B video. The muxer re-bases each
# track (edit list plus tfdt); its edts is stripped and chunk timing set
# from the source instead.
_MUX_OPTIONS = {
    'movflags': 'cmaf+frag_every_frame+delay_moov+default_base_moof',
}
_VIDEO_CODECS = frozenset(('h264', 'hevc', 'av1'))
_AUDIO_CODECS = frozenset(('aac', 'opus'))
_PRIME_LIMIT = Fraction(10)  # media seconds to wait for every track's header
_QUEUE_SIZE = 256


class IngestError(RuntimeError):
    pass


def load_av():
    """Import PyAV, or raise ImportError naming the extra to install."""
    try:
        import av
    except ImportError as e:
        raise ImportError(f"media ingest needs PyAV: {INSTALL_HINT}") from e
    return av


@dataclass
class IngestChunk:
    """One CMAF chunk (moof+mdat) of one track."""
    track: str
    payload: bytes
    key: bool
    group_id: int
    time_us: int  # decode time on the input's timeline


@dataclass
class _RawChunk:
    track: 'IngestTrack'
    payload: bytearray
    key: bool
    group_id: int
    dts: Fraction  # seconds
    cto: Fraction  # seconds


class _Sink:
    """Write-only muxer output: non-seekable, so the muxer streams boxes
    instead of patching bytes already handed out."""

    def __init__(self):
        self.data = bytearray()

    def write(self, b) -> int:
        self.data += b
        return len(b)


def _rank(stream) -> Tuple[int, int]:
    """(bitrate, pixels): HLS/DASH variant bandwidth when the demuxer
    reports one, else the stream's own bitrate."""
    ctx = stream.codec_context
    try:
        variant = int((stream.metadata or {}).get('variant_bitrate') or 0)
    except ValueError:
        variant = 0
    pixels = (getattr(ctx, 'width', 0) or 0) * (getattr(ctx, 'height', 0) or 0)
    return (variant or ctx.bit_rate or 0, pixels)


def _variant(stream) -> Optional[str]:
    return (stream.metadata or {}).get('variant_bitrate')


class IngestTrack:
    """One selected input stream and its CMAF fragmenter. `init` (the
    CMAF header), `codec` and `timescale` are set once the first packet
    is muxed."""

    def __init__(self, av, stream, name: str, kind: str):
        self.stream = stream
        self.name = name
        self.kind = kind
        ctx = stream.codec_context
        # variant bandwidth spans the whole variant; audio has its own rate
        self.bitrate: Optional[int] = (
            _rank(stream)[0] if kind == 'video' else ctx.bit_rate) or None
        self.width = self.height = self.fps = None
        self.samplerate = self.channels = None
        if kind == 'video':
            self.width, self.height = ctx.width, ctx.height
            rate = stream.average_rate or stream.guessed_rate
            self.fps = round(float(rate), 3) if rate else None
        else:
            self.samplerate = ctx.sample_rate
            self.channels = ctx.layout.nb_channels
        self.init: Optional[bytes] = None
        self.codec: Optional[str] = None
        self.timescale: Optional[int] = None
        self.first_dts: Optional[Fraction] = None
        self._header = bytearray()
        self._sink = _Sink()
        self._out = av.open(self._sink, 'w', format='mp4', options=_MUX_OPTIONS)
        self._ostream = self._out.add_stream_from_template(stream)
        self._bsf = None
        if ctx.name == 'aac' and not ctx.extradata:  # ADTS (e.g. TS segments)
            from av.bitstream import BitStreamFilterContext
            self._bsf = BitStreamFilterContext('aac_adtstoasc', stream)
        self._pending: Deque[Tuple[bool, Fraction, Fraction]] = collections.deque()
        self._group = -1
        self._started = kind != 'video'  # video opens on a key frame
        self._closed = False

    def push(self, packet) -> List[_RawChunk]:
        """Mux one demuxed packet; returns the chunks it completed (the
        muxer emits each fragment when the next packet arrives)."""
        if not self._started:
            if not packet.is_keyframe:
                return []
            self._started = True
        for pkt in (self._bsf.filter(packet) if self._bsf else (packet,)):
            if pkt.dts is None:
                continue
            tb = Fraction(pkt.time_base)
            dts = pkt.dts * tb
            cto = (pkt.pts - pkt.dts) * tb if pkt.pts is not None else Fraction(0)
            if self.first_dts is None:
                self.first_dts = dts
            self._pending.append((self.kind != 'video' or pkt.is_keyframe, dts, cto))
            pkt.stream = self._ostream
            self._out.mux(pkt)
        return self._drain()

    def close(self) -> List[_RawChunk]:
        """Flush the last fragment."""
        if self._closed:
            return []
        self._closed = True
        self._out.close()
        return self._drain()

    def _drain(self) -> List[_RawChunk]:
        data = self._sink.data
        chunks = []
        pos = 0
        while pos + 8 <= len(data):
            size, btype = struct.unpack_from('>I4s', data, pos)
            if size < 8:
                raise IngestError(f"{self.name}: unsupported box size {size}")
            if pos + size > len(data):
                break
            if btype == b'moof':
                end = pos + size
                if end + 8 > len(data):
                    break
                mdat = struct.unpack_from('>I', data, end)[0]
                if end + mdat > len(data):
                    break
                key, dts, cto = self._pending.popleft()
                if key:
                    self._group += 1
                chunks.append(_RawChunk(self, bytearray(data[pos:end + mdat]),
                                        key, self._group, dts, cto))
                pos = end + mdat
                continue
            if self.init is None and btype in (b'ftyp', b'moov'):
                self._header += data[pos:pos + size]
                if btype == b'moov':
                    self.init = strip_edit_lists(bytes(self._header))
                    self.codec = init_codec_string(self.init)
                    self.timescale = init_timescale(self.init)
            pos += size  # other top-level boxes (mfra, ...) are dropped
        del data[:pos]
        return chunks


def _select(container, rendition: str, audio: bool):
    """(video streams, audio stream or None). best: the top-ranked video;
    all: every video, best first. Audio: the stream of the chosen video's
    HLS variant when there is one, else the top-ranked."""
    videos = []
    for s in container.streams.video:
        if s.codec_context.name in _VIDEO_CODECS:
            videos.append(s)
        else:
            logger.warning("ingest: skipping %s video stream %d",
                           s.codec_context.name, s.index)
    videos.sort(key=_rank, reverse=True)
    if rendition == 'best':
        videos = videos[:1]
    chosen = None
    if audio:
        audios = sorted((s for s in container.streams.audio
                         if s.codec_context.name in _AUDIO_CODECS),
                        key=_rank, reverse=True)
        variant = _variant(videos[0]) if videos else None
        chosen = next((s for s in audios
                       if variant is not None and _variant(s) == variant),
                      audios[0] if audios else None)
    return videos, chosen


def _video_names(streams) -> List[str]:
    if len(streams) == 1:
        return ['video']
    names: List[str] = []
    for s in streams:
        name = f"video-{s.codec_context.height}p"
        if name in names:
            name = f"{name}-{_rank(s)[0] // 1000}k"
        while name in names:
            name += '+'
        names.append(name)
    return names


class Ingest:
    """An opened input, its selected tracks, and the demux thread that
    feeds their CMAF chunks to the event loop.

    The constructor blocks on network I/O (open, then demux until every
    track has its CMAF header): run it in an executor."""

    def __init__(self, url: str, rendition: str = 'best', audio: bool = True,
                 timeout: Optional[Tuple[float, float]] = (15.0, 30.0)):
        if rendition not in RENDITIONS:
            raise ValueError(f"rendition must be one of {RENDITIONS}")
        av = load_av()
        self.url = url
        self._container = av.open(url, timeout=timeout)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._shift = Fraction(0)
        try:
            videos, aud = _select(self._container, rendition, audio)
            if not videos and aud is None:
                raise IngestError("no h264/hevc/av1 video or aac/opus audio stream")
            self.tracks: List[IngestTrack] = [
                IngestTrack(av, s, name, 'video')
                for s, name in zip(videos, _video_names(videos))]
            if aud is not None:
                self.tracks.append(IngestTrack(av, aud, 'audio', 'audio'))
            self._discard_unselected(av)
            self._demux = self._container.demux(*[t.stream for t in self.tracks])
            self._backlog = self._prime(av)
        except BaseException:
            self._container.close()
            raise

    def _by_index(self) -> Dict[int, IngestTrack]:
        return {t.stream.index: t for t in self.tracks}

    def _discard_unselected(self, av) -> None:
        """Unselected HLS/DASH renditions are then never fetched."""
        keep = {t.stream.index for t in self.tracks}
        for s in self._container.streams:
            if s.index not in keep:
                s.discard = av.stream.Discard.all

    def _prime(self, av) -> List[IngestChunk]:
        """Demux until every track has its CMAF header, returning the
        chunks read on the way. A track still without one after
        _PRIME_LIMIT of media is dropped. Fixes the timeline shift that
        keeps every decode time non-negative."""
        tracks = self._by_index()
        raw: List[_RawChunk] = []
        first = None
        for packet in self._demux:
            track = tracks.get(packet.stream.index)
            if track is None or packet.dts is None:
                continue
            dts = packet.dts * Fraction(packet.time_base)
            first = dts if first is None else first
            raw += track.push(packet)
            if all(t.init is not None for t in self.tracks):
                break
            if dts - first > _PRIME_LIMIT:
                break
        for t in [t for t in self.tracks if t.init is None]:
            logger.warning("ingest: no %s data from %s; track dropped",
                           t.name, self.url)
            self.tracks.remove(t)
        if not self.tracks:
            raise IngestError(f"no media read from {self.url}")
        self._discard_unselected(av)
        starts = [t.first_dts for t in self.tracks if t.first_dts is not None]
        self._shift = max(Fraction(0), -min(starts)) if starts else Fraction(0)
        return [self._finish(c) for c in raw if c.track in self.tracks]

    def _finish(self, c: _RawChunk) -> IngestChunk:
        """Set the chunk's tfdt and composition offset from the source."""
        dts = c.dts + self._shift
        ts = c.track.timescale
        set_chunk_timing(c.payload, round(dts * ts), round(c.cto * ts))
        return IngestChunk(c.track.name, bytes(c.payload), c.key, c.group_id,
                           round(dts * 1_000_000))

    async def chunks(self) -> AsyncIterator[IngestChunk]:
        """CMAF chunks in demux order until the input ends; demuxing runs
        on a thread, at most _QUEUE_SIZE chunks ahead of the consumer."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._thread = threading.Thread(target=self._run, args=(loop, queue),
                                        name='aiomoqt-ingest', daemon=True)
        self._thread.start()
        backlog, self._backlog = self._backlog, []
        try:
            for c in backlog:
                yield c
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self._stop.set()
            while not queue.empty():  # unblock a pending put
                queue.get_nowait()

    def close(self) -> None:
        """Stop the demux thread at its next packet, or release the input
        if chunks() never started one."""
        self._stop.set()
        if self._thread is None:
            self._container.close()

    def _put(self, loop, queue, item) -> bool:
        try:
            fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        except RuntimeError:  # loop closed
            return False
        while True:
            try:
                fut.result(timeout=0.5)
                return True
            except concurrent.futures.TimeoutError:
                if self._stop.is_set():
                    fut.cancel()
                    return False

    def _run(self, loop, queue) -> None:
        tracks = self._by_index()
        try:
            for packet in self._demux:
                if self._stop.is_set():
                    return
                track = tracks.get(packet.stream.index)
                if track is None or packet.dts is None:
                    continue
                for c in track.push(packet):
                    if not self._put(loop, queue, self._finish(c)):
                        return
            for track in self.tracks:
                for c in track.close():
                    if not self._put(loop, queue, self._finish(c)):
                        return
            self._put(loop, queue, None)
        except Exception as e:
            self._put(loop, queue, e)
        finally:
            self._container.close()


__all__ = ['Ingest', 'IngestChunk', 'IngestError', 'IngestTrack',
           'INSTALL_HINT', 'RENDITIONS', 'load_av']
