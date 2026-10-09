"""Media ingest via PyAV: HLS, DASH, files and anything else FFmpeg opens,
re-fragmented per track into CMAF chunks for CMSF publishing.

Each selected input stream gets its own mp4 muxer writing one sample per
moof+mdat (cmsf §3.3) into memory. Every chunk's tfdt and composition
offset are then set from the source packet, so all tracks share the
input's timeline exactly.

Groups: for DASH, one per source segment, numbered by it, so equally
numbered groups of the renditions are time-aligned (msf §4.2). Otherwise
one per video key frame (per audio frame), counted from the track's
first, which keeps renditions aligned only when their key frames are.

Needs the `media` extra (PyAV); load_av() raises ImportError naming it.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import functools
import heapq
import itertools
import os
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from fractions import Fraction
from typing import (
    AsyncIterator, Callable, Deque, Dict, Iterator, List, Optional, Tuple,
)

import certifi

from ..utils.logger import get_logger
from .cmaf import (
    init_codec_string, init_timescale, set_chunk_timing, strip_edit_lists,
)
from .dash import Presentation, SegmentFeed

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
_HTTP_TIMEOUT = 30.0
_HTTP_ATTEMPTS = 3


class IngestError(RuntimeError):
    pass


def load_av():
    """Import PyAV, or raise ImportError naming the extra to install."""
    try:
        import av
    except ImportError as e:
        raise ImportError(f"media ingest needs PyAV: {INSTALL_HINT}") from e
    return av


def tls_context() -> ssl.SSLContext:
    """Verifying client context: SSL_CERT_FILE when set, else certifi."""
    return ssl.create_default_context(
        cafile=os.environ.get('SSL_CERT_FILE') or certifi.where())


def http_get(url: str, ctx: ssl.SSLContext, headers: Optional[dict] = None):
    """urlopen() retried on timeouts, connection errors and 5xx; 4xx and
    certificate failures raise at once."""
    request = urllib.request.Request(url, headers=headers or {})
    for attempt in range(1, _HTTP_ATTEMPTS + 1):
        try:
            return urllib.request.urlopen(request, context=ctx,
                                          timeout=_HTTP_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == _HTTP_ATTEMPTS:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if (attempt == _HTTP_ATTEMPTS
                    or isinstance(getattr(e, 'reason', None), ssl.SSLError)):
                raise
        time.sleep(0.5 * attempt)


class _HttpReader:
    """One HTTP(S) response as a read-only file for FFmpeg, limited to the
    byte range FFmpeg asks for (also when the server ignores Range)."""

    def __init__(self, url: str, options: dict, ctx: ssl.SSLContext):
        start = int(options.get('offset') or 0)
        end = int(options.get('end_offset') or 0)
        headers = {}
        if start or end:
            headers['Range'] = f"bytes={start}-{end - 1 if end else ''}"
        self._resp = http_get(url, ctx, headers)
        ranged = self._resp.status == 206
        self._skip = 0 if ranged else start
        self._left = (end - start) if end else None

    def read(self, n: int) -> bytes:
        while self._skip:
            dropped = self._resp.read(min(self._skip, 1 << 16))
            if not dropped:
                return b''
            self._skip -= len(dropped)
        if self._left is not None:
            n = min(n, self._left)
        data = self._resp.read(n) if n else b''
        if self._left is not None:
            self._left -= len(data)
        return data

    def close(self) -> None:
        self._resp.close()


def _io_open(ctx: ssl.SSLContext):
    """PyAV io_open hook: every FFmpeg request (playlists, segments, live
    reloads) goes through _HttpReader, since the TLS library in PyAV's
    wheels does not find the system CA store on every platform."""
    def io_open(url, flags, options):
        if url.startswith('crypto'):
            raise OSError("encrypted HLS segments are not supported")
        if not url.startswith(('http://', 'https://')):
            raise OSError(f"unsupported URL: {url[:80]}")
        return _HttpReader(url, options, ctx)
    return io_open


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
    is muxed.

    `offset` (seconds) is added to every source time. With `segment_of`
    (media seconds -> source segment number) a group opens at the first
    sync sample of each new source segment, numbered by it; otherwise
    at every video key frame (every audio frame), counted."""

    def __init__(self, av, stream, name: str, kind: str, *,
                 bitrate: Optional[int] = None, offset: Fraction = Fraction(0),
                 segment_of: Optional[Callable[[Fraction], Optional[int]]] = None):
        self.stream = stream
        self.name = name
        self.kind = kind
        ctx = stream.codec_context
        # variant bandwidth spans the whole variant; audio has its own rate
        self.bitrate: Optional[int] = bitrate or (
            _rank(stream)[0] if kind == 'video' else ctx.bit_rate) or None
        self.offset = offset
        self._segment_of = segment_of
        self._segment = None
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
        self._pending: Deque[Tuple[bool, Optional[int], Fraction, Fraction]] = (
            collections.deque())
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
            cto = (pkt.pts - pkt.dts) * tb if pkt.pts is not None else Fraction(0)
            sync = self.kind != 'video' or pkt.is_keyframe
            start, group = sync, None
            if self._segment_of is not None:
                group = self._segment_of(pkt.dts * tb + cto) if sync else None
                start = group is not None and (self._segment is None
                                               or group > self._segment)
                if start:
                    self._segment = group
            dts = pkt.dts * tb + self.offset
            if self.first_dts is None:
                self.first_dts = dts
            self._pending.append((start, group if start else None, dts, cto))
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
                key, group, dts, cto = self._pending.popleft()
                if key:
                    self._group = group if group is not None else self._group + 1
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


