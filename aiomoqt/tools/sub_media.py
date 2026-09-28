#!/usr/bin/env python3
"""LOC/MSF media subscriber — consumes an MSF broadcast and writes
playable files, or pipes one track straight into a player.

  %(prog)s moqt://localhost:4433/ -N demo/live -t 30 --out ./media-out

Reads the catalog track, subscribes every LOC track it describes, and
writes:
  <out>/video.h264   Annex-B elementary stream (SPS/PPS injected from
                     the catalog/wire decoder config at keyframes)
  <out>/audio.wav    pcm-s16 with header from the catalog track entry

Play them:  ffplay video.h264   /   ffplay audio.wav

Live piping (--pipe sends that track's raw stream to stdout, status
goes to stderr; the other track still writes to --out):

  %(prog)s URL -N demo/live --pipe video | \\
      ffplay -fflags nobuffer -flags low_delay -probesize 32 -f h264 -i -
  %(prog)s URL -N demo/live --pipe audio | \\
      ffplay -f s16le -ar 48000 -ch_layout stereo -i -

Delivery analysis (--analyze writes no media): measures whether the
network delivered the stream well enough to play, without decoding it.
One block per -i interval, then a summary; --report adds CSV.

  %(prog)s URL -N demo/live --analyze -t 60 -i 5 --report run.csv

Latency (ts_skew) is receive wall clock minus the publisher timestamp:
it carries the clock offset between publisher and subscriber when they
run on different machines. Jitter, the playout model and cross-track
skew are built from differences and do not.
"""
import asyncio
import csv
import logging
import os
import sys
import time
import wave

from aiomoqt.client import MOQTClient
from aiomoqt.media import MediaSubscriber
from aiomoqt.media.catalog import PACKAGING_CMAF
from aiomoqt.media.cmaf import init_timescale
from aiomoqt.media.loc import LOC_PROP_TIMESCALE
from aiomoqt.types import MOQTRequestError
from aiomoqt.media.sources import (
    IvfWriter, adts_frame, avcc_param_sets, lp_to_annexb,
)
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.media_stats import (
    CSV_FIELDS, MediaAnalysis, csv_row, format_interval, format_summary,
    interval_header, wall_clock_us,
)
from aiomoqt.utils.url import parse_relay_url


def parse_args():
    parser = _cli.make_parser(
        'LOC/MSF media subscriber (catalog-driven, writes playable '
        'files)', epilog=__doc__)
    _cli.add_endpoint(parser)
    _cli.add_identity(parser, namespace='demo/live')
    parser.add_argument('--discover', action='store_true',
                        help='Treat -N as a namespace prefix and find the '
                             'broadcast published under it (d18). Lets a '
                             'subscriber attach to a per-run namespace '
                             'like aiomoqt/demo-<rand4> without being told '
                             'which one.')
    parser.add_argument('--out', type=str, default='./media-out',
                        help='Output directory (default: ./media-out)')
    parser.add_argument('--pipe', choices=('video', 'audio'), default=None,
                        help='Stream this track raw to stdout for piping '
                             'into a player (video: Annex-B h264; audio: '
                             's16le pcm). Status moves to stderr; the '
                             'other track still writes to --out.')
    parser.add_argument('--show-catalog', action='store_true',
                        help='Print the full catalog JSON (and every '
                             'applied update) to stderr')
    parser.add_argument('--inspect', type=int, default=0, metavar='N',
                        help='Print per-frame wire detail for the first '
                             'N frames of each track (group/object ids, '
                             'size, key, property ids, ts_skew). ts_skew '
                             'is n/a for media-time timestamps')
    parser.add_argument('--analyze', action='store_true',
                        help='Measure delivery instead of writing media: '
                             'per track latency (ts_skew p50/p95/max), '
                             'RFC 3550 jitter, loss/reorder/duplicates, '
                             'group integrity, keyframe cost, bitrate, '
                             'and a playout model (would-be late / '
                             'underrun, buffer). ts_skew carries the '
                             'clock offset when publisher and subscriber '
                             'are on different machines; jitter does '
                             'not. --out is ignored.')
    parser.add_argument('--target-latency', type=float, default=None,
                        metavar='MS',
                        help='Playout cushion for --analyze; overrides '
                             'the catalog targetLatency (default: catalog, '
                             'else 500)')
    parser.add_argument('--report', type=str, default=None, metavar='PATH',
                        help='With --analyze: write interval and summary '
                             'rows as CSV to PATH (- = stdout)')
    _cli.add_run(parser, duration=30)
    _cli.add_session(parser, keepalive=True)
    _cli.add_help(parser)
    args = parser.parse_args()
    if args.analyze and args.pipe:
        parser.error('--analyze writes no media; drop --pipe')
    if args.report and not args.analyze:
        parser.error('--report requires --analyze')
    if args.interval <= 0:
        parser.error('-i/--interval must be > 0')
    return args


