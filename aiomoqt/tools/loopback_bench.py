#!/usr/bin/env python3
"""aiomoqt-bench loopback - direct pub-to-sub benchmark without a relay.

Runs the publisher as a server that the subscriber connects to directly.
Measures pure Python throughput on the aiopquic stack without relay overhead.

Usage:
  python -m aiomoqt.tools.loopback_bench -s 4096 -r 5000 -t 20
  python -m aiomoqt.tools.loopback_bench -P 4 -s 16384 -r 60 -t 20
  python -m aiomoqt.tools.loopback_bench --fanout -s 4096 -t 20
"""
import argparse
import asyncio
import sys
import time

from aiomoqt.types import MOQTMessageType, parse_draft_spec
from aiomoqt.client import MOQTClient
from aiomoqt.delivery import StreamMapping
from aiomoqt.server import MOQTServer
from aiomoqt.track import (_PACED_YIELD_EVERY, PublishedTrack,
                           SubscribedTrack)
from aiomoqt.types import LOC_TIMESTAMP, ForwardingPreference
from aiomoqt.utils import cli as _cli
from aiomoqt.utils import wait_cond_timeout
from aiomoqt.utils.certs import ensure_self_signed, find_cert, find_key
from aiomoqt.tools.sub_bench import BenchReporter


CERT = find_cert()
KEY = find_key(CERT)


def parse_args():
    parser = argparse.ArgumentParser(
        add_help=False,
        description='aiomoqt-bench loopback - direct pub/sub, no relay',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '-s', '--object-size', type=int, default=4096,
        help='Object payload size bytes (default: 4096)')
    parser.add_argument(
        '-g', '--group-size', type=int, default=4096,
        help='Objects per group (default: 10000)')
    parser.add_argument(
        '-P', '--streams', type=int, default=1,
        help='Parallel subgroup streams (default: 1)')
    parser.add_argument(
        '-r', '--rate', type=float, default=0,
        help='Aggregate objects/sec across all streams (0=max, '
             'default: max). Per-stream emit rate is rate/streams. '
             '-P only changes parallelism, not offered load.')
    parser.add_argument(
        '-t', '--duration', type=int, default=20,
        help='Duration seconds (default: 20)')
    parser.add_argument(
        '-i', '--interval', type=float, default=5.0,
        help='Report interval seconds (default: 5)')
    parser.add_argument(
        '--no-stats', action='store_true',
        help='Count objects and bytes only — no latency, jitter, loss '
             'or group accounting. Measures the delivery ceiling '
             'without the measurement in it.')
    parser.add_argument(
        '--bind', type=str, default='localhost',
        help='Address the publisher listens on and the subscriber '
             'dials (default: localhost)')
    parser.add_argument(
        '-p', '--port', type=int, default=4434,
        help='Local port (default: 4434)')
    parser.add_argument(
        '--cert', type=str, default=None,
        help='TLS certificate (default: the self-signed test cert)')
    parser.add_argument(
        '--key', type=str, default=None,
        help='TLS key (default: the test cert\'s key)')
    parser.add_argument(
        '-k', '--insecure', action='store_true', default=None,
        help='Skip certificate verification (the default unless --cert '
             'is given; then that cert is the trust anchor)')
    parser.add_argument(
        '-d', '--debug', action='store_true')
    parser.add_argument(
        '-Q', '--quic', action='store_true',
        help='Raw QUIC (default: H3/WebTransport)')
    parser.add_argument(
        '-W', '--wt', action='store_true',
        help='H3/WebTransport (the default)')
    parser.add_argument(
        '-D', '--datagram', action='store_true',
        help='ObjectDatagrams instead of subgroup streams (object '
             'must fit one packet, ~1150B max payload). '
             'The stream/datagram A/B lever: run twice at the same -s '
             'and -r, once with and once without.')
    parser.add_argument(
        '--fanout', action='store_true',
        help='Publish through produce() and its per-subscriber send lane '
             '(FanoutDelivery), the path LOC media tracks take, instead '
             'of the per-subscriber generator. One subgroup per group.')
    parser.add_argument(
        '--per-peer-props', action='store_true',
        help='With --fanout: object properties as a callable resolved '
             'per subscriber, as LOC media tracks build them, instead of '
             'the generator\'s one dict per object.')
    parser.add_argument(
        '--draft', type=parse_draft_spec, default=14,
        help='MoQT draft version to negotiate (e.g. 14, 16, or 18, '
             'default: 14). Applied to BOTH the loopback publisher '
             '(server) and subscriber (client) so the raw-QUIC ALPN '
             '("moq-00" for d14, "moqt-NN" for d16+) and the WT version '
             'match. Auto-negotiation is intentionally not used here: a '
             'd14 server offers only "moq-00" while a d16+ client offers '
             '"moqt-NN", so the asymmetric ALPN offer fails to connect.')
    parser.add_argument(
        '--cc-algo', type=str, default='bbr',
        help='Congestion control (bbr | bbr1 | c4 | cubic | dcubic | '
             'fast | newreno | prague). Default: bbr')
    parser.add_argument(
        '--max-inflight-bytes', type=int, default=None,
        help='Per-stream TX budget (aiomoqt tx_max_inflight_bytes): '
             'producer pauses while one stream\'s un-transmitted bytes '
             'exceed this. Default: aiomoqt default (1 MiB). '
             'Pass 0 to disable.')
    parser.add_argument(
        '--max-queued-bytes', type=int, default=None,
        help='Aggregate publisher byte budget across ALL streams '
             '(QuicConfiguration.tx_max_queued_bytes): producer parks '
             'at stream rollover while total un-transmitted TX bytes '
             'exceed this. Steady-state latency ~ value / throughput. '
             'Default: aiopquic default (4 MiB). Pass 0 to disable.')
    _cli.add_logging(parser)
    _cli.add_help(parser)
    _cli.add_timestamps(parser)
    _cli.add_timestamp_checking(parser)
    _cli.add_payload_check(parser)
    args = parser.parse_args()
    if args.insecure is None:
        # The self-signed test cert cannot verify.
        args.insecure = args.cert is None
    args.cert = args.cert or CERT
    args.key = args.key or KEY
    if args.fanout and args.streams > 1:
        parser.error("--fanout: one subgroup per group; -P must be 1")
    if args.per_peer_props and not args.fanout:
        parser.error("--per-peer-props needs --fanout")
    if args.datagram and args.object_size > 1152:
        parser.error(f"--datagram: object_size {args.object_size} can "
                     f"never fit a DATAGRAM frame (max ~1152B payload "
                     f"at 1200B frame ceiling; frames cannot fragment)")
    return args


