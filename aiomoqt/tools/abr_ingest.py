"""ABR ingester: a LOC broadcast in; the same broadcast plus an ABR ladder
out, under one namespace with one catalog.

The source publisher dials the ingester, which publishes to the relays it
dials (bump in the wire). pub_media announces with --pub-ns:

  python -m aiomoqt.tools.abr_ingest -Q -p 4443 -N live/x moqt://relay:4433
  python -m aiomoqt.tools.pub_media moqt://localhost:4443 -k --pub-ns \\
      -N live/x --mp4 clip.mp4 --loop -t 0

--source URL subscribes the source from a relay instead, waiting up to
the setup timeout for it to be published; publishing back to that same
relay needs another namespace (--out-ns).

--input URL reads the source with cymedia: a file, or a live SRT, RTMP,
RTSP or HLS input, as tracks video and audio. --input-option passes
libavformat options; the ingester ends when the input does:

  python -m aiomoqt.tools.abr_ingest -N live/x moqt://relay:4433 \\
      --input 'srt://0.0.0.0:9000?mode=listener'

The video track (-T; default the catalog's first LOC video track) must be
H.264. Every LOC track passes through unchanged. The rungs, H.264 or AV1
(--codec), are encoded from the video by cymedia (uv pip install 'aiomoqt[abr]'), keep its group and
object IDs, open each group on a key frame and join its catalog altGroup.

The ladder encodes at the catalog's framerate, waiting up to 5 s for a
source catalog update that gives one before assuming 30 fps. Framerates
and passthrough bitrates the source adds later are republished.

Each -i interval reports, per output track, the delay from a source
frame's arrival to its hand-off to the output track; rungs add the
encoder's share. --no-ladder measures the hand-off alone.

--packaging cmaf publishes every output track as CMSF: one CMAF chunk
per object, led by a prft, with the CMAF header in the catalog.

--head-trace logs, per relay, each video group's first object: when it
was written, its stream opened and its bytes went into packets, with the
connection's cwin, bytes in flight, pacing rate, RTT and losses.
"""
import argparse
import asyncio
import contextlib
import importlib
import signal
import sys
import time
from typing import Dict, List, Optional, Set, Tuple, Union

from aiomoqt.client import MOQTClient
from aiomoqt.fanout import FanoutPublisher
from aiomoqt.media.abr import (
    CODECS, DEFAULT_RUNGS, AbrLadder, DelayMeter, Rung, SourceInput, frame_rate,
    ladder_catalog, quantiles_ms, rungs_below,
)
from aiomoqt.media.broadcast import MediaSubscriber
from aiomoqt.media.catalog import (
    PACKAGING_CMAF, PACKAGING_LOC, Catalog, CatalogTrack, InitData,
)
from aiomoqt.media.cmaf import (
    CmafChunker, aac_track, asc_info, init_codec_string, opus_track, prft_box,
    video_track,
)
from aiomoqt.media.loc import (
    LOC_PROP_AUDIO_CONFIG, LOC_PROP_VIDEO_CONFIG, LocFrame, LocTrackPublisher,
)
from aiomoqt.media.mpegts import opus_samples
from aiomoqt.server import MOQTServer
from aiomoqt.types import (
    MOQTMessageType, MOQTRequestError, RequestErrorCode, SubscribeErrorCode,
)
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.certs import find_cert, find_key
from aiomoqt.utils.logger import get_logger
from aiomoqt.utils.url import parse_relay_url

logger = get_logger(__name__)

CERT = find_cert()
KEY = find_key(CERT)
_QUEUE = 512          # frames per output track awaiting hand-off
_FALLBACK_FPS = 30.0
_FPS_WAIT_S = 5.0     # from the first key frame
_LADDER_READY_S = 10.0
_SHUTDOWN_DRAIN_S = 2.0
EXIT_UNAVAILABLE = 69


def _option(value: str) -> Tuple[str, str]:
    key, sep, val = value.partition('=')
    if not key or not sep:
        raise argparse.ArgumentTypeError(f"{value!r}: KEY=VALUE")
    return key, val


