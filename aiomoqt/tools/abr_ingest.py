"""ABR ingester: a LOC broadcast in; the same broadcast plus an ABR ladder
out, under one namespace with one catalog.

The source publisher dials the ingester, which publishes to the relays it
dials (bump in the wire). pub_media announces with --pub-ns:

  python -m aiomoqt.tools.abr_ingest -Q -p 4443 -N live/x moqt://relay:4433
  python -m aiomoqt.tools.pub_media moqt://localhost:4443 -k --pub-ns \\
      -N live/x --mp4 clip.mp4 --loop -t 0

--source URL subscribes the source from a relay instead; publishing back
to that same relay needs another namespace (--out-ns).

The video track (-T; default the catalog's first LOC video track) must be
H.264. Every LOC track passes through unchanged. The rungs are encoded
from the video by cymedia (uv pip install
git+https://github.com/gmarzot/cymedia), keep its group and object IDs, open each group on a key frame and join its catalog altGroup.

Each -i interval reports, per output track, the delay from a source
frame's arrival to its hand-off to the output track; rungs add the
encoder's share. --no-ladder measures the hand-off alone.
"""
import argparse
import asyncio
import contextlib
import importlib
import signal
import sys
import time
from typing import Dict, List, Optional

from aiomoqt.client import MOQTClient
from aiomoqt.fanout import FanoutPublisher
from aiomoqt.media.abr import (
    DEFAULT_RUNGS, AbrLadder, DelayMeter, Rung, frame_rate, ladder_catalog,
    quantiles_ms, rungs_below,
)
from aiomoqt.media.broadcast import MediaSubscriber
from aiomoqt.media.catalog import PACKAGING_LOC, Catalog, CatalogTrack
from aiomoqt.media.loc import LOC_PROP_VIDEO_CONFIG, LocFrame, LocTrackPublisher
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
_LADDER_READY_S = 10.0
_SHUTDOWN_DRAIN_S = 2.0
EXIT_UNAVAILABLE = 69


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
    _cli.add_identity(parser, namespace='live/abr')
    parser.add_argument('--out-ns', metavar='NS',
                        help='Namespace to publish under (default: -N)')
    parser.add_argument('--profile', choices=('nvenc', 'cpu'),
                        default='nvenc',
                        help='cymedia encoder profile (default: nvenc)')
    default_rungs = ', '.join(f'{r.width}x{r.height}:{r.bit_rate / 1e6:g}M'
                              for r in DEFAULT_RUNGS)
    parser.add_argument('--rung', metavar='WxH:BPS', type=_rung,
                        action='append',
                        help='A ladder rung; repeat for each (default: '
                             f'those of {default_rungs} below the source '
                             'height)')
    parser.add_argument('--no-ladder', action='store_true',
                        help='Pass the source through without a ladder')
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
    if args.source and (args.quic or args.wt):
        parser.error('--source dials the source and -Q/-W listen for it: '
                     'pick one')
    if args.no_ladder and args.rung:
        parser.error('--no-ladder takes no --rung')
    if args.rung and len({r.name for r in args.rung}) < len(args.rung):
        parser.error('two rungs of one height would share a track name')
    if not args.source and not (args.cert and args.key):
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


_NOT_YET = (RequestErrorCode.DOES_NOT_EXIST, SubscribeErrorCode.TRACK_DOES_NOT_EXIST)


async def _subscribe_source(args, session, on_frame):
    """The source's MediaSubscriber and catalog. A publisher may announce
    before it serves SUBSCRIBE, so does-not-exist retries until the setup
    timeout."""
    deadline = time.monotonic() + _cli.setup_timeout(args)
    while True:
        sub = MediaSubscriber(
            session, args.namespace, on_frame=on_frame,
            track_filter=lambda t: t.packaging == PACKAGING_LOC)
        try:
            return sub, await sub.start(timeout=_cli.setup_timeout(args))
        except MOQTRequestError as e:
            if e.error_code not in _NOT_YET or time.monotonic() > deadline:
                raise
        await asyncio.sleep(0.2)


def _video_entry(catalog: Catalog, name: Optional[str]) -> CatalogTrack:
    for t in catalog.tracks:
        if t.packaging != PACKAGING_LOC or t.name is None:
            continue
        if (t.name == name if name else t.role == 'video'):
            return t
    raise SystemExit(f"  error: no LOC video track "
                     f"{repr(name) if name else ''} in the source catalog")