def print_banner(args):
    transport_label = "QUIC" if args.quic else "H3/WebTransport"
    # url implies port + transport; raw QUIC has no path, WT uses "/"
    host = f"[{args.bind}]" if ':' in args.bind else args.bind
    url = (f"moqt://{host}:{args.port}" if args.quic
           else f"https://{host}:{args.port}/")
    if args.rate > 0:
        per_stream = (args.rate / args.streams
                      if args.streams > 1 else args.rate)
        rate_s = f"{args.rate}/s total ({per_stream:.1f}/s per stream)"
    else:
        rate_s = "max"

    def row(label, value):
        print(f"  {label + ':':<14}{value}")

    print("─" * 56)
    print("  aiomoqt-bench loopback (no relay)")
    print("─" * 56)
    row("draft", args.draft)
    row("url", url)
    row("transport", transport_label)
    row("delivery", ("DATAGRAM" if args.datagram else "subgroup streams")
        + (", fanout lane" if args.fanout else "")
        + (", per-peer props" if args.per_peer_props else ""))
    row("cc algorithm", args.cc_algo)
    row("sub-groups", args.streams)
    row("group size", f"{args.group_size} objects")
    if args.verify_payload:
        row("payload", f"checked (every {args.verify_payload})")
    row("object size", f"{args.object_size} B")
    row("object rate", rate_s)
    row("duration", f"{args.duration}s")
    print("─" * 56)


