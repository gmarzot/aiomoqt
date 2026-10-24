#!/usr/bin/env python3
"""aiomoqt-bench subscriber - receives MoQT objects and reports stats.

Usage:
  # H3/WebTransport (default)
  moq-sub-bench https://relay.example.com/moq

  # Raw QUIC
  moq-sub-bench moqt://relay.example.com

  # Bare hostname
  moq-sub-bench relay.example.com -t 60 -i 10
"""
import asyncio
import contextlib
import logging
import os
import sys
import time
import tracemalloc

# Opt-in memory profiling: AIOMOQT_TRACEMALLOC=1 enables tracemalloc and
# dumps top allocators at exit. Default off — zero perf impact when unset.
if os.environ.get("AIOMOQT_TRACEMALLOC") == "1":
    tracemalloc.start(25)  # 25-frame stack capture per allocation

from aiomoqt.types import (
    MOQTException, MOQTRequestError,
)
from aiomoqt.client import MOQTClient
from aiomoqt.track import SubscribedTrack
from aiomoqt.utils import wait_cond_timeout
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.stats import TrackStats
from aiomoqt.utils.logger import set_log_level, get_logger
from aiomoqt.utils.url import parse_relay_url


logger = get_logger(__name__)


class BenchReporter:
    """Console view over the shared TrackStats accounting.

    TrackStats owns the numbers (so every tool reports identically);
    this owns only the table — printing an interval row whenever
    report_interval elapses, and the end-of-run summary.
    """

    def __init__(self, report_interval: float = 5.0,
                 minimal: bool = False):
        self.stats = TrackStats(windowed=False, minimal=minimal,
                                group_loss=True)
        self.report_interval = report_interval
        self._last_report = 0.0
        self._header_printed = False

    def start(self):
        """Print the table header. Call once the tool has finished its
        own setup output — the header used to print lazily on the first
        object, which raced whatever the caller printed around
        subscribe() and produced out-of-order banners."""
        if not self._header_printed:
            print(TrackStats.header())
            print("  " + "─" * 101)
            self._header_printed = True
        self._last_report = time.monotonic()

    def on_object(self, msg, size_bytes: int, recv_time_us: int,
                  group_id: int = None, subgroup_id: int = None):
        self.stats.on_object(msg, size_bytes, recv_time_us,
                             group_id, subgroup_id)
        now = time.monotonic()
        if not self._header_printed:
            self.start()
            return
        if now - self._last_report >= self.report_interval:
            print(self.stats.interval_row(now))
            self._last_report = now

    def print_summary(self):
        s = self.stats.summary()
        if not s:
            print("\n  No data received.")
            return
        print()
        print("═" * 56)
        print(f"  aiomoqt-bench results  ({s['active_s']:.1f}s active "
              f"/ {s['duration_s']:.1f}s elapsed)")
        print("═" * 56)
        blind = s.get('datagram') or s.get('minimal')
        grps = 'n/a' if blind else f"{s['groups']:,}"
        print(f"  Groups:      {grps}")
        print(f"  Objects:     {s['objects']:,}")
        print(f"  Bytes:       {s['bytes']:,}")
        grp_rate = 'n/a' if blind else f"{s['grp_rate']:.1f} grp/s"
        print(f"  GrpRate:     {grp_rate}")
        print(f"  ObjRate:     {s['obj_rate']:.1f} obj/s")
        print(f"  Throughput:  {s['mbps']:.2f} Mbps")
        if s['lat_mean']:
            mark = "  (clock offset)" if s.get('clock_offset') else ""
            print(f"  Latency:     min={s['lat_min']:.1f}  "
                  f"avg={s['lat_mean']:.1f}  max={s['lat_max']:.1f}  "
                  f"sd={s['lat_sd']:.1f} ms{mark}")
            print(f"               p50={s['lat_p50']:.1f}  "
                  f"p95={s['lat_p95']:.1f}  p99={s['lat_p99']:.1f} ms")
        if s.get('ts_source') or s.get('ts_malformed'):
            bad = (f"  ({s['ts_malformed']} malformed)"
                   if s.get('ts_malformed') else '')
            print(f"  Timestamps:  {s.get('ts_source') or 'none'}{bad}")
        if s.get('minimal'):
            print("  Jitter:      n/a")
            print("  Lost:        n/a  (--no-stats: counters only)")
        else:
            print(f"  Jitter:      {s['jitter_ms']:.2f} ms")
            print(f"  Lost:        {s['lost']} ({s['loss_pct']:.2f}%)")
            print(f"  Out-of-order:   {s['ooo']}")
        print("═" * 56)


