#!/usr/bin/env python3
"""LOC/MSF media subscriber — consumes an MSF broadcast and writes
playable files, or pipes one track straight into a player.

  %(prog)s moqt://localhost:4433/ -N demo/live -t 30 --out ./media-out

  # no relay: listen, and let the publisher dial in
  %(prog)s -Q -p 4433 -N demo/live --analyze -i 5
  pub_media moqt://HOST:4433/ -k -N demo/live --pub-ns --mp4 clip.mp4

With -Q/-W instead of a URL, sub_media listens and takes the first
publisher session to dial it; once that session announces -N
(PUBLISH_NAMESPACE, pub_media --pub-ns) it runs as a dialed run does.

Reads the catalog track, subscribes every media track it describes
(--packaging narrows to one packaging), and writes:
  <out>/video.h264   Annex-B elementary stream (SPS/PPS injected from
                     the catalog/wire decoder config at keyframes);
                     <name>.h264 when several LOC video tracks
  <out>/audio.wav    pcm-s16 with header from the catalog track entry
  <out>/<role>.mp4   fMP4 for CMAF and LOCMAF tracks (LOCMAF objects
                     rebuilt to CMAF chunks); <name>.mp4 when several
                     tracks share a role
  <out>/<name>.ts    the TS packets of an mpeg2ts (MSFTS) track

On exit it prints one line per track: `name: N frames, R rejected`.

Play them:  ffplay video.h264   /   ffplay audio.wav

Live piping (--pipe sends that track's raw stream to stdout, status
goes to stderr; the other track still writes to --out):

  %(prog)s URL -N demo/live --pipe video | \\
      ffplay -fflags nobuffer -flags low_delay -probesize 32 -f h264 -i -
  %(prog)s URL -N demo/live --pipe audio | \\
      ffplay -f s16le -ar 48000 -ch_layout stereo -i -

MPEG-TS out (--ts-out): the packets of one mpeg2ts track, in Group and
Object order as its objects arrive, to a file, stdout (-) or UDP, seven
packets to a datagram; with --ts-playout MS, each at its PCR time behind
an MS buffer. --analyze on that track writes a JSON line per -i interval,
each followed by the summary so far, and the run's summary at the end
(SIGTERM too): CC errors, PCR, delay variation, late packets and output
breaks at the buffer and at 100 to 1000 ms, object loss, and the delay
from the publisher's wall clock when it gives one (clocks must agree).

  %(prog)s URL -N contrib/feed --ts-out udp://127.0.0.1:6000
  %(prog)s URL -N contrib/feed --ts-out udp://127.0.0.1:6000 \\
      --ts-playout 300 --analyze -i 5 --report rx.jsonl

Frame log (--frame-log): a JSON line per LOC frame received, keyed by the
sha1 of its payload, the bytes aiomoqt's TS demuxer yields for that frame
of a pub_media --ts source.

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
import contextlib
import csv
import functools
import hashlib
import json
import math
import os
import signal
import sys
import time
import wave
from typing import Optional

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import SubscribedTrack
from aiomoqt.media import MediaSubscriber, MsftsTrackSubscriber
from aiomoqt.media.catalog import (
    MEDIA_PACKAGINGS, PACKAGING_CMAF, PACKAGING_LOC, PACKAGING_LOCMAF,
    PACKAGING_MEDIA_TIMELINE, PACKAGING_MPEG2TS,
)
from aiomoqt.media.cmaf import init_timescale
from aiomoqt.media import msfts, tsplayout
from aiomoqt.media.loc import LOC_PROP_TIMESCALE
from aiomoqt.media.msfts import MODE_ES_PACKETS, TS_PACKET
from aiomoqt.media.udp import UdpOutput, is_udp_url, parse_udp_url
from aiomoqt.types import MOQTMessageType, MOQTRequestError
from aiomoqt.media.sources import (
    IvfWriter, adts_frame, as_annexb, avcc_param_sets,
)
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.certs import ensure_self_signed, find_cert, find_key
from aiomoqt.utils.media_stats import (
    CSV_FIELDS, MediaAnalysis, csv_row, format_interval, format_summary,
    interval_header, wall_clock_us,
)
from aiomoqt.utils.hostinfo import CPU_FIELDS, cpu_fields, cpu_summary
from aiomoqt.utils.timing import (
    SOURCE_FRINDELL, TimestampMalformed, capture_time_us,
)
from aiomoqt.utils.url import parse_relay_url


def parse_args():
    parser = _cli.make_parser(
        'LOC/MSF media subscriber (catalog-driven, writes playable '
        'files)', epilog=__doc__)
    _cli.add_optional_endpoint(parser)
    listen = parser.add_argument_group(
        'listen for the publisher (instead of a URL)')
    _cli.add_listener(listen)
    _cli.add_tls(listen)
    _cli.add_identity(parser, namespace='demo/live')
    parser.add_argument('--discover', action='store_true',
                        help='Treat -N as a namespace prefix and find the '
                             'broadcast published under it (d18). Lets a '
                             'subscriber attach to a per-run namespace '
                             'like aiomoqt/demo-<rand4> without being told '
                             'which one.')
    parser.add_argument('--packaging', choices=MEDIA_PACKAGINGS,
                        default=None,
                        help='Subscribe only catalog tracks of this '
                             'packaging (default: every media track)')
    parser.add_argument('--out', type=str, default='./media-out',
                        help='Output directory (default: ./media-out)')
    parser.add_argument('--pipe', choices=('video', 'audio'), default=None,
                        help='Stream this track raw to stdout for piping '
                             'into a player (video: Annex-B h264; audio: '
                             's16le pcm). Status moves to stderr; the '
                             'other track still writes to --out.')
    parser.add_argument('--ts-out', type=str, default=None, metavar='URL',
                        help='Write the TS packets of the mpeg2ts (MSFTS) '
                             'track to udp://HOST:PORT (seven packets a '
                             'datagram), a file, or - for stdout, in Group '
                             'and Object order as objects arrive. Subscribes '
                             'that track only, and the one a catalog '
                             'replaces it with; -T picks one of several. '
                             'LOC tracks are refused.')
    ts_group = parser.add_argument_group('MPEG-TS (mpeg2ts tracks)')
    ts_group.add_argument(
        '--ts-playout', type=int, default=None, metavar='MS',
        help='With --ts-out: release each TS packet at its PCR time behind '
             'a fixed buffer of MS: first PCR arrival + MS + (PCR - first '
             'PCR) / 27 MHz, across PCR discontinuities. Default: as '
             'objects arrive.')
    ts_group.add_argument(
        '--ts-late', choices=('drop', 'emit'), default='drop',
        help='A packet past its release time: drop it (default) or emit it '
             'at once; counted late either way')
    ts_group.add_argument(
        '--break-ms', type=int, default=tsplayout.BREAK_MS, metavar='MS',
        help='A gap of MS or more in the released media is an output '
             f'break (default: {tsplayout.BREAK_MS})')
    ts_group.add_argument(
        '--arrival-log', type=str, default=None, metavar='FILE',
        help='Write a JSON line per PCR: pcr (27 MHz), arrival_ns and '
             'release_ns, wall clock')
    parser.add_argument('--frame-log', type=str, default=None,
                        metavar='FILE',
                        help='Write a JSON line per LOC frame received: '
                             'track, group, object, key, size, sha1 of the '
                             'payload (as aiomoqt\'s TS demuxer yields the '
                             'frame) and arrival_ns, wall clock')
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
                             'not. --out is ignored. On an mpeg2ts track: '
                             'a JSON line per interval and a summary (TS '
                             'packets, CC errors, PCR, delay variation, '
                             'late packets and breaks at several buffers), '
                             'beside --ts-out if given.')
    parser.add_argument('--target-latency', type=float, default=None,
                        metavar='MS',
                        help='Playout cushion for --analyze; overrides '
                             'the catalog targetLatency (default: catalog, '
                             'else 500)')
    parser.add_argument('--report', type=str, default=None, metavar='PATH',
                        help='With --analyze: write interval and summary '
                             'rows as CSV to PATH (- = stdout); JSON lines '
                             'for an mpeg2ts track')
    _cli.add_run(parser)
    _cli.add_interval(parser)
    _cli.add_session(parser)
    _cli.add_keepalive(parser)
    _cli.add_timestamp_checking(parser, default=None,
                                default_help='report with --analyze, '
                                             'else cancel')
    _cli.add_logging(parser)
    _cli.add_help(parser)
    args = parser.parse_args()
    _cli.check_url_vs_listener(parser, args)
    args.listen = bool(args.quic or args.wt)
    if not (args.url or args.listen):
        parser.error('give a URL to dial, or -Q/-W to listen for the '
                     'publisher')
    for flag, on in (('--cert', args.cert), ('--key', args.key)):
        if on and not args.listen:
            parser.error(f'{flag} applies to -Q/-W only')
    if args.listen and args.discover:
        parser.error('--discover asks a relay; with -Q/-W the publisher '
                     'announces -N')
    if args.analyze and args.pipe:
        parser.error('--analyze writes no media; drop --pipe')
    if args.report and not args.analyze:
        parser.error('--report requires --analyze')
    if args.ts_playout is not None:
        if args.ts_playout <= 0:
            parser.error('--ts-playout must be positive')
        if not (args.ts_out or args.analyze):
            parser.error('--ts-playout needs --ts-out or --analyze')
    if args.break_ms <= 0:
        parser.error('--break-ms must be positive')
    if args.ts_out:
        if args.packaging is not None:
            parser.error('--ts-out subscribes the mpeg2ts track; drop '
                         '--packaging')
        if args.ts_out == '-' and args.pipe:
            parser.error('--ts-out - and --pipe both write stdout')
        if is_udp_url(args.ts_out):
            try:
                parse_udp_url(args.ts_out, listen=False)
            except ValueError as e:
                parser.error(f'--ts-out: {e}')
    if args.interval <= 0:
        parser.error('-i/--interval must be > 0')
    return args


_CHUNKED = (PACKAGING_CMAF, PACKAGING_LOCMAF)


class _Writers:
    """Per-track sinks: LOC video → Annex-B .h264 (video.h264, or
    <name>.h264 when several wanted LOC video tracks), pcm audio → .wav,
    CMAF and LOCMAF → fMP4 (<role>.mp4, or <name>.mp4 when several wanted
    tracks share the role), mpeg2ts → its packets (<name>.ts). One track
    may stream raw to stdout instead (pipe_role)."""

    def __init__(self, out_dir: str, subscriber: MediaSubscriber,
                 pipe_role: str = None, wanted=None):
        self.out = out_dir
        self.sub = subscriber
        self.pipe_role = pipe_role
        self.wanted = wanted or (lambda entry: True)
        self.ts = {}
        self.ts_bytes = {}
        self.h264 = {}
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
        if entry is not None and entry.packaging == PACKAGING_MPEG2TS:
            sink = self.ts.get(name)
            if sink is None:
                sink = self.ts[name] = open(
                    os.path.join(self.out, f'{entry.name}.ts'), 'wb',
                    buffering=0)
            sink.write(frame.payload)
            self.ts_bytes[name] = (self.ts_bytes.get(name, 0)
                                   + len(frame.payload))
            return
        if entry is not None and entry.packaging in _CHUNKED:
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
                fh = open(os.path.join(self.out,
                                       f'{self._stem(entry)}.mp4'), 'wb')
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
                self._pipe(param_sets + as_annexb(frame.payload))
                return
            fh = self.h264.get(name)
            if fh is None:
                fh = self.h264[name] = open(
                    os.path.join(self.out, f'{self._h264_stem(entry)}.h264'),
                    'wb')
            fh.write(param_sets)
            fh.write(as_annexb(frame.payload))
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

    def _stem(self, entry) -> str:
        if entry.role is None:
            return entry.name
        peers = [t for t in self.sub.catalog.tracks
                 if t.role == entry.role and t.packaging in _CHUNKED
                 and self.wanted(t)]
        return entry.name if len(peers) > 1 else entry.role

    def _h264_stem(self, entry) -> str:
        catalog = self.sub.catalog
        peers = [t for t in (catalog.tracks if catalog else ())
                 if t.role == 'video' and t.packaging not in _CHUNKED
                 and not (t.codec or '').startswith('av01')
                 and self.wanted(t)]
        return entry.name if len(peers) > 1 else 'video'

    def close(self):
        self.closed = True
        for fh in self.h264.values():
            fh.close()
        if self.ivf:
            self.ivf.close()
        if self.wav:
            self.wav.close()
        if self.aac:
            self.aac.close()
        for fh in self.cmaf.values():
            if fh is not sys.stdout.buffer:
                fh.close()
        for sink in self.ts.values():
            sink.close()


class _StdoutSink:
    """--ts-out -: stdout, until its reader goes away."""

    def __init__(self):
        self.closed = False

    def write(self, data: bytes) -> None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            self.closed = True

    def close(self) -> None:
        pass


def _ts_sink(dest: str, packet_size: int):
    """--ts-out: UDP, stdout or a file written unbuffered."""
    if is_udp_url(dest):
        return UdpOutput(dest, packet_size)
    if dest == '-':
        return _StdoutSink()
    return open(dest, 'wb', buffering=0)


class _TsLane:
    """The one mpeg2ts track a TS run subscribes: its packets to
    --ts-out as objects arrive or, with --ts-playout, at their PCR
    times; with --analyze, --ts-playout or --arrival-log, a TsPlayout
    model of it. When the catalog replaces the track (msfts-02 §6), the
    lane goes on in the next one once the old one ends (`switches`)."""

    def __init__(self, args):
        self.args = args
        self.name: Optional[str] = None
        self.track = None
        self.player: Optional[tsplayout.TsPlayout] = None
        self.sink = None
        self.objects = 0
        self.bytes = 0
        self.packet_size = TS_PACKET
        self._log = None
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.switches = 0
        self.stale = 0
        self._next = None
        self._parked: list = []
        self._park_timer = None

    def start(self, entry, track) -> None:
        args = self.args
        self.name, self.track = entry.name, track
        self.packet_size = entry.extra.get('mpeg2tsPacketSize', TS_PACKET)
        if args.ts_out:
            self.sink = _ts_sink(args.ts_out, self.packet_size)
        if args.analyze or args.ts_playout is not None or args.arrival_log:
            if args.arrival_log:
                self._log = open(args.arrival_log, 'w')
            self.player = tsplayout.TsPlayout(
                args.ts_playout, late=args.ts_late, break_ms=args.break_ms,
                pcr_pid=entry.extra.get('mpeg2tsPcrPid'),
                arrival_log=self._log,
                release=args.ts_playout is not None and self.sink is not None)
            timing = getattr(track, 'timing', None)
            if timing is not None and timing.declared and timing.wall_clock:
                # Every object carries its time: no timeline samples.
                self.player.e2e_source = SOURCE_FRINDELL
        if self.player is not None and self.player.release:
            self._task = asyncio.ensure_future(self._play())

    def follow(self, entry, track) -> None:
        """Go on in `track` (catalog entry `entry`) once the current
        track ends, or HOLD_S after its first frame."""
        self._next = (entry, track)
        old = self.track
        if old is None or getattr(old, 'ended', True):
            self._switch()
        else:
            old.on_end = self._switch

    def _switch(self) -> None:
        if self._next is None:
            return
        entry, track = self._next
        self._next = None
        if self._park_timer is not None:
            self._park_timer.cancel()
            self._park_timer = None
        old = self.track
        if old is not None:
            old.on_end = None
            old.flush()
        self.name, self.track = entry.name, track
        self.switches += 1
        if self.player is not None:
            self.player.switch()
        _status(f"  switched to {entry.name}")
        parked, self._parked = self._parked, []
        for frame, group_id, object_id in parked:
            self.on_frame(entry.name, frame, group_id, object_id)

    def _frindell_us(self, frame, group_id, object_id) -> Optional[int]:
        """The object's draft-frindell time on the publisher's wall
        clock, µs; None when the track declares none."""
        timing = getattr(self.track, 'timing', None)
        if timing is None or not timing.declared:
            return None
        try:
            us, source = capture_time_us(timing, group_id, object_id,
                                         frame.extensions)
        except TimestampMalformed:
            return None
        return us if source == SOURCE_FRINDELL else None

    def timeline(self, name, records) -> None:
        if name == self.name and self.player is not None:
            self.player.timeline(records)

    def on_frame(self, name, frame, group_id, object_id) -> None:
        if name != self.name:
            nxt = self._next
            if nxt is not None and name == nxt[0].name:
                self._parked.append((frame, group_id, object_id))
                if self._park_timer is None:
                    self._park_timer = asyncio.get_running_loop().call_later(
                        msfts.HOLD_S, self._switch)
            else:
                self.stale += 1
            return
        payload = frame.payload
        self.objects += 1
        self.bytes += len(payload)
        p = self.player
        if p is not None:
            p.push(payload, time.monotonic_ns(), group_id, object_id,
                   self._frindell_us(frame, group_id, object_id),
                   time.time_ns())
            if p.release:
                self._wake.set()
                return
        if self.sink is not None:
            self.sink.write(payload)

    async def _play(self) -> None:
        """Release the queued packets as they fall due."""
        p, sink = self.player, self.sink
        if p is None or sink is None:
            return
        while True:
            due = p.due()
            if due is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            wait = (due - time.monotonic_ns()) / 1e9
            if wait > 0.0005:
                await asyncio.sleep(wait)
            data = p.pop(time.monotonic_ns() + 500_000)
            if data:
                sink.write(data)

    def report(self, kind: str, t0: float, t1: float,
               final: bool = True) -> dict:
        """An interval, or the summary: of the run, or with `final`
        False, of the intervals reported so far."""
        p = self.player
        assert p is not None
        if self.track is not None:
            p.dups = self.track.order.late
        body = (p.interval() if kind == 'interval' else
                p.summary() if final else p.totals())
        return {"type": kind, "track": self.name, "t0": round(t0, 3),
                "t1": round(t1, 3), **body}

    def close(self) -> None:
        sink, p = self.sink, self.player
        if self._task is not None:
            self._task.cancel()
            if sink is not None and p is not None:
                rest = p.pop(1 << 62)
                if rest:
                    sink.write(rest)
        if sink is not None:
            sink.close()
        if self._log is not None:
            self._log.close()


async def _follow_timeline(session, namespace, catalog, lane,
                           name: str) -> None:
    """Feed lane the MSF media timeline of track `name`, if the catalog
    has one (<name>.timeline, from pub_media or moq2ts): the publisher's
    wall clock per group."""
    entry = next((t for t in catalog.tracks
                  if t.packaging == PACKAGING_MEDIA_TIMELINE
                  and name in (t.depends or ())), None)
    if entry is None or lane.player is None:
        return

    def on_object(msg, size, recv_us, group_id, subgroup_id):
        if getattr(msg, 'status', None) not in (None, 0):
            return
        try:
            records = json.loads(bytes(msg.payload))
        except ValueError:
            return
        if isinstance(records, list):
            lane.timeline(name, records)
    track = SubscribedTrack(session, entry.namespace or namespace,
                            entry.name, on_object=on_object)
    try:
        await track.subscribe()
    except Exception as e:
        _status(f"  note: media timeline {entry.name}: {e}")


class _FrameLog:
    """--frame-log: a JSON line per LOC frame received."""

    def __init__(self, path: str, subscriber: MediaSubscriber):
        self.fh = open(path, 'w', buffering=1)
        self.sub = subscriber
        self._loc = {}

    def on_arrival(self, name, msg, recv_us, group_id, subgroup_id, frame):
        if frame is None:
            return
        loc = self._loc.get(name)
        if loc is None:
            entry = self.sub.catalog.find(name) if self.sub.catalog else None
            loc = self._loc[name] = (entry is not None
                                     and entry.packaging == PACKAGING_LOC)
        if not loc:
            return
        payload = frame.payload
        self.fh.write(json.dumps({
            'track': name, 'group': group_id, 'object': msg.object_id,
            'key': frame.key_frame, 'size': len(payload),
            'sha1': hashlib.sha1(payload).hexdigest(),
            'arrival_ns': time.time_ns()}, separators=(',', ':')) + '\n')

    def close(self) -> None:
        self.fh.close()


def _ts_out_track(args, catalog):
    """The catalog track --ts-out writes, or None with why not."""
    tracks = [t for t in catalog.tracks if args.trackname is None
              or t.name == args.trackname]
    ts = [t for t in tracks if t.packaging == PACKAGING_MPEG2TS]
    if len(ts) == 1:
        if ts[0].extra.get('mpeg2tsMode') == MODE_ES_PACKETS:
            return None, (f"track '{ts[0].name}' is es-packets, one PID; "
                          f"--ts-out needs a program or a multiplex")
        return ts[0], None
    if ts:
        return None, (f"several mpeg2ts tracks "
                      f"({', '.join(t.name for t in ts)}); pick one with -T")
    if args.trackname is not None and tracks:
        return None, (f"track '{args.trackname}' is {tracks[0].packaging}, "
                      f"not mpeg2ts")
    return None, ("no mpeg2ts track"
                  + (f" named '{args.trackname}'" if args.trackname else "")
                  + " in the catalog (tracks: "
                  + ', '.join(f'{t.name} {t.packaging}'
                              for t in catalog.tracks) + ")")


def _timestamp_policy(args) -> str:
    """--timestamp-errors, else report under --analyze (a diagnosis keeps
    the track) and cancel otherwise."""
    return args.timestamp_errors or ('report' if args.analyze else 'cancel')


def _status(*parts):
    print(*parts, file=sys.stderr)


def _now_us() -> int:
    return int(time.time() * 1_000_000)


class _Inspector:
    """--inspect: wire detail for the first N frames of each track."""

    def __init__(self, n: int):
        self.n = n
        self.counts = {}
        self.timing_of = lambda name: None

    def on_arrival(self, name, msg, recv_us, group_id, subgroup_id, frame):
        if frame is None:
            return
        c = self.counts[name] = self.counts.get(name, 0) + 1
        if c > self.n:
            return
        exts = msg.extensions or {}
        try:
            cap, src = capture_time_us(self.timing_of(name), group_id,
                                        msg.object_id, exts)
        except TimestampMalformed:
            cap, src = None, 'malformed'
        wall = (cap if src == 'frindell' else
                wall_clock_us(frame.timestamp, exts.get(LOC_PROP_TIMESCALE),
                              recv_us))
        skew = None if wall is None else round((recv_us - wall) / 1000)
        _status(f"  [{name}] g{group_id}.o{msg.object_id} "
                f"{len(frame.payload)}B key={frame.key_frame} "
                f"ts_skew_ms={skew} ts_source={src} "
                f"extra_props={sorted((frame.extensions or {}))}")


def _fan(*sinks):
    sinks = [s for s in sinks if s]
    if not sinks:
        return None

    def call(*a):
        for s in sinks:
            s(*a)
    return call


def _wanted(args, entry) -> bool:
    """Catalog track selected by -T and --packaging."""
    return ((args.trackname is None or entry.name == args.trackname)
            and (args.packaging is None or entry.packaging == args.packaging))


def _register(analysis: MediaAnalysis, catalog, entry) -> None:
    """Add a subscribed catalog track to the analysis before its first
    object can arrive."""
    cmaf = entry.packaging in _CHUNKED
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
    end = start + args.duration if args.duration else math.inf
    k = 0
    while not closed.done() and time.monotonic() < end:
        k += 1
        due = min(start + k * args.interval, end)
        await asyncio.wait([closed], timeout=max(0.0, due - time.monotonic()))
        t0, t1, rows, skew = analysis.interval(_now_us())
        _table(report, format_interval(t0, t1, rows))
        if report:
            for r in rows:
                report[1].writerow(csv_row('interval', t0, t1, r, skew)
                                   + cpu_fields())
            report[0].flush()


def _open_report(path, csv_header: bool = True):
    """(file, csv writer) with the header written; `-` is stdout. An
    mpeg2ts track's report is JSON lines, no header."""
    if not path:
        return None
    fh = sys.stdout if path == '-' else open(path, 'w', newline='')
    w = csv.writer(fh)
    if csv_header:
        w.writerow((*CSV_FIELDS, *CPU_FIELDS))
    return fh, w