def _rung(value: str) -> Rung:
    try:
        return Rung.parse(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def parse_args(argv=None):
    parser = _cli.make_parser(
        'ABR ingester: a LOC broadcast plus an ABR ladder, one catalog',
        epilog=__doc__)
    _cli.add_endpoints(parser)
    _cli.add_listener(parser)
    parser.add_argument('--source', metavar='URL', type=_cli.endpoint_url,
                        help='Subscribe the source from this relay instead '
                             'of listening for its publisher')
    parser.add_argument('--input', metavar='URL',
                        help='Read the source with cymedia instead: a file, '
                             'or a live SRT, RTMP, RTSP or HLS input')
    parser.add_argument('--input-option', metavar='KEY=VALUE', type=_option,
                        action='append',
                        help='A libavformat option for --input, e.g. listen=1 '
                             'for an RTMP server; repeat for each')
    _cli.add_identity(parser, namespace='live/abr')
    parser.add_argument('--out-ns', metavar='NS',
                        help='Namespace to publish under (default: -N)')
    parser.add_argument('--profile', choices=('nvenc', 'videotoolbox', 'cpu'),
                        default='nvenc',
                        help='cymedia encoder profile (default: nvenc)')
    parser.add_argument('--codec', choices=CODECS, default='h264',
                        help='Rung codec (default: h264)')
    default_rungs = ', '.join(f'{r.width}x{r.height}:{r.bit_rate / 1e6:g}M'
                              for r in DEFAULT_RUNGS)
    parser.add_argument('--rung', metavar='WxH:BPS', type=_rung,
                        action='append',
                        help='A ladder rung; repeat for each (default: '
                             f'those of {default_rungs} below the source '
                             'height)')
    parser.add_argument('--no-ladder', action='store_true',
                        help='Pass the source through without a ladder')
    parser.add_argument('--packaging', choices=(PACKAGING_LOC, PACKAGING_CMAF),
                        default=PACKAGING_LOC,
                        help='Output packaging (default: loc); cmaf = CMSF, '
                             'one CMAF chunk per object')
    parser.add_argument('--head-trace', action='store_true',
                        help="Log each video group's first object per relay: "
                             "when it was written, its stream opened and its "
                             "bytes went into packets, with the connection's "
                             "cwin, bytes in flight, pacing rate, RTT and "
                             "losses (INFO on aiomoqt.data)")
    parser.add_argument('--cert', default=CERT,
                        help='TLS certificate for the listener')
    parser.add_argument('--key', default=KEY,
                        help='TLS key for the listener')
    _cli.add_run(parser)
    _cli.add_interval(parser)
    _cli.add_session(parser)
    _cli.add_keepalive(parser, default=10)
    _cli.add_logging(parser)
    _cli.add_help(parser)
    args = parser.parse_args(argv)
    if args.profile == 'videotoolbox' and args.codec != 'h264':
        parser.error('--profile videotoolbox encodes H.264 only')
    if args.source and (args.quic or args.wt):
        parser.error('--source dials the source and -Q/-W listen for it: '
                     'pick one')
    if args.input and (args.source or args.quic or args.wt):
        parser.error('--input reads the source itself: no --source or -Q/-W')
    if args.input_option and not args.input:
        parser.error('--input-option needs --input')
    if args.no_ladder and args.rung:
        parser.error('--no-ladder takes no --rung')
    if args.rung and len({r.name for r in args.rung}) < len(args.rung):
        parser.error('two rungs of one height would share a track name')
    if not (args.source or args.input) and not (args.cert and args.key):
        parser.error('listening needs --cert and --key, or certs/')
    return args


def _ns(parts) -> str:
    return '/'.join(p.decode() if isinstance(p, bytes) else p for p in parts)


async def _dial(args, url: str, stack: contextlib.AsyncExitStack):
    relay = parse_relay_url(url)
    client = MOQTClient(
        relay.host, relay.port, path=relay.path, use_quic=relay.use_quic,
        verify_tls=not args.insecure, supported_drafts=args.draft,
        debug=args.debug, keylog_filename=args.keylogfile,
        **_cli.transport_kwargs(args))
    session = await stack.enter_async_context(client.connect())
    await session.client_session_init()
    return session


async def _accept(args, stack: contextlib.AsyncExitStack):
    """Listen until a publisher announces -N; that is the source session."""
    quic, wt = _cli.resolve_listener(args)
    found = asyncio.get_running_loop().create_future()

    async def on_namespace(session, msg):
        session.publish_namespace_ok(msg)
        ns = _ns(msg.namespace)
        if ns == args.namespace and not found.done():
            found.set_result(session)
        elif ns != args.namespace:
            logger.info(f"abr_ingest: ignoring announced '{ns}'")

    server = MOQTServer(
        host=args.bind, port=args.port, certificate=args.cert,
        private_key=args.key, path=args.path, use_quic=quic and not wt,
        supported_drafts=args.draft, debug=args.debug,
        **_cli.transport_kwargs(args))
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE, on_namespace)
    handle = await (server.serve_dual() if quic and wt else server.serve())
    stack.callback(handle.close)
    stack.push_async_callback(server.close_sessions)
    print(f"  listening on {'QUIC+WT' if quic and wt else 'QUIC' if quic else 'WT'}"
          f" port {args.port} for '{args.namespace}'")
    return await found


