#!/usr/bin/env python3
"""aiomoqt-bench subscriber - receives MoQT objects and reports stats.

Usage:
  # H3/WebTransport (default)
  python -m aiomoqt.examples.bench_sub https://relay.example.com/moq

  # Raw QUIC
  python -m aiomoqt.examples.bench_sub moqt://relay.example.com

  # Bare hostname
  python -m aiomoqt.examples.bench_sub relay.example.com -t 60 -i 10
"""
import argparse
import asyncio
import logging
import math
import os
import random
import time
import tracemalloc

# Opt-in memory profiling: AIOMOQT_TRACEMALLOC=1 enables tracemalloc and
# dumps top allocators at exit. Default off — zero perf impact when unset.
if os.environ.get("AIOMOQT_TRACEMALLOC") == "1":
    tracemalloc.start(25)  # 25-frame stack capture per allocation

from aiomoqt.types import (
    ParamType, MOQTException, MOQTRequestError,
    MOQT_TIMESTAMP_EXT, ObjectStatus,
)
from aiomoqt.client import MOQTClient
from aiomoqt.messages import ObjectDatagram
from aiomoqt.track import SubscribedTrack
from aiomoqt.utils import wait_cond_timeout
from aiomoqt.utils.format import fmt_bps, fmt_ms, fmt_rate
from aiomoqt.utils.logger import set_log_level, get_logger
from aiomoqt.utils.url import parse_relay_url


logger = get_logger(__name__)