def _finish_analysis(analysis: MediaAnalysis, start_us: int, end_us: int,
                     report) -> int:
    """Print the summary; exit code 1 when any track delivered nothing."""
    rows, skew = analysis.summary(end_us)
    _table(report, [*format_summary(rows, skew), f"  host: {cpu_summary()}"])
    if report:
        elapsed = (end_us - start_us) / 1e6
        for r in rows:
            report[1].writerow(csv_row('summary', 0.0, elapsed, r,
                                       skew['p50_ms']) + cpu_fields())
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


def _ts_forced(args) -> bool:
    """A flag that only an mpeg2ts track serves."""
    return bool(args.ts_out or args.ts_playout is not None
                or args.arrival_log)


async def _ts_run(args, lane: _TsLane, closed, report) -> None:
    """--analyze on an mpeg2ts track: a JSON line per -i interval and the
    summary so far, then the run's summary; else wait for the run to
    end."""
    out = report[0] if report else sys.stdout
    start = time.monotonic()
    end = start + args.duration if args.duration else math.inf
    k = 0
    last = 0.0
    try:
        while not closed.done() and time.monotonic() < end:
            if lane.sink is not None and getattr(lane.sink, 'closed', False):
                break
            k += 1
            due = min(start + k * args.interval, end)
            await asyncio.wait([closed],
                               timeout=max(0.0, due - time.monotonic()))
            now = time.monotonic() - start
            if lane.player is not None and args.analyze:
                print(json.dumps(lane.report('interval', last, now)),
                      file=out)
                print(json.dumps(lane.report('summary', 0.0, now, False)),
                      file=out, flush=True)
            last = now
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
    if lane.player is not None and args.analyze:
        if lane.track is not None:
            lane.track.flush()
        now = time.monotonic() - start
        if now - last > 0.001:
            print(json.dumps(lane.report('interval', last, now)),
                  file=out, flush=True)
        print(json.dumps(lane.report('summary', 0.0, now)), file=out,
              flush=True)
    if report and report[0] is not sys.stdout:
        report[0].close()