class _Writers:
    """Per-track sinks: LOC video → Annex-B .h264, pcm audio → .wav.
    One track may stream raw to stdout instead (pipe_role)."""

    def __init__(self, out_dir: str, subscriber: MediaSubscriber,
                 pipe_role: str = None):
        self.out = out_dir
        self.sub = subscriber
        self.pipe_role = pipe_role
        self.video = None
        self.ivf = None
        self.wav = None
        self.aac = None
        self.cmaf = {}
        self.counts = {}
        self.pipe_closed = False
        self.closed = False

    def _pipe(self, data: bytes) -> None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            self.pipe_closed = True

    def on_frame(self, name, frame, group_id, object_id):
        if self.closed:
            return  # late frame during teardown
        self.counts[name] = self.counts.get(name, 0) + 1
        entry = self.sub.catalog.find(name) if self.sub.catalog else None
        role = entry.role if entry else None
        if entry is not None and entry.packaging == 'cmaf':
            # CMAF chunks pass through verbatim; init segment (CMAF
            # header) from the catalog prefixes the fMP4 sink.
            init = self.sub.catalog.resolve_init(entry)
            if self.pipe_role == role:
                if name not in self.cmaf:
                    self.cmaf[name] = sys.stdout.buffer
                    if init:
                        self._pipe(init)
                self._pipe(frame.payload)
                return
            fh = self.cmaf.get(name)
            if fh is None:
                fh = open(os.path.join(self.out, f'{role or name}.mp4'),
                          'wb')
                self.cmaf[name] = fh
                if init:
                    fh.write(init)
            fh.write(frame.payload)
            return
        if role == 'video' and (entry.codec or '').startswith('av01'):
            # AV1 temporal units pass through verbatim; IVF wraps them
            # into an ffplay-playable stream (config OBUs are in-band).
            if self.pipe_role == 'video':
                if self.ivf is None:
                    self.ivf = IvfWriter(sys.stdout.buffer, entry.width,
                                         entry.height, entry.framerate)
                try:
                    self.ivf.add(frame.payload)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, ValueError):
                    self.pipe_closed = True
                return
            if self.ivf is None:
                self.ivf = IvfWriter(
                    open(os.path.join(self.out, 'video.ivf'), 'wb'),
                    entry.width, entry.height, entry.framerate)
            self.ivf.add(frame.payload)
        elif role == 'video':
            config = self.sub.tracks[name].config
            param_sets = (avcc_param_sets(config)
                          if frame.key_frame and config else b'')
            if self.pipe_role == 'video':
                self._pipe(param_sets + lp_to_annexb(frame.payload))
                return
            if self.video is None:
                self.video = open(os.path.join(self.out, 'video.h264'),
                                  'wb')
            self.video.write(param_sets)
            self.video.write(lp_to_annexb(frame.payload))
        elif role == 'audio' and (entry.codec or '').startswith('mp4a'):
            # Raw AAC AUs; the AudioSpecificConfig arrives via catalog
            # initRef. ADTS-wrapped output is directly playable.
            asc = self.sub.tracks[name].config
            if asc is None:
                return
            data = adts_frame(asc, frame.payload)
            if self.pipe_role == 'audio':
                self._pipe(data)
                return
            if self.aac is None:
                self.aac = open(os.path.join(self.out, 'audio.aac'), 'wb')
            self.aac.write(data)
        elif role == 'audio' and (entry.codec or '').startswith('pcm-s16'):
            if self.pipe_role == 'audio':
                self._pipe(frame.payload)
                return
            if self.wav is None:
                self.wav = wave.open(
                    os.path.join(self.out, 'audio.wav'), 'wb')
                self.wav.setnchannels(int(entry.channelConfig or 2))
                self.wav.setsampwidth(2)
                self.wav.setframerate(entry.samplerate or 48000)
            self.wav.writeframes(frame.payload)

    def close(self):
        self.closed = True
        if self.video:
            self.video.close()
        if self.ivf:
            self.ivf.close()
        if self.wav:
            self.wav.close()
        if self.aac:
            self.aac.close()
        for fh in self.cmaf.values():
            if fh is not sys.stdout.buffer:
                fh.close()