def parse_args():
    parser = _cli.make_parser(
        'aiomoqt subscriber bench — MoQT benchmark receiver',
        epilog="""
The URL scheme selects the transport:
  moqt://host[:port][/path]   raw QUIC
  https://host[:port][/path]  H3/WebTransport
  host[:port]                 H3/WebTransport

examples:
  moq-sub-bench moqt://relay.example.com:4433
  moq-sub-bench https://relay.example.com/moq -t 60 -i 10
""")
    _cli.add_endpoint(parser)
    _cli.add_identity(parser)
    _cli.add_run(parser, duration=0)   # 0 = run until publisher closes
    _cli.add_interval(parser)
    _cli.add_session(parser)
    _cli.add_keepalive(parser)
    _cli.add_compat(parser)
    _cli.add_timestamp_checking(parser)
    parser.add_argument(
        '--auth-token', type=str, default=None,
        help='Send this token as AUTH_TOKEN parameter on SUBSCRIBE '
             '(required by some relays)')
    _cli.add_help(parser)
    return parser.parse_args()


def print_banner(relay, args):
    print("─" * 56)
    print("  aiomoqt-bench subscriber")
    print("─" * 56)
    print(f"  relay:       {relay}")
    print(f"  transport:   {relay.transport_name}")
    print(f"  namespace:   {args.namespace}")
    print(f"  trackname:   {args.trackname or '(auto-discover)'}")
    print(f"  duration:    {args.duration}s")
    print(f"  interval:    {args.interval}s")
    print("─" * 56)


async def run(args):
    log_level = (logging.DEBUG if args.debug
                 else logging.WARNING)
    set_log_level(log_level)

    # AIOMOQT_TASK_DUMP=1 installs a SIGUSR1 handler that dumps every
    # asyncio task's stack to stderr. Useful for diagnosing hangs:
    # `kill -USR1 <pid>` while the bench is stuck. No-op when unset.
    from aiomoqt.utils.taskdump import install as _install_task_dump
    _install_task_dump()

    relay = parse_relay_url(
        args.url)
    stats = BenchReporter(report_interval=args.interval,
                          minimal=args.no_stats)
    print_banner(relay, args)

    client = MOQTClient(
        relay.host, relay.port,
        path=relay.path,
        use_quic=relay.use_quic,
        verify_tls=not args.insecure,
        supported_drafts=args.draft,
        debug=args.debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=args.cc_algo,
        keep_alive_interval=args.keepalive,
    )

    failed = True
    try:
        print("  Connecting...")
        async with contextlib.AsyncExitStack() as stack:
            # -t bounds connecting as well as receiving.
            try:
                async with asyncio.timeout(args.duration or None):
                    session = await stack.enter_async_context(
                        client.connect())
            except TimeoutError:
                raise ConnectionError(f"no session within {args.duration}s")
            try:
                async with asyncio.timeout(args.duration or None):
                    await session.client_session_init()
                    track = SubscribedTrack(
                        session,
                        namespace=args.namespace,
                        trackname=args.trackname,
                        on_object=stats.on_object,
                        auth_token=(args.auth_token.encode()
                                    if args.auth_token else None),
                        timestamp_errors=args.timestamp_errors,
                    )
                    stats.stats.timing = track.timing
                    await track.subscribe()
                print(f"  Subscribed to '{track.fqtn}', receiving...\n")
                stats.start()

                if not await wait_cond_timeout(
                        track.wait_closed(), timeout=args.duration):
                    track.completed = True
                failed = False

            except MOQTRequestError as e:
                print(f"  Request error: {e}")
                session.close()
            except MOQTException as e:
                print(f"  MoQT error: {e}")
                session.close(
                    e.error_code, e.reason_phrase)
            except TimeoutError:
                print(f"  Error: no subscription within {args.duration}s")
            except Exception as e:
                print(f"  Error: {e}")
    except Exception as e:
        print(f"  Connection failed: {e}")

    stats.print_summary()

    if tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot()
        top = snap.statistics("filename")
        print("\n=== tracemalloc top 25 by filename ===")
        for stat in top[:25]:
            print(f"  {stat.size / (1024 * 1024):8.1f} MB  "
                  f"{stat.count:>8d} blocks  {stat.traceback}")
    # Zero objects is a failed run.
    return 1 if failed or not stats.stats.total_objects else 0


def cli():
    """Console entry point (moq-sub-bench)."""
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\n  Interrupted.")


if __name__ == "__main__":
    cli()