def _listener_cert(args):
    """(cert, key): --cert/--key, else certs/, else a generated
    self-signed localhost cert."""
    cert = args.cert or find_cert()
    key = args.key or find_key(cert)
    if cert and key:
        return cert, key
    if args.cert:
        raise SystemExit(f"--cert {args.cert}: no key beside it; use --key")
    return ensure_self_signed()


def _ns(parts) -> str:
    return '/'.join(p.decode() if isinstance(p, bytes) else p for p in parts)


@contextlib.asynccontextmanager
async def _dial(args):
    relay = parse_relay_url(args.url)
    client = MOQTClient(
        relay.host, relay.port, path=relay.path,
        use_quic=relay.use_quic, verify_tls=not args.insecure,
        supported_drafts=args.draft, debug=args.debug,
        keylog_filename=args.keylogfile,
        **_cli.transport_kwargs(args),
    )
    _status(f"  relay: {relay}  namespace: {args.namespace}")
    async with client.connect() as session:
        await session.client_session_init()
        yield session


@contextlib.asynccontextmanager
async def _listen(args):
    """The first publisher session to dial in, once it has announced -N;
    later sessions are refused."""
    quic, wt = _cli.resolve_listener(args)
    cert, key = _listener_cert(args)
    loop = asyncio.get_running_loop()
    first = loop.create_future()
    announced = loop.create_future()
    server = MOQTServer(
        host=args.bind, port=args.port, certificate=cert,
        private_key=key, path=args.path, use_quic=quic and not wt,
        supported_drafts=args.draft, debug=args.debug,
        **_cli.transport_kwargs(args))

    def on_session(session):
        if not first.done():
            first.set_result(session)
            server.refuse_sessions("one publisher session at a time")

    async def on_namespace(session, msg):
        session.publish_namespace_ok(msg)
        ns = _ns(msg.namespace)
        if ns == args.namespace and not announced.done():
            announced.set_result(session)
        elif ns != args.namespace:
            _status(f"  note: ignoring announced '{ns}'")
    server.register_session_handler(on_session)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE, on_namespace)
    handle = await (server.serve_dual() if quic and wt else server.serve())
    try:
        host = args.bind or 'localhost'
        _status(f"  listening: {'QUIC+WT' if quic and wt else 'QUIC' if quic else 'WT'}"
                f" {host}:{args.port}{args.path}  namespace: {args.namespace}")
        session = await first
        _status(f"  publisher up: draft-{session.negotiated_draft}")
        closed = asyncio.ensure_future(session.async_closed())
        try:
            async with asyncio.timeout(_cli.setup_timeout(args)):
                await asyncio.wait({announced, closed},
                                   return_when=asyncio.FIRST_COMPLETED)
        except asyncio.TimeoutError:
            pass
        finally:
            closed.cancel()
        if not announced.done():
            _status(f"  error: the publisher did not announce "
                    f"'{args.namespace}' within {_cli.setup_timeout(args)}s")
            sys.exit(2)
        yield session
    finally:
        await server.close_sessions()
        handle.close()