def _status(*parts):
    print(*parts, file=sys.stderr)


def _now_us() -> int:
    return int(time.time() * 1_000_000)


class _Inspector:
    """--inspect: wire detail for the first N frames of each track."""

    def __init__(self, n: int):
        self.n = n
        self.counts = {}

    def on_arrival(self, name, msg, recv_us, group_id, subgroup_id, frame):
        if frame is None:
            return
        c = self.counts[name] = self.counts.get(name, 0) + 1
        if c > self.n:
            return
        exts = msg.extensions or {}
        wall = wall_clock_us(frame.timestamp, exts.get(LOC_PROP_TIMESCALE),
                             recv_us)
        skew = None if wall is None else round((recv_us - wall) / 1000)
        _status(f"  [{name}] g{group_id}.o{msg.object_id} "
                f"{len(frame.payload)}B key={frame.key_frame} "
                f"ts_skew_ms={skew} "
                f"extra_props={sorted((frame.extensions or {}))}")


def _fan(*sinks):
    sinks = [s for s in sinks if s]
    if not sinks:
        return None

    def call(*a):
        for s in sinks:
            s(*a)
    return call


def _register(analysis: MediaAnalysis, catalog, entry) -> None:
    """Add a subscribed catalog track to the analysis before its first
    object can arrive."""
    cmaf = entry.packaging == PACKAGING_CMAF
    timescale = None
    if cmaf:
        init = catalog.resolve_init(entry)
        timescale = (init_timescale(init) if init else None) or entry.timescale
    analysis.add_track(entry.name, role=entry.role, timescale=timescale,
                       cmaf=cmaf, target_latency_ms=entry.targetLatency)


def _table(report, lines) -> None:
    """Analysis tables go to stdout unless the CSV report holds it."""
    out = sys.stderr if report and report[0] is sys.stdout else sys.stdout
    for line in lines:
        print(line, file=out)
    out.flush()


async def _analyze(args, analysis: MediaAnalysis, closed, report) -> None:
    """One interval block every -i seconds for -t seconds, or until the
    session closes."""
    _table(report, [interval_header(analysis.tracks)])
    start = time.monotonic()
    end = start + args.duration
    k = 0
    while not closed.done() and time.monotonic() < end:
        k += 1
        due = min(start + k * args.interval, end)
        await asyncio.wait([closed], timeout=max(0.0, due - time.monotonic()))
        t0, t1, rows, skew = analysis.interval(_now_us())
        _table(report, format_interval(t0, t1, rows))
        if report:
            for r in rows:
                report[1].writerow(csv_row('interval', t0, t1, r, skew))
            report[0].flush()


def _open_report(path):
    """(file, csv writer) with the header written; `-` is stdout."""
    if not path:
        return None
    fh = sys.stdout if path == '-' else open(path, 'w', newline='')
    w = csv.writer(fh)
    w.writerow(CSV_FIELDS)
    return fh, w


def _finish_analysis(analysis: MediaAnalysis, start_us: int, end_us: int,
                     report) -> int:
    """Print the summary; exit code 1 when any track delivered nothing."""
    rows, skew = analysis.summary(end_us)
    _table(report, format_summary(rows, skew))
    if report:
        elapsed = (end_us - start_us) / 1e6
        for r in rows:
            report[1].writerow(csv_row('summary', 0.0, elapsed, r,
                                       skew['p50_ms']))
        report[0].flush()
        if report[0] is not sys.stdout:
            report[0].close()
    if not analysis.tracks:
        _status("  FAIL: no media track subscribed")
        return 1
    silent = analysis.silent_tracks()
    if silent:
        _status(f"  FAIL: no objects delivered on {', '.join(silent)}")
        return 1
    return 0