_NO_SOURCE = (RequestErrorCode.DOES_NOT_EXIST, SubscribeErrorCode.TRACK_DOES_NOT_EXIST)
_SOURCE_POLL_S = 0.2


async def _subscribe_source(args, session, on_frame, on_catalog):
    """The source's MediaSubscriber and catalog. With --source, waits up
    to the setup timeout for a source not yet published on the relay."""
    timeout = _cli.setup_timeout(args)
    deadline = time.monotonic() + timeout
    sub = MediaSubscriber(
        session, args.namespace, on_frame=on_frame, on_catalog=on_catalog,
        track_filter=lambda t: t.packaging == PACKAGING_LOC)
    while True:
        try:
            return sub, await sub.start(timeout=timeout)
        except MOQTRequestError as e:
            if (not args.source or e.error_code not in _NO_SOURCE
                    or time.monotonic() > deadline):
                raise
        await asyncio.sleep(_SOURCE_POLL_S)


def _video_entry(catalog: Catalog, name: Optional[str]) -> CatalogTrack:
    for t in catalog.tracks:
        if t.packaging != PACKAGING_LOC or t.name is None:
            continue
        if (t.name == name if name else t.role == 'video'):
            return t
    raise SystemExit(f"  error: no LOC video track "
                     f"{repr(name) if name else ''} in the source catalog")


_VIDEO_TIMESCALE = 90000
_CONFIG_PROPS = (LOC_PROP_VIDEO_CONFIG, LOC_PROP_AUDIO_CONFIG)