def _video_names(renditions: List[Tuple[int, int]]) -> List[str]:
    """'video' for one rendition, else video-<height>p, disambiguated by
    bitrate; `renditions` holds (height, bitrate) pairs."""
    if len(renditions) == 1:
        return ['video']
    names: List[str] = []
    for height, bitrate in renditions:
        name = f"video-{height}p"
        if name in names:
            name = f"{name}-{bitrate // 1000}k"
        while name in names:
            name += '+'
        names.append(name)
    return names


def _fetch(url: str, byte_range: Optional[str] = None, *,
           ctx: ssl.SSLContext) -> bytes:
    """A resource, or its `first-last` byte range, over HTTP(S) or from a
    local path."""
    first = last = None
    if byte_range:
        a, _, b = byte_range.partition('-')
        first, last = int(a), (int(b) if b else None)
    if url.startswith(('http://', 'https://')):
        headers = {'Range': f"bytes={byte_range}"} if byte_range else None
        with http_get(url, ctx, headers) as resp:
            data = resp.read()
            if byte_range and resp.status != 206:
                data = data[first:None if last is None else last + 1]
            return data
    path = urllib.parse.urlparse(url).path if url.startswith('file:') else url
    with open(path, 'rb') as f:
        if first is None:
            return f.read()
        f.seek(first)
        return f.read(-1 if last is None else last - first + 1)


def _fetch_remote(url: str, byte_range: Optional[str] = None, *,
                  ctx: ssl.SSLContext) -> bytes:
    """_fetch for resources a remote MPD names: HTTP(S) only, so a
    manifest cannot make the ingest read local files."""
    if urllib.parse.urlsplit(url).scheme.lower() not in ('http', 'https'):
        raise OSError(f"unsupported DASH URL: {url[:80]}")
    return _fetch(url, byte_range, ctx=ctx)


def _is_dash(url: str, ctx: ssl.SSLContext) -> bool:
    """An .mpd URL, or one whose content is an MPD; .m3u8 never is."""
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith('.mpd'):
        return True
    if path.endswith('.m3u8'):
        return False
    try:
        if url.startswith(('http://', 'https://')):
            with http_get(url, ctx) as resp:
                head = resp.read(4096)
        else:
            with open(url, 'rb') as f:
                head = f.read(4096)
    except OSError:
        return False
    return b'<MPD' in head


_Source = Tuple[Iterator, Dict[int, 'IngestTrack']]


def _merge(sources: List[_Source]) -> Iterator[Tuple['IngestTrack', object]]:
    """(track, packet) from several demuxers, earliest decode time first,
    one packet of lookahead each. A source whose track map empties is no
    longer read."""
    heap: list = []
    order = itertools.count()

    def pull(i):
        packets, tracks = sources[i]
        for p in packets:
            if not tracks:
                return
            track = tracks.get(p.stream.index)
            if track is None or p.dts is None:
                continue
            key = p.dts * Fraction(p.time_base) + track.offset
            heapq.heappush(heap, (key, next(order), i, track, p))
            return

    for i in range(len(sources)):
        pull(i)
    while heap:
        _, _, i, track, packet = heapq.heappop(heap)
        yield track, packet
        pull(i)