class _FanoutTrack(PublishedTrack):
    """The default generator's objects, written once through produce()
    so each takes its subscriber's send lane. Unpaced, it pauses once a
    lane holds more than _LANE_HIGH objects, until it is down to
    _LANE_LOW. `per_peer_props` builds properties per subscriber."""

    _LANE_HIGH = 16
    _LANE_LOW = 8
    per_peer_props = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mapping = (StreamMapping.DATAGRAM
                        if self.forwarding == ForwardingPreference.DATAGRAM
                        else StreamMapping.PER_GROUP)
        self._lane_moved = asyncio.Event()
        self._waiting = False

    def _on_sent(self, group_id, object_id):
        if self._waiting:
            self._lane_moved.set()

    def _stamp_per_peer(self, group_id, object_id):
        """Stamped now, at production: lane wait counts in latency."""
        loc = {LOC_TIMESTAMP: int(time.time() * 1_000_000)}
        if self.timestamps == 'loc':
            return loc
        full = self._timestamp_writer().stamp(group_id, object_id)
        return lambda s: full if s._profile.track_properties else loc

    async def produce(self, out) -> None:
        pad = self._payload_tail()
        await self._await_schedule(0, 0)
        # As the generator stamps: per object, for the first peer's draft.
        stamp = self._stamper(self._subs[0].session)
        per_peer = self.per_peer_props
        next_t = time.monotonic()
        tick = 0
        group_id = 0
        while True:
            for object_id in range(self.group_size):
                payload = (f"{group_id}.{object_id}".encode()
                           + pad)[:self.object_size]
                if per_peer:
                    extensions = self._stamp_per_peer(group_id, object_id)
                elif stamp is None:
                    extensions = {LOC_TIMESTAMP: int(time.time() * 1_000_000)}
                else:
                    extensions = stamp(group_id, object_id)
                await out.write(group_id, object_id, payload,
                                extensions=extensions,
                                group_start=object_id == 0)
                tick += 1
                if self.rate > 0:
                    next_t += 1.0 / self.rate
                    lead = next_t - time.monotonic()
                    if lead > 0.0005:
                        await asyncio.sleep(lead)
                        continue
                elif self._backlog(out) > self._LANE_HIGH:
                    await self._wait_lanes(out)
                    continue
                if tick % _PACED_YIELD_EVERY == 0:
                    await asyncio.sleep(0)
            group_id += 1

    @staticmethod
    def _backlog(out) -> int:
        return max((len(ln.items) for ln in out.lanes), default=0)

    async def _wait_lanes(self, out) -> None:
        self._waiting = True
        try:
            while self._backlog(out) > self._LANE_LOW:
                self._lane_moved.clear()
                # Timeout: a lane skipping to the next group drops objects
                # without calling _on_sent.
                try:
                    await asyncio.wait_for(self._lane_moved.wait(), 0.01)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._waiting = False


async def _on_subscribe(session, msg, args):
    """Server-side subscribe handler using PublishedTrack."""
    track_class = _FanoutTrack if args.fanout else PublishedTrack
    track = track_class(
        session,
        namespace="aiomoqt",
        trackname="track",
        object_size=args.object_size,
        group_size=args.group_size,
        num_subgroups=args.streams,
        rate=args.rate,
        forwarding=(ForwardingPreference.DATAGRAM if args.datagram
                    else ForwardingPreference.SUBGROUP),
        timestamps=args.timestamps,
    )
    if args.fanout:
        track.per_peer_props = args.per_peer_props
    # Suppress publisher periodic stats in loopback mode —
    # both sides print to the same terminal, causing interleaved output
    track._stats_header_printed = True  # skip header
    track._quiet = True  # checked in _generate_subgroup
    # d14 direct connection: respond with subscribe_ok and generate
    ok = session.subscribe_ok(
        request_msg=msg, track_properties=track._track_properties(session))
    track.track_alias = ok.track_alias
    track._generating = True
    await track.generate(session, ok.track_alias)


async def run_server(args):
    """Run a MOQTServer that generates data when subscribers connect."""
    from functools import partial

    server = MOQTServer(
        host=args.bind, port=args.port,
        certificate=args.cert, private_key=args.key,
        path="/",
        use_quic=args.quic,
        supported_drafts=args.draft,
        congestion_control_algorithm=args.cc_algo,
        # None = honor protocol default (16 MB); 0 = opt out.
        **({'tx_max_inflight_bytes':
            (None if args.max_inflight_bytes == 0
             else args.max_inflight_bytes)}
           if args.max_inflight_bytes is not None else {}),
        **({'tx_max_queued_bytes': args.max_queued_bytes}
           if args.max_queued_bytes is not None else {}),
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE,
        partial(_on_subscribe, args=args))
    return await server.serve()