class BenchStats:
    """Collects and reports benchmark statistics."""

    def __init__(self, report_interval: float = 5.0):
        self.report_interval = report_interval
        self.start_time: float = 0
        self.last_report_time: float = 0
        self.first_object_time: float = 0
        self.last_object_time: float = 0

        # Per-interval (reset each report). Latency list is bounded by
        # interval × rate, not by run length, so it stays small for any
        # reasonable -i.
        self.iv_objects: int = 0
        self.iv_bytes: int = 0
        # Interval latency RESERVOIR (Algorithm R, same as the
        # cumulative lat_reservoir below). Capped because the interval
        # report runs INSIDE on_object on the receive loop: sorting a
        # full 5 s window (500K+ floats at 100K+ obj/s) blocks the
        # drain ~100-170 ms, queueing arrivals in sc->rx and poisoning
        # the NEXT interval's latencies with the bench's own stall —
        # visible only in 2-process runs (in-process the producer
        # co-stalls, so no queue forms). 16K samples keep the sort
        # ~1.5 ms with negligible percentile error.
        self.iv_latencies: list = []
        self.iv_lat_count: int = 0
        self.iv_lat_max: int = 16384

        # Cumulative
        self.total_objects: int = 0
        self.total_bytes: int = 0
        self.total_groups: set = set()  # unique group_ids seen

        # Cumulative latency stats — constant-memory replacements for
        # the previous all_latencies list. min/max/avg/sd computed from
        # running sums; percentiles estimated from a bounded reservoir
        # sample (Algorithm R), which gives unbiased estimates of any
        # quantile from a stream of unknown total length.
        self.lat_count: int = 0
        self.lat_sum: float = 0.0
        self.lat_sum_sq: float = 0.0
        self.lat_min: float = float('inf')
        self.lat_max: float = float('-inf')
        self.lat_reservoir: list = []
        self.lat_reservoir_max: int = 10000  # ~250 KB at 8 B / float

        # Loss tracking per group/subgroup
        self.expected_seq: dict = {}   # key → next expected object_id
        self.stride: dict = {}         # key → object_id stride (auto-detected)
        self.total_lost: int = 0
        self.total_ooo: int = 0

        # RFC 3550 interarrival jitter — running sum + count (constant
        # memory) instead of full-history list.
        self.last_recv_ms: float = 0
        self.last_send_ms: float = 0
        self.jitter: float = 0.0
        self.jitter_sum: float = 0.0
        self.jitter_count: int = 0

    def on_object(self, msg, size_bytes: int, recv_time_us: int,
                  group_id: int = None, subgroup_id: int = None):
        """Callback for each received data object. Timestamps are
        microseconds since epoch on the wire; latency stats are
        float ms (sub-millisecond resolution preserved)."""
        if self.start_time == 0:
            self.start_time = time.monotonic()
            self.last_report_time = self.start_time
            self._print_header()

        now = time.monotonic()

        # Latency from timestamp extension (microseconds → float ms)
        send_us = (msg.extensions.get(MOQT_TIMESTAMP_EXT)
                   if msg.extensions else None)
        latency = None
        if send_us is not None:
            raw_us = recv_time_us - send_us
            # Reject corrupt timestamps. Real under-load latency can
            # legitimately exceed 60s; cap at 10 minutes (in us).
            if -1_000_000 <= raw_us <= 600_000_000:
                latency = raw_us / 1000.0  # float ms
            else:
                logger.warning(f"BenchStats: corrupt timestamp: "
                               f"send={send_us} recv={recv_time_us}")
                send_us = None

        if latency is not None:
            if len(self.iv_latencies) < self.iv_lat_max:
                self.iv_latencies.append(latency)
            else:
                j = random.randint(0, self.iv_lat_count)
                if j < self.iv_lat_max:
                    self.iv_latencies[j] = latency
            self.iv_lat_count += 1

            # Update cumulative latency running stats + reservoir.
            self.lat_sum += latency
            self.lat_sum_sq += latency * latency
            if latency < self.lat_min:
                self.lat_min = latency
            if latency > self.lat_max:
                self.lat_max = latency
            if len(self.lat_reservoir) < self.lat_reservoir_max:
                self.lat_reservoir.append(latency)
            else:
                # Algorithm R: replace index k with probability
                # reservoir_max / count_seen.
                j = random.randint(0, self.lat_count)
                if j < self.lat_reservoir_max:
                    self.lat_reservoir[j] = latency
            self.lat_count += 1

            # RFC 3550 jitter (us domain → ms result)
            if self.last_recv_ms > 0 and self.last_send_ms is not None:
                d = abs((recv_time_us - self.last_recv_ms)
                        - (send_us - self.last_send_ms)) / 1000.0
                self.jitter += (d - self.jitter) / 16.0
                self.jitter_sum += self.jitter
                self.jitter_count += 1
            self.last_recv_ms = recv_time_us
            self.last_send_ms = send_us

        # Loss detection (skip status messages)
        is_status = (hasattr(msg, 'status')
                     and msg.status != ObjectStatus.NORMAL)
        if not is_status:
            gid = getattr(msg, 'group_id', group_id)
            oid = msg.object_id
            if isinstance(msg, ObjectDatagram):
                key = f"dg_{gid}"
            else:
                sid = subgroup_id if subgroup_id is not None else 0
                key = f"sg_{gid}_{sid}"

            expected = self.expected_seq.get(key)
            if expected is None:
                # First object on this subgroup — record start
                self.expected_seq[key] = oid
            else:
                # Detect stride from second object, then use it
                st = self.stride.get(key)
                if st is None and oid > expected:
                    st = oid - expected
                    self.stride[key] = st
                st = st or 1
                if oid > expected:
                    lost = (oid - expected) // st - 1
                    if lost > 0:
                        self.total_lost += lost
                elif oid < expected:
                    self.total_ooo += 1
            self.expected_seq[key] = oid + (self.stride.get(key) or 1)

        self.iv_objects += 1
        self.iv_bytes += size_bytes
        self.total_objects += 1
        self.total_bytes += size_bytes
        if self.first_object_time == 0:
            self.first_object_time = now
        self.last_object_time = now
        gid = getattr(msg, 'group_id', group_id)
        if gid is not None:
            self.total_groups.add(gid)

        if now - self.last_report_time >= self.report_interval:
            self._print_interval(now)
            self.last_report_time = now

    @staticmethod
    def _pct(data: list, p: float) -> float:
        if not data:
            return 0.0
        s = sorted(data)
        return s[min(int(len(s) * p / 100), len(s) - 1)]

    def _print_header(self):
        print(
            f"  {'Interval':<10}{'Grps':<8}{'Objs':<10}"
            f"{'ObjRate':<10}{'Bitrate':<10}"
            f"{'Latency':<20}"
            f"{'Jitter':<8}{'Loss':<17}"
        )
        print("  " + "─" * 93)

    def _print_interval(self, now: float):
        dt = now - self.last_report_time
        if dt <= 0:
            return

        elapsed = now - self.start_time
        rate = self.iv_objects / dt
        bps = (self.iv_bytes * 8) / dt

        lat = self.iv_latencies
        if lat:
            avg = sum(lat) / len(lat)
            p99 = self._pct(lat, 99)
            lat_s = f"{fmt_ms(avg)} p99: {fmt_ms(p99)}"
        else:
            lat_s = "--"

        iv = f"{elapsed - dt:.0f}-{elapsed:.0f}s"
        grps = len(self.total_groups)
        rate_s = fmt_rate(rate)
        bps_s = fmt_bps(bps)
        jitter_s = fmt_ms(self.jitter)
        expected = self.total_objects + self.total_lost
        loss_pct = int(round(
            100 * self.total_lost / expected)) if expected else 0
        loss_s = f"{loss_pct}% ({self.total_lost} objs)"
        print(
            f"  {iv:<10}{grps:<8}{self.total_objects:<10}"
            f"{rate_s:<10}{bps_s:<10}"
            f"{lat_s:<20}"
            f"{jitter_s:<8}{loss_s:<17}"
        )

        self.iv_objects = 0
        self.iv_bytes = 0
        self.iv_latencies = []
        self.iv_lat_count = 0

    def print_summary(self):
        if self.start_time == 0:
            print("\n  No data received.")
            return

        dur = time.monotonic() - self.start_time
        if dur <= 0:
            return

        # Rate and throughput use the active window (first → last
        # object received) so post-pub idle time doesn't dilute them.
        active = (self.last_object_time - self.first_object_time
                  if self.first_object_time else 0)
        active = active if active > 0 else dur
        rate = self.total_objects / active
        mbps = (self.total_bytes * 8) / (active * 1e6)
        total_expected = self.total_objects + self.total_lost
        loss_pct = (
            self.total_lost / total_expected * 100
            if total_expected > 0 else 0
        )

        print()
        print("═" * 56)
        print(f"  aiomoqt-bench results  ({active:.1f}s active "
              f"/ {dur:.1f}s elapsed)")
        print("═" * 56)
        print(f"  Objects:     {self.total_objects:,}")
        print(f"  Bytes:       {self.total_bytes:,}")
        print(f"  Rate:        {rate:.1f} obj/s")
        print(f"  Throughput:  {mbps:.2f} Mbps")

        if self.lat_count > 0:
            avg = self.lat_sum / self.lat_count
            var = max(0.0, self.lat_sum_sq / self.lat_count - avg * avg)
            sd = math.sqrt(var)
            print(
                f"  Latency:     min={self.lat_min:.1f}  "
                f"avg={avg:.1f}  max={self.lat_max:.1f}  "
                f"sd={sd:.1f} ms"
            )
            # Percentiles from reservoir sample (Algorithm R). With a
            # 10K reservoir we get unbiased p50/p95/p99 estimates from
            # any-length stream at constant memory.
            sample = self.lat_reservoir
            print(
                f"               p50={self._pct(sample, 50):.1f}  "
                f"p95={self._pct(sample, 95):.1f}  "
                f"p99={self._pct(sample, 99):.1f} ms"
            )

        if self.jitter_count > 0:
            javg = self.jitter_sum / self.jitter_count
            print(
                f"  Jitter:      {self.jitter:.2f} ms (final)  "
                f"avg={javg:.2f} ms"
            )

        print(f"  Lost:        {self.total_lost} ({loss_pct:.2f}%)")
        print(f"  Out-of-order:{self.total_ooo:>4}")
        print("═" * 56)