class _Cmaf:
    """One output track's LOC frames as CMSF objects: prft + one-sample
    moof+mdat. tfdt is the LOC timestamp in track ticks, kept rising;
    the prft carries the capture time. Audio tfdt steps by each frame's
    samples and rejoins the timestamp when they differ by over half a frame;
    an audio frame at or behind the last tfdt is dropped."""

    def __init__(self, track, loc_timescale: Optional[int],
                 fps: Optional[Tuple[int, int]] = None,
                 samples=None):
        self.chunker = CmafChunker(track)
        self.init = self.chunker.init_segment()
        self.codec = init_codec_string(self.init)
        self.video = track.width is not None
        self._units = loc_timescale or 1_000_000
        ts = self.chunker.timescale
        self._frame = (None if fps is None
                       else max(1, round(ts * fps[1] / fps[0])))
        self._samples = samples  # audio: payload -> duration in ticks
        self._last: Optional[int] = None
        self._next = 0  # audio: last tfdt plus its sample count
        self._dur = self._frame or ts // 30

    def wrap(self, frame: LocFrame) -> Optional[bytes]:
        """The CMSF object for `frame`; None when it is dropped."""
        ts = frame.timestamp
        dt = None if ts is None else ts * self.chunker.timescale // self._units
        if self._samples is not None:
            dur = self._samples(frame.payload)
            if self._last is not None:
                if dt is None or abs(dt - self._next) <= dur // 2:
                    dt = self._next
                elif dt <= self._last:
                    return None
            dt = dt or 0
            self._next = dt + dur
        else:
            if self._last is not None:
                if dt is not None and dt > self._last and self._frame is None:
                    self._dur = dt - self._last
                if dt is None or dt <= self._last:
                    dt = self._last + self._dur
            dt = dt or 0
            dur = self._frame or self._dur
        self._last = dt
        chunk = self.chunker.chunk(frame.payload, dur,
                                   frame.key_frame or not self.video,
                                   decode_time=dt)
        if ts is None:
            return chunk
        return prft_box(ts * 1_000_000 // self._units, dt) + chunk


def _cmaf_for(entry: CatalogTrack, config: Optional[bytes],
              codec: Optional[str], loc_timescale: Optional[int],
              fps: Optional[Tuple[int, int]]) -> Optional[_Cmaf]:
    """The CMSF packager for an output track; None if its codec or
    config cannot make a CMAF header."""
    codec = codec or ''
    if entry.role == 'audio':
        channels = (int(entry.channelConfig)
                    if (entry.channelConfig or '').isdigit() else None)
        if codec.startswith('mp4a.40') and config:
            per_frame = asc_info(config)[2]
            return _Cmaf(aac_track(config, entry.samplerate, channels),
                         loc_timescale, samples=lambda _p: per_frame)
        if codec == 'opus':
            return _Cmaf(opus_track(channels or 2), loc_timescale,
                         samples=opus_samples)
        return None
    fourcc = {'avc1': 'avc1', 'avc3': 'avc1', 'hvc1': 'hvc1', 'hev1': 'hvc1',
              'av01': 'av01'}.get(codec[:4])
    if fourcc is None or not config:
        return None
    if entry.framerate and fps is None:
        fps = frame_rate(entry.framerate)
    return _Cmaf(video_track(fourcc, config, entry.width or 0,
                             entry.height or 0, _VIDEO_TIMESCALE),
                 loc_timescale, fps=fps)


class _Output:
    """One output track: a bounded hand-off queue and its meter. Frames
    are dropped while no peer takes them (`idle`), so a late subscriber
    gets live frames rather than a backlog. After a drop it skips to the
    next group start, so object IDs stay the source's. `cmaf` packages
    each frame as a CMSF object."""

    def __init__(self, track: LocTrackPublisher,
                 cmaf: Optional[_Cmaf] = None):
        self.track = track
        self.cmaf = cmaf
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE)
        self.meter = DelayMeter()
        self.frames = 0
        self.idle = 0
        self.dropped = 0
        self._skip: Optional[str] = 'idle'  # until a group start

    def put(self, frame: LocFrame, group_id: int, t0: Optional[int]):
        if self.track.demand[0] == 0:
            self._skip = 'idle'
        elif frame.key_frame:
            self._skip = None
        if self._skip is None:
            try:
                self.queue.put_nowait((frame, group_id, t0))
                return
            except asyncio.QueueFull:
                self._skip = 'full'
        if self._skip == 'idle':
            self.idle += 1
        else:
            self.dropped += 1

    async def feed(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                break
            frame, group_id, t0 = item
            payload = (frame.payload if self.cmaf is None
                       else self.cmaf.wrap(frame))
            if payload is None:
                self.dropped += 1
                continue
            await self.track.send_frame(
                payload, key_frame=frame.key_frame,
                timestamp=frame.timestamp, extensions=frame.extensions,
                group_id=group_id)
            if t0 is not None:
                self.meter.add(time.monotonic_ns() - t0)
            self.frames += 1
        await self.track.finish()


class _Ingest:
    def __init__(self, args, sub: Union[MediaSubscriber, SourceInput],
                 catalog: Catalog):
        self.args = args
        self.sub = sub
        self.catalog = catalog
        self.out_ns = args.out_ns or args.namespace
        self.video = _video_entry(catalog, args.trackname)
        self.video_name: str = self.video.name or ''
        self.rungs: List[Rung] = (
            [] if args.no_ladder
            else args.rung or rungs_below(self.video.height or 0))
        if not args.no_ladder and not self.rungs:
            raise SystemExit(f"  error: no default rung below the "
                             f"source's {self.video.height}p; give --rung")
        self.fps: Tuple[int, int] = frame_rate(_FALLBACK_FPS)
        self._first_key: Optional[int] = None
        self.ladder: Optional[AbrLadder] = None
        self.outputs: Dict[str, _Output] = {}
        self.pub: Optional[FanoutPublisher] = None
        self.before_start = 0
        self.left_out: Set[str] = set()
        self.keyed = asyncio.Event()
        self._feeders: List[asyncio.Task] = []
        self._republishing: Set[asyncio.Task] = set()

    def on_frame(self, name: str, frame: LocFrame, group_id: int,
                 object_id: int) -> None:
        now = time.monotonic_ns()
        if (name == self.video_name and frame.key_frame
                and not self.keyed.is_set() and self._rate_known(now)):
            if self.rungs:
                self._start_ladder()
            self.keyed.set()
        if name == self.video_name and self.ladder is not None:
            self.ladder.push(frame, group_id, object_id)
        self._hand(name, frame, group_id, now)

    def _rate_known(self, now: int) -> bool:
        """Whether the ladder can start: the catalog gives a framerate, or
        _FPS_WAIT_S has passed since the first key frame (then
        _FALLBACK_FPS)."""
        if not self.rungs:
            return True
        if self.video.framerate:
            self.fps = frame_rate(self.video.framerate)
            return True
        if self._first_key is None:
            self._first_key = now
            logger.info(f"abr_ingest: waiting up to {_FPS_WAIT_S:g} s for "
                        f"a framerate for '{self.video_name}'")
        if now - self._first_key < _FPS_WAIT_S * 1e9:
            return False
        logger.warning(f"abr_ingest: catalog gives no framerate for "
                       f"'{self.video_name}'; assuming {_FALLBACK_FPS:g}")
        return True

    def on_catalog(self, catalog: Catalog) -> None:
        """A source catalog update. Once publishing, framerates (rungs:
        the video's) and passthrough bitrates it adds or changes are
        republished."""
        self.catalog = catalog
        src = {t.name: t for t in catalog.tracks
               if t.packaging == PACKAGING_LOC}
        self.video = src.get(self.video_name, self.video)
        if self.pub is None:
            return
        out = Catalog.from_json(self.pub.catalog_track.catalog.to_json())
        rungs = {r.name for r in self.rungs}
        changed = []
        for t in out.tracks:
            rung = t.name in rungs
            s = self.video if rung else src.get(t.name)
            if s is None:
                continue
            for field in ('framerate',) if rung else ('framerate', 'bitrate'):
                value = getattr(s, field)
                if value is not None and getattr(t, field) != value:
                    setattr(t, field, value)
                    changed.append(f"{t.name} {field} {value:g}")
        if not changed:
            return
        out.generatedAt = int(time.time() * 1000)
        logger.info(f"abr_ingest: catalog republished: {', '.join(changed)}")
        task = asyncio.ensure_future(
            self.pub.catalog_track.publish_catalog(out))
        self._republishing.add(task)
        task.add_done_callback(self._republishing.discard)

    def _start_ladder(self) -> None:
        cymedia = importlib.import_module("cymedia")
        cymedia.set_ffmpeg_log_level(32 if self.args.debug else 16)
        src = self.sub.tracks[self.video_name]
        ladder = self.ladder = AbrLadder(
            self.rungs, config=src.config, fps=self.fps,
            profile=self.args.profile, codec=self.args.codec,
            timescale=src.timescale, on_frame=self._on_rung)
        ladder.start()

    def _on_rung(self, i: int, frame: LocFrame, group_id: int,
                 object_id: int, pushed_ns: Optional[int]) -> None:
        out = self.outputs.get(self.rungs[i].name)
        if (out is not None and out.cmaf is None
                and self.ladder is not None):
            out.track.config = self.ladder.configs[i]
        self._hand(self.rungs[i].name, frame, group_id, pushed_ns)

    def _hand(self, name: str, frame: LocFrame, group_id: int,
              t0: Optional[int]) -> None:
        out = self.outputs.get(name)
        if out is None:
            if name not in self.left_out:
                self.before_start += 1
            return
        out.put(frame, group_id, t0)

    async def start_outputs(self, sessions) -> None:
        """Publish once the source has a key frame and every rung its
        decoder config, so the catalog is complete from the first."""
        await asyncio.wait_for(self.keyed.wait(), _cli.setup_timeout(self.args))
        configs: List[bytes] = []
        if self.ladder is not None:
            await asyncio.wait_for(self.ladder.ready.wait(), _LADDER_READY_S)
            configs = self.ladder.rung_configs()
            catalog = ladder_catalog(self.catalog, self.video_name,
                                     self.rungs, configs)
        else:
            catalog = Catalog.from_json(self.catalog.to_json())
        catalog.tracks = [t for t in catalog.tracks
                          if t.packaging == PACKAGING_LOC
                          and (t.name in self.sub.tracks
                               or t.name in {r.name for r in self.rungs})]
        if self.out_ns != self.args.namespace:
            for t in catalog.tracks:
                t.namespace = None
        timescale = self.sub.tracks[self.video_name].timescale
        as_cmaf = self.args.packaging == PACKAGING_CMAF
        cmaf = (self._cmaf_catalog(catalog, configs, timescale)
                if as_cmaf else {})
        self.pub = FanoutPublisher(sessions, self.out_ns, catalog)
        video_props = {k: v for k, v in
                       self.sub.properties(self.video_name).items()
                       if k != LOC_PROP_VIDEO_CONFIG}
        tracks = []
        for entry in catalog.tracks:
            name = entry.name or ''
            src = self.sub.tracks.get(name)
            if src is not None:
                track = LocTrackPublisher(
                    self.pub.session, self.out_ns, name,
                    config=None if as_cmaf else src.config,
                    timescale=src.timescale,
                    media_kind='audio' if entry.role == 'audio' else 'video',
                    codec_string=None if as_cmaf else src.codec_string)
                props = self.sub.properties(name)
            else:
                i = next(k for k, r in enumerate(self.rungs) if r.name == name)
                track = LocTrackPublisher(
                    self.pub.session, self.out_ns, name,
                    config=None if as_cmaf else configs[i], timescale=timescale,
                    media_kind='video')
                props = video_props
            if as_cmaf:
                props = {k: v for k, v in props.items()
                         if k not in _CONFIG_PROPS}
            track.track_properties = dict(props) or None
            tracks.append(track)
        for track in tracks:
            track.trace_heads = (self.args.head_trace
                                 and track.media_kind == 'video')
            self.pub.add_track(track)
        await self.pub.start(forward=0)
        for track in tracks:
            out = self.outputs[track.trackname] = _Output(
                track, cmaf.get(track.trackname))
            self._feeders.append(asyncio.ensure_future(out.feed()))
        print(f"  publishing '{self.out_ns}': "
              f"{', '.join(t.trackname for t in tracks)}")

    def _cmaf_catalog(self, catalog: Catalog, configs: List[bytes],
                      timescale: Optional[int]) -> Dict[str, _Cmaf]:
        """Turn `catalog` into a CMSF one in place: each track packaged
        cmaf, its CMAF header in initDataList, codec from that header.
        A track no CMAF header can carry is left out."""
        rungs = {r.name: i for i, r in enumerate(self.rungs)}
        out: Dict[str, _Cmaf] = {}
        kept, init = [], []
        for entry in catalog.tracks:
            name = entry.name or ''
            if name in rungs:
                pkg = _cmaf_for(entry, configs[rungs[name]], entry.codec,
                                timescale, self.fps)
            else:
                src = self.sub.tracks[name]
                pkg = _cmaf_for(entry, src.config,
                                entry.codec or src.codec_string,
                                src.timescale, None)
            if pkg is None or pkg.codec is None:
                logger.warning(f"abr_ingest: '{name}' ({entry.codec}) left "
                               f"out: no CMAF header for it")
                self.left_out.add(name)
                continue
            ref = f"cmaf{len(init)}"
            entry.packaging, entry.initRef, entry.codec = (
                PACKAGING_CMAF, ref, pkg.codec)
            init.append(InitData.from_bytes(ref, pkg.init))
            kept.append(entry)
            out[name] = pkg
        catalog.tracks, catalog.initDataList = kept, init or None
        return out

    async def stop(self, sessions) -> None:
        if self.ladder is not None:
            with contextlib.suppress(Exception):
                await self.ladder.flush()
        for out in self.outputs.values():
            with contextlib.suppress(asyncio.QueueFull):
                out.queue.put_nowait(None)
        if self._feeders:
            await asyncio.wait(self._feeders, timeout=_SHUTDOWN_DRAIN_S)
        if self.pub is None:
            return
        for s in sessions:
            s.drain()
        if self._republishing:
            await asyncio.wait(self._republishing, timeout=_SHUTDOWN_DRAIN_S)
        await self.pub.catalog_track.finish()
        deadline = time.monotonic() + _SHUTDOWN_DRAIN_S
        tracks = [*self.pub.tracks.values(), *self.pub.catalog_track.members]
        while time.monotonic() < deadline and not all(
                sub.done or (sub.request_id is None
                             and sub.subscribe_request_id is None)
                for track in tracks for sub in track.subscriptions):
            await asyncio.sleep(0.02)
        await asyncio.gather(*(s.wait_delivered() for s in sessions),
                             return_exceptions=True)

    def close(self) -> None:
        if self.ladder is not None:
            self.ladder.close()

    def report(self, final: bool = False) -> None:
        engine = {}
        if self.ladder is not None:
            engine = {r.name: self.ladder.engine_delays(i)
                      for i, r in enumerate(self.rungs)}
        lines = []
        for name, out in self.outputs.items():
            q = quantiles_ms(out.meter.take())
            line = (f"  {name:12s} {out.frames:7d} frames {out.idle:6d} idle "
                    f"{out.dropped:4d} dropped")
            if q is not None:
                line += (f"  arrival to hand-off p50 {q[0]:6.2f} "
                         f"p99 {q[1]:6.2f} max {q[2]:7.2f} ms")
            e = quantiles_ms(engine.get(name, ()))
            if e is not None:
                line += f"  (encoder p50 {e[0]:.2f} p99 {e[1]:.2f})"
            lines.append(line)
        if final and self.ladder is not None:
            st = self.ladder.stats()
            lines.append(
                f"  ladder: {self.ladder.frames_in} in, "
                f"{self.ladder.input_dropped} dropped at input, "
                f"{self.ladder.late_dropped} late, groups "
                f"abandoned per rung "
                f"{[b['groups_abandoned'] for b in st['branches']]}")
        if final and self.before_start:
            lines.append(f"  {self.before_start} frames before publishing "
                         f"started")
        if lines:
            print('\n'.join(lines), flush=True)


async def run(args) -> int:
    if args.head_trace:
        args.log = ['data=info', *(args.log or ())]
    _cli.apply_logging(args)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    async with contextlib.AsyncExitStack() as stack:
        ingest: Optional[_Ingest] = None

        def on_frame(name, frame, group_id, object_id):
            if ingest is not None:
                ingest.on_frame(name, frame, group_id, object_id)

        def on_catalog(catalog):
            if ingest is not None:
                ingest.on_catalog(catalog)

        if args.input:
            print(f"  input: {args.input}")
            try:
                source = await SourceInput.open(
                    args.input, dict(args.input_option or ()),
                    timeout=_cli.setup_timeout(args))
            except (OSError, ValueError, ImportError, asyncio.TimeoutError) as e:
                print(f"  error: {args.input}: {str(e) or type(e).__name__}")
                return EXIT_UNAVAILABLE
            stack.callback(source.close)
            sub, catalog = source, source.catalog
        else:
            if args.source:
                print(f"  source: {args.source} '{args.namespace}'")
                source = await _dial(args, args.source, stack)
            else:
                source = await _accept(args, stack)
            sub, catalog = await _subscribe_source(args, source, on_frame,
                                                   on_catalog)
        ingest = _Ingest(args, sub, catalog)
        if args.input:
            source.start(on_frame)
        print(f"  source tracks: {', '.join(sub.tracks)}; video "
              f"'{ingest.video.name}' {ingest.video.width}x"
              f"{ingest.video.height}")
        if ingest.rungs:
            print(f"  ladder ({args.profile}, {args.codec}): "
                  f"{', '.join(r.name for r in ingest.rungs)}")
        sessions = []
        for url in args.url:
            try:
                sessions.append(await _dial(args, url, stack))
            except Exception as e:
                print(f"  error: {url}: {str(e) or type(e).__name__}")
                return EXIT_UNAVAILABLE
        rc = 0
        try:
            await ingest.start_outputs(sessions)
            closed = [asyncio.ensure_future(s.async_closed())
                      for s in (source, *sessions)]
            waiters = [asyncio.ensure_future(stop.wait()), *closed]
            deadline = time.monotonic() + args.duration if args.duration else None
            while not any(w.done() for w in waiters):
                timeout = args.interval
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - time.monotonic()))
                await asyncio.wait(waiters, timeout=timeout,
                                   return_when=asyncio.FIRST_COMPLETED)
                ingest.report()
                if deadline is not None and time.monotonic() >= deadline:
                    break
            if args.input and closed[0].done():
                print("  the input ended; stopping")
            elif any(c.done() for c in closed):
                print("  a session closed; stopping")
            for w in waiters:
                w.cancel()
        except asyncio.TimeoutError:
            print("  error: the source sent no key frame, or the ladder no "
                  "decoder config, in time")
            rc = 1
        finally:
            await ingest.stop(sessions)
            ingest.report(final=True)
            ingest.close()
    return rc


def main():
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