async def run_subscriber(args, stats):
    """Connect as subscriber and collect stats."""
    client = MOQTClient(
        args.bind, args.port,
        path="/",
        use_quic=args.quic,
        supported_drafts=args.draft,
        verify_tls=not args.insecure,
        ca_file=None if args.insecure else args.cert,
        debug=args.debug,
        congestion_control_algorithm=args.cc_algo,
    )

    try:
        async with client.connect() as session:
            await session.client_session_init()

            track = SubscribedTrack(
                session,
                namespace="aiomoqt",
                trackname="track",
                on_object=stats.on_object,
                timestamp_errors=args.timestamp_errors,
            )
            stats.stats.timing = track.timing
            # Loopback server does not send PUBLISH; use direct SUBSCRIBE.
            # Loopback passes explicit trackname → auto-routes to direct.
            await track.subscribe()

            print("  Subscriber connected, receiving...\n")
            stats.start()

            if not await wait_cond_timeout(
                    track.wait_closed(), timeout=args.duration):
                track.completed = True
            stats.finish()
    except Exception as e:
        print(f"  Subscriber error: {e}")


async def main():
    args = parse_args()
    _cli.apply_logging(args)

    # AIOMOQT_TASK_DUMP=1 installs SIGUSR1 (task stacks) + SIGUSR2
    # (aiopquic counters) handlers. No-op when env not set.
    from aiomoqt.utils.taskdump import install as _install_task_dump
    _install_task_dump()

    # AIOMOQT_TRACEMALLOC=1 enables Python-level allocation tracking.
    # Baseline snap is taken 2s after subscriber connects; end snap is
    # taken right before cleanup. The diff (end - baseline) localizes
    # what GREW during steady-state operation — the sub-side retention
    # signature is exactly this case.
    import os as _os
    import tracemalloc as _tm
    _trace_enabled = _os.environ.get("AIOMOQT_TRACEMALLOC") == "1"
    if _trace_enabled:
        _tm.start(25)

    stats = BenchReporter(report_interval=args.interval,
                          minimal=args.no_stats,
                          verify_payload=args.verify_payload)
    print_banner(args)

    if not args.cert and not args.key:
        args.cert, args.key = ensure_self_signed()
    if not args.cert or not args.key:
        print("  Error: TLS certificate required. "
              "Use --cert and --key,")
        print("  or place cert.pem/key.pem in <project>/certs/")
        return 1

    print("  Starting server...")
    quic_server = await run_server(args)

    # Give server a moment
    await asyncio.sleep(0.5)

    # Baseline tracemalloc snap 2 s after subscriber connects.
    _baseline_snap = [None]
    if _trace_enabled:
        async def _take_baseline():
            await asyncio.sleep(2.0)
            _baseline_snap[0] = _tm.take_snapshot()
        asyncio.create_task(_take_baseline())

    # Run subscriber
    print("  Connecting subscriber...")
    await run_subscriber(args, stats)

    # End tracemalloc snap before cleanup; diff against baseline.
    if _trace_enabled and _baseline_snap[0] is not None:
        end_snap = _tm.take_snapshot()
        diff = end_snap.compare_to(_baseline_snap[0], 'lineno')
        print()
        print("=" * 70)
        print("  tracemalloc: top 30 growers (baseline @ +2s → end)")
        print("=" * 70)
        for s in diff[:30]:
            frame = s.traceback[0] if s.traceback else None
            loc = (f"{frame.filename.split('/')[-1]}:{frame.lineno}"
                   if frame else "<no frame>")
            sign = "+" if s.size_diff >= 0 else ""
            print(f"  {sign}{s.size_diff/1024/1024:7.2f} MB  "
                  f"{sign}{s.count_diff:7d} blocks  {loc}")
        print("=" * 70)

    # Cleanup
    quic_server.close()
    # Give worker thread + close walker a tick to run before we sample.
    await asyncio.sleep(0.5)
    stats.print_summary()
    received = stats.stats.total_objects

    # AIOMOQT_TASK_DUMP=1 also enables a final post-shutdown counter dump
    # so we can tell whether the close-walker swept leaked WT links
    # (deferred-destroy pattern) or they truly leak.
    import os as _os2
    if _os2.environ.get("AIOMOQT_TASK_DUMP") == "1":
        import sys as _sys2
        try:
            from aiopquic._binding._transport import dump_all_counters
            print("\n=== aiopquic counter-dump (post-shutdown) ===",
                  file=_sys2.stderr)
            dump_all_counters(file=_sys2.stderr)
        except Exception as _e2:
            print(f"(post-shutdown counter dump failed: {_e2})",
                  file=_sys2.stderr)
    return 0 if received and not stats.payload_bad else 1


def cli():
    """Console entry point (moq-loopback-bench)."""
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    cli()