async def run(args):
    set_log_level(logging.DEBUG if args.debug else logging.WARNING)
    relay = parse_relay_url(args.url)
    if not args.analyze:
        os.makedirs(args.out, exist_ok=True)
    analysis = (MediaAnalysis(cushion_ms=args.target_latency)
                if args.analyze else None)
    report = _open_report(args.report)
    inspector = _Inspector(args.inspect) if args.inspect else None

    client = MOQTClient(
        relay.host, relay.port, path=relay.path,
        use_quic=relay.use_quic, verify_tls=not args.insecure,
        supported_drafts=args.draft, debug=args.debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=args.cc_algo,
        keep_alive_interval=args.keepalive,
    )
    _status(f"  relay: {relay}  namespace: {args.namespace}")
    rc = 0
    async with client.connect() as session:
        await session.client_session_init()

        def _select(entry):
            if args.trackname is not None and entry.name != args.trackname:
                return False
            if analysis is not None:
                _register(analysis, sub.catalog, entry)
            return True

        sub = MediaSubscriber(
            session, args.namespace, discover=args.discover,
            track_filter=_select,
            on_catalog=((lambda c: _status(c.to_json(indent=2)))
                        if args.show_catalog else None))
        writers = (None if args.analyze
                   else _Writers(args.out, sub, pipe_role=args.pipe))
        if writers is not None:
            sub.on_frame = writers.on_frame
        sub.on_arrival = _fan(inspector and inspector.on_arrival,
                              analysis and analysis.on_arrival)
        try:
            catalog = await sub.start(timeout=args.duration)
        except MOQTRequestError as e:
            _status(f"  error: {e} — no publisher on "
                    f"'{args.namespace}'?")
            sys.exit(2)
        except asyncio.TimeoutError:
            _status(f"  error: no catalog received on "
                    f"'{args.namespace}' within {args.duration}s")
            sys.exit(2)
        _status(f"  catalog: {[t.name for t in catalog.tracks]}")
        if not sub.tracks:
            _status("  error: no LOC/CMAF catalog track"
                    + (f" named '{args.trackname}'" if args.trackname
                       else ""))
            sys.exit(2)
        if args.pipe == 'audio':
            a = catalog.find('audio')
            if a and (a.codec or '').startswith('mp4a'):
                _status("  piping ADTS aac — play with: ffplay -i -")
            else:
                layout = ('mono' if (a and a.channelConfig == '1')
                          else 'stereo')
                _status(f"  piping s16le — play with: ffplay -f s16le "
                        f"-ar {a.samplerate if a else 48000} "
                        f"-ch_layout {layout} -i -")
        elif args.pipe == 'video':
            _status("  piping h264 — play with: ffplay -fflags nobuffer "
                    "-flags low_delay -probesize 32 -f h264 -i -")
        closed = asyncio.ensure_future(session.async_closed())
        try:
            if analysis is not None:
                start_us = _now_us()
                analysis.start(start_us)
                try:
                    await _analyze(args, analysis, closed, report)
                finally:
                    # Summary also on Ctrl-C; late arrivals stay out of it.
                    sub.on_arrival = None
                    rc = _finish_analysis(analysis, start_us, _now_us(),
                                          report)
            else:
                async with asyncio.timeout(args.duration + 5):
                    while not writers.pipe_closed and not closed.done():
                        await asyncio.sleep(0.1)
        except asyncio.TimeoutError:
            pass
        finally:
            if not closed.done():
                closed.cancel()
            if writers is not None:
                writers.close()
    if writers is None:
        return rc
    for name, n in sorted(writers.counts.items()):
        _status(f"  {name}: {n} frames")
    if args.pipe is None:
        files = [n for n, f in (("video.h264", writers.video),
                                ("video.ivf", writers.ivf),
                                ("audio.wav", writers.wav),
                                ("audio.aac", writers.aac)) if f]
        files += [os.path.basename(fh.name)
                  for fh in writers.cmaf.values()
                  if fh is not sys.stdout.buffer]
        _status(f"  wrote {args.out}/{{{', '.join(files)}}} — "
                f"play each with ffplay")


def main():
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