async def run(args):
    _cli.apply_logging(args)
    # True: the mpeg2ts track only; None: decided by the catalog.
    ts_mode = True if _ts_forced(args) else None if args.analyze else False
    if not args.analyze and not ts_mode:
        os.makedirs(args.out, exist_ok=True)
    analysis = (MediaAnalysis(cushion_ms=args.target_latency)
                if args.analyze else None)
    report = None
    inspector = _Inspector(args.inspect) if args.inspect else None
    frame_log = None
    lane = _TsLane(args)
    sub = None

    def _timing_of(name):
        track = sub.tracks.get(name) if sub is not None else None
        return track.timing if track is not None else None
    if inspector is not None:
        inspector.timing_of = _timing_of
    if analysis is not None:
        analysis.timing_of = _timing_of

    rc = 0
    opened = (_listen(args) if getattr(args, 'listen', False)
              else _dial(args))
    async with opened as session:
        def _select(entry):
            nonlocal ts_mode
            if not _wanted(args, entry) or sub is None:
                return False
            ts_entry = _ts_out_track(args, sub.catalog)[0]
            if ts_mode is None:
                ts_mode = ts_entry is not None
            if ts_mode:
                return ts_entry is entry
            if analysis is not None:
                _register(analysis, sub.catalog, entry)
            return True

        def _on_frame(name, frame, group_id, object_id):
            if ts_mode:
                lane.on_frame(name, frame, group_id, object_id)
            elif writers is not None:
                writers.on_frame(name, frame, group_id, object_id)

        def _analysis_arrival(*a):
            if analysis is not None and not ts_mode:
                analysis.on_arrival(*a)

        following = set()

        async def _switch_to(entry):
            if sub is None:
                return
            # A name the stream comes back to: its old track has ended.
            old = sub.tracks.get(entry.name)
            if old is not None and getattr(old, 'ended', False):
                sub.tracks.pop(entry.name)
            try:
                track = await sub.subscribe_track(entry)
            except Exception as e:
                _status(f"  error: {entry.name}: {e}")
                return
            finally:
                following.discard(entry.name)
            if track is not None:
                lane.follow(entry, track)
                await _follow_timeline(session, args.namespace, sub.catalog,
                                       lane, entry.name)

        def _on_catalog(catalog):
            if args.show_catalog:
                _status(catalog.to_json(indent=2))
            if not ts_mode or lane.track is None or sub is None:
                return
            entry = _ts_out_track(args, catalog)[0]
            if (entry is not None and entry.name != lane.name
                    and entry.name not in following):
                following.add(entry.name)
                asyncio.ensure_future(_switch_to(entry))

        sub = MediaSubscriber(
            session, args.namespace, discover=args.discover,
            track_filter=_select,
            timestamp_errors=_timestamp_policy(args),
            on_catalog=_on_catalog)
        writers = (None if args.analyze or ts_mode
                   else _Writers(args.out, sub, pipe_role=args.pipe,
                                 wanted=functools.partial(_wanted, args)))
        sub.on_frame = _on_frame
        if args.frame_log:
            frame_log = _FrameLog(args.frame_log, sub)
        sub.on_arrival = _fan(inspector and inspector.on_arrival,
                              analysis and _analysis_arrival,
                              frame_log and frame_log.on_arrival)
        try:
            catalog = await sub.start(timeout=_cli.setup_timeout(args))
        except MOQTRequestError as e:
            _status(f"  error: {e} — no publisher on "
                    f"'{args.namespace}'?")
            sys.exit(2)
        except asyncio.TimeoutError:
            _status(f"  error: no catalog received on "
                    f"'{args.namespace}' within "
                    f"{_cli.setup_timeout(args)}s")
            sys.exit(2)
        _status(f"  catalog: {[t.name for t in catalog.tracks]}")
        if ts_mode and not sub.tracks:
            entry, why = _ts_out_track(args, catalog)
            flag = '--ts-out' if args.ts_out else (
                '--ts-playout' if args.ts_playout is not None
                else '--arrival-log')
            _status(f"  error: {flag}: " + (
                why or f"track '{entry.name}' was not subscribed: its "
                       f"MSFTS fields are invalid"))
            sys.exit(2)
        if not sub.tracks:
            _status("  error: no media catalog track"
                    + (f" named '{args.trackname}'" if args.trackname
                       else "")
                    + (f" of packaging {args.packaging}" if args.packaging
                       else ""))
            sys.exit(2)
        report = _open_report(args.report, csv_header=not ts_mode)
        if ts_mode:
            entry = _ts_out_track(args, catalog)[0]
            lane.start(entry, sub.tracks[entry.name])
            await _follow_timeline(session, args.namespace, catalog, lane,
                                   entry.name)
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
            if ts_mode:
                await _ts_run(args, lane, closed, report)
            elif analysis is not None:
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
                try:
                    async with asyncio.timeout(
                            args.duration + 5 if args.duration else None):
                        while not writers.pipe_closed and not closed.done():
                            await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    # Ctrl-C: frame counts below still print.
                    asyncio.current_task().uncancel()
        except asyncio.TimeoutError:
            pass
        finally:
            if not closed.done():
                closed.cancel()
            for track in sub.tracks.values():
                if isinstance(track, MsftsTrackSubscriber):
                    track.flush()
            if writers is not None:
                writers.close()
            lane.close()
            if frame_log is not None:
                frame_log.close()
    if ts_mode:
        return _ts_status(args, lane)
    if writers is None:
        return rc
    for name in sorted(set(writers.counts) | set(sub.tracks)):
        line = f"  {name}: {writers.counts.get(name, 0)} frames"
        track = sub.tracks.get(name)
        rejected = getattr(track, 'objects_rejected', None)
        if rejected is not None:
            line += f", {rejected} rejected"
        _status(line)
    if args.pipe is None:
        files = [os.path.basename(fh.name) for fh in writers.h264.values()]
        files += [n for n, f in (("video.ivf", writers.ivf),
                                 ("audio.wav", writers.wav),
                                 ("audio.aac", writers.aac)) if f]
        files += [os.path.basename(fh.name)
                  for fh in writers.cmaf.values()
                  if fh is not sys.stdout.buffer]
        files += [os.path.basename(fh.name) for fh in writers.ts.values()]
        _status(f"  wrote {args.out}/{{{', '.join(files)}}} — "
                f"play each with ffplay")
    return rc


def _ts_status(args, lane: _TsLane) -> int:
    """The TS run's last lines; 1 when no object arrived."""
    track = lane.track
    line = (f"  {lane.name}: {lane.objects} objects, "
            f"{lane.bytes // lane.packet_size} packets")
    if track is not None:
        line += (f", {track.objects_rejected} rejected, "
                 f"{track.order.late} late, {track.order.skipped} skipped")
    if lane.switches:
        line += f", {lane.switches} track switches"
    p = lane.player
    if p is not None and p.model is not None:
        line += (f"; playout {p.buffer_ms} ms: {p.model.late} late, "
                 f"{p.model.breaks} breaks")
    _status(line)
    if args.ts_out:
        _status(f"  wrote {args.ts_out}")
    if not lane.objects:
        _status(f"  FAIL: no objects delivered on {lane.name or 'any track'}")
        return 1
    return 0


async def _main(args):
    """run() as the program: SIGTERM stops it as Ctrl-C does, so the
    summaries still print."""
    task = asyncio.current_task()
    try:
        if task is not None:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError, ValueError):
        pass
    return await run(args)


def main():
    try:
        sys.exit(_cli.run(_main(parse_args())))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == '__main__':
    main()