class _Output:
    """One output track: a bounded hand-off queue and its meter. Frames
    are dropped while no peer takes them (`idle`), so a late subscriber
    gets live frames rather than a backlog. After a drop it skips to the
    next group start, so object IDs stay the source's."""

    def __init__(self, track: LocTrackPublisher):
        self.track = track
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
            await self.track.send_frame(
                frame.payload, key_frame=frame.key_frame,
                timestamp=frame.timestamp, extensions=frame.extensions,
                group_id=group_id)
            if t0 is not None:
                self.meter.add(time.monotonic_ns() - t0)
            self.frames += 1
        await self.track.finish()


class _Ingest:
    def __init__(self, args, sub: MediaSubscriber, catalog: Catalog):
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
        if self.video.framerate:
            self.fps = frame_rate(self.video.framerate)
        else:
            logger.warning(f"abr_ingest: catalog gives no framerate for "
                           f"'{self.video_name}'; assuming {_FALLBACK_FPS:g}")
            self.fps = frame_rate(_FALLBACK_FPS)
        self.ladder: Optional[AbrLadder] = None
        self.outputs: Dict[str, _Output] = {}
        self.pub: Optional[FanoutPublisher] = None
        self.before_start = 0
        self.keyed = asyncio.Event()
        self._feeders: List[asyncio.Task] = []

    def on_frame(self, name: str, frame: LocFrame, group_id: int,
                 object_id: int) -> None:
        now = time.monotonic_ns()
        if name == self.video_name and frame.key_frame:
            if self.ladder is None and self.rungs:
                self._start_ladder()
            self.keyed.set()
        if name == self.video_name and self.ladder is not None:
            self.ladder.push(frame, group_id, object_id)
        self._hand(name, frame, group_id, now)

    def _start_ladder(self) -> None:
        cymedia = importlib.import_module("cymedia")
        cymedia.set_ffmpeg_log_level(32 if self.args.debug else 16)
        src = self.sub.tracks[self.video_name]
        ladder = self.ladder = AbrLadder(
            self.rungs, config=src.config, fps=self.fps,
            profile=self.args.profile, timescale=src.timescale,
            on_frame=self._on_rung)
        ladder.start()

    def _on_rung(self, i: int, frame: LocFrame, group_id: int,
                 object_id: int, pushed_ns: Optional[int]) -> None:
        out = self.outputs.get(self.rungs[i].name)
        if out is not None and self.ladder is not None:
            out.track.config = self.ladder.configs[i]
        self._hand(self.rungs[i].name, frame, group_id, pushed_ns)

    def _hand(self, name: str, frame: LocFrame, group_id: int,
              t0: Optional[int]) -> None:
        out = self.outputs.get(name)
        if out is None:
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
        self.pub = FanoutPublisher(sessions, self.out_ns, catalog)
        timescale = self.sub.tracks[self.video_name].timescale
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
                    config=src.config, timescale=src.timescale,
                    media_kind='audio' if entry.role == 'audio' else 'video',
                    codec_string=src.codec_string)
                track.track_properties = (
                    dict(self.sub.properties(name)) or None)
            else:
                i = next(k for k, r in enumerate(self.rungs) if r.name == name)
                track = LocTrackPublisher(
                    self.pub.session, self.out_ns, name,
                    config=configs[i], timescale=timescale,
                    media_kind='video')
                track.track_properties = dict(video_props) or None
            tracks.append(track)
        for track in tracks:
            self.pub.add_track(track)
        await self.pub.start(forward=0)
        for track in tracks:
            out = self.outputs[track.trackname] = _Output(track)
            self._feeders.append(asyncio.ensure_future(out.feed()))
        print(f"  publishing '{self.out_ns}': "
              f"{', '.join(t.trackname for t in tracks)}")

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
                f"{self.ladder.input_dropped} dropped at input, groups "
                f"abandoned per rung "
                f"{[b['groups_abandoned'] for b in st['branches']]}")
        if final and self.before_start:
            lines.append(f"  {self.before_start} frames before publishing "
                         f"started")
        if lines:
            print('\n'.join(lines), flush=True)


async def run(args) -> int:
    _cli.apply_logging(args)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    async with contextlib.AsyncExitStack() as stack:
        if args.source:
            print(f"  source: {args.source} '{args.namespace}'")
            source = await _dial(args, args.source, stack)
        else:
            source = await _accept(args, stack)
        ingest: Optional[_Ingest] = None

        def on_frame(name, frame, group_id, object_id):
            if ingest is not None:
                ingest.on_frame(name, frame, group_id, object_id)

        sub, catalog = await _subscribe_source(args, source, on_frame)
        ingest = _Ingest(args, sub, catalog)
        print(f"  source tracks: {', '.join(sub.tracks)}; video "
              f"'{ingest.video.name}' {ingest.video.width}x"
              f"{ingest.video.height}")
        if ingest.rungs:
            print(f"  ladder ({args.profile}): "
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
            if any(c.done() for c in closed):
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