class Ingest:
    """An opened input, its selected tracks, and the demux thread that
    feeds their CMAF chunks to the event loop. DASH (an .mpd URL, or an
    MPD by content) is read through aiomoqt.media.dash, one mp4 reader
    per representation; anything else through FFmpeg's own demuxers.

    The constructor blocks on network I/O (open, then demux until every
    track has its CMAF header): run it in an executor."""

    def __init__(self, url: str, rendition: str = 'best', audio: bool = True,
                 timeout: Optional[Tuple[float, float]] = (15.0, 30.0),
                 ssl_context: Optional[ssl.SSLContext] = None,
                 stop: Optional[threading.Event] = None):
        """`stop`, when set, abandons opening (between packets; DASH
        segment waits at once) and later stops demuxing."""
        if rendition not in RENDITIONS:
            raise ValueError(f"rendition must be one of {RENDITIONS}")
        self._av = load_av()
        self.url = url
        self.tracks: List[IngestTrack] = []
        self._stop = stop or threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._shift = Fraction(0)
        self._containers: list = []
        ctx = ssl_context or tls_context()
        try:
            if _is_dash(url, ctx):
                self._sources = self._open_dash(url, rendition, audio, ctx)
            else:
                self._sources = self._open_ffmpeg(url, rendition, audio,
                                                  timeout, ctx)
            self._demux = _merge(self._sources)
            self._backlog = self._prime()
        except BaseException:
            self._close_containers()
            raise

    def _open_ffmpeg(self, url, rendition, audio, timeout, ctx) -> List[_Source]:
        kwargs = {}
        if url.startswith(('http://', 'https://')):
            kwargs['io_open'] = _io_open(ctx)
        container = self._av.open(url, timeout=timeout, **kwargs)
        self._containers.append(container)
        videos, aud = _select(container, rendition, audio)
        if not videos and aud is None:
            raise IngestError("no h264/hevc/av1 video or aac/opus audio stream")
        names = _video_names([(s.codec_context.height, _rank(s)[0])
                              for s in videos])
        self.tracks = [IngestTrack(self._av, s, name, 'video')
                       for s, name in zip(videos, names)]
        if aud is not None:
            self.tracks.append(IngestTrack(self._av, aud, 'audio', 'audio'))
        keep = {t.stream.index for t in self.tracks}
        for s in container.streams:  # unselected renditions are never fetched
            if s.index not in keep:
                s.discard = self._av.stream.Discard.all
        return [(container.demux(*[t.stream for t in self.tracks]),
                 {t.stream.index: t for t in self.tracks})]

    def _open_dash(self, url, rendition, audio, ctx) -> List[_Source]:
        remote = url.startswith(('http://', 'https://'))
        pres = Presentation(url, functools.partial(
            _fetch_remote if remote else _fetch, ctx=ctx))
        videos, aud = pres.select(rendition, audio)
        reps = videos + ([aud] if aud is not None else [])
        names = _video_names([(r.height or 0, r.bandwidth) for r in videos])
        start = pres.start_time(reps)
        sources = []
        for rep, name in zip(reps, names + ['audio']):
            feed = SegmentFeed(pres, rep, start, self._stop)
            container = self._av.open(feed, format='mov',
                                      options={'ignore_editlist': '1'})
            self._containers.append(container)
            streams = (container.streams.video if rep.kind == 'video'
                       else container.streams.audio)
            if not streams:
                raise IngestError(f"DASH representation {rep.id}: no {rep.kind}")
            track = IngestTrack(self._av, streams[0], name, rep.kind,
                                bitrate=rep.bandwidth,
                                offset=-Fraction(rep.pto, rep.timescale),
                                segment_of=feed.segment_of)
            self.tracks.append(track)
            sources.append((container.demux(streams[0]),
                            {streams[0].index: track}))
        return sources

    def _prime(self) -> List[IngestChunk]:
        """Demux until every track has its CMAF header, returning the
        chunks read on the way. A track still without one after
        _PRIME_LIMIT of media is dropped. Fixes the timeline shift that
        keeps every decode time non-negative."""
        raw: List[_RawChunk] = []
        first = None
        for track, packet in self._demux:
            if self._stop.is_set():
                raise IngestError(f"opening {self.url} stopped")
            t = packet.dts * Fraction(packet.time_base) + track.offset
            first = t if first is None else first
            raw += track.push(packet)
            if all(x.init is not None for x in self.tracks):
                break
            if t - first > _PRIME_LIMIT:
                break
        for track in [x for x in self.tracks if x.init is None]:
            logger.warning("ingest: no %s data from %s; track dropped",
                           track.name, self.url)
            self.tracks.remove(track)
            track.stream.discard = self._av.stream.Discard.all
            for _, tracks in self._sources:
                if tracks.get(track.stream.index) is track:
                    del tracks[track.stream.index]
        if not self.tracks:
            raise IngestError(f"no media read from {self.url}")
        starts = [x.first_dts for x in self.tracks if x.first_dts is not None]
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
            self._close_containers()

    def _close_containers(self) -> None:
        for container in self._containers:
            container.close()

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
        try:
            for track, packet in self._demux:
                if self._stop.is_set():
                    return
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
            self._close_containers()


__all__ = ['Ingest', 'IngestChunk', 'IngestError', 'IngestTrack',
           'INSTALL_HINT', 'RENDITIONS', 'http_get', 'load_av', 'tls_context']