def parse_args():
    parser = argparse.ArgumentParser(
        description='aiomoqt-bench subscriber - MoQT benchmark receiver',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
relay URL forms:
  moqt://host:port            raw QUIC (port default 443)
  https://host:port/path  H3/WT (port default 443)
  host:port                   H3/WT
  host                        H3/WT, port 443, no default path

examples:
  %(prog)s relay.example.com
  %(prog)s relay.example.com -t 60 -i 10
  %(prog)s moqt://relay.example.com
""")
    parser.add_argument(
        'relay', type=str,
        help='Relay URL: moqt://host:port, '
             'https://host:port/ep, or host[:port]')
    parser.add_argument(
        '-Q', '--quic', '--use-quic', action='store_true',
        dest='force_quic',
        help='Raw QUIC even for https:// URLs')
    parser.add_argument(
        '-n', '--namespace', type=str, default='aiomoqt',
        help='MoQT namespace (default: aiomoqt)')
    parser.add_argument(
        '--trackname', type=str, default=None,
        help='MoQT track name. Explicit → direct SUBSCRIBE. '
             'Omit → SUBSCRIBE_NAMESPACE auto-discovery.')
    parser.add_argument(
        '--auth-token', type=str, default=None,
        help='Send this token as AUTH_TOKEN parameter on SUBSCRIBE '
             '(required by some relays)')
    parser.add_argument(
        '-t', '--duration', type=int, default=0,
        help='Duration in seconds (default: 30)')
    parser.add_argument(
        '-i', '--interval', type=float, default=5.0,
        help='Report interval in seconds (default: 5)')
    parser.add_argument(
        '-d', '--debug', action='store_true')
    parser.add_argument(
        '--keylogfile', type=str, default=None)
    parser.add_argument(
        '-k', '--insecure', action='store_true',
        help='Skip TLS certificate verification')
    parser.add_argument(
        '--draft', type=int, default=None,
        help='MoQT draft version (e.g. 14, 16)')
    parser.add_argument(
        '--cc-algo', type=str, default=None,
        help='Congestion control algorithm '
             '(bbr | bbr1 | newreno | cubic | dcubic | prague | fast). '
             'Default: aiopquic default (bbr1)')
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
        args.relay, force_quic=args.force_quic)
    stats = BenchStats(report_interval=args.interval)
    print_banner(relay, args)

    client = MOQTClient(
        relay.host, relay.port,
        path=relay.path,
        use_quic=relay.use_quic,
        verify_tls=not args.insecure,
        draft_version=args.draft,
        debug=args.debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=args.cc_algo,
    )

    try:
        print("  Connecting...")
        async with client.connect() as session:
            try:
                await session.client_session_init()

                track = SubscribedTrack(
                    session,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    draft=args.draft,
                    on_object=stats.on_object,
                    auth_token=(args.auth_token.encode()
                                if args.auth_token else None),
                )
                await track.subscribe()
                print(f"  Subscribed to '{track.fqtn}', receiving...\n")

                if not await wait_cond_timeout(
                        track.wait_closed(), timeout=args.duration):
                    track.completed = True

            except MOQTRequestError as e:
                print(f"  Request error: {e}")
                session.close()
            except MOQTException as e:
                print(f"  MoQT error: {e}")
                session.close(
                    e.error_code, e.reason_phrase)
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


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
