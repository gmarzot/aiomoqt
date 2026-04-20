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
import time

from aiomoqt.types import (
    ParamType, MOQTException, MOQTRequestError,
    MOQT_TIMESTAMP_EXT, ObjectStatus,
)
from aiomoqt.client import MOQTClient
from aiomoqt.messages import ObjectDatagram
from aiomoqt.track import SubscribedTrack
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

        # Per-interval (reset each report)
        self.iv_objects: int = 0
        self.iv_bytes: int = 0
        self.iv_latencies: list = []

        # Cumulative
        self.total_objects: int = 0
        self.total_bytes: int = 0
        self.total_groups: set = set()  # unique group_ids seen
        self.all_latencies: list = []

        # Loss tracking per group/subgroup
        self.expected_seq: dict = {}   # key → next expected object_id
        self.stride: dict = {}         # key → object_id stride (auto-detected)
        self.total_lost: int = 0
        self.total_ooo: int = 0

        # RFC 3550 interarrival jitter
        self.last_recv_ms: float = 0
        self.last_send_ms: float = 0
        self.jitter: float = 0.0
        self.jitter_samples: list = []

    def on_object(self, msg, size_bytes: int, recv_time_ms: int,
                  group_id: int = None, subgroup_id: int = None):
        """Callback for each received data object."""
        if self.start_time == 0:
            self.start_time = time.monotonic()
            self.last_report_time = self.start_time
            self._print_header()

        now = time.monotonic()

        # Latency from timestamp extension
        send_ms = (msg.extensions.get(MOQT_TIMESTAMP_EXT)
                   if msg.extensions else None)
        if send_ms is not None:
            latency = recv_time_ms - send_ms
            # Reject corrupt timestamps (>60s latency = parse error)
            if abs(latency) > 60000:
                logger.warning(f"BenchStats: corrupt timestamp: "
                               f"send={send_ms} recv={recv_time_ms}")
                send_ms = None

        if send_ms is not None:
            self.iv_latencies.append(latency)
            self.all_latencies.append(latency)

            # RFC 3550 jitter
            if self.last_recv_ms > 0 and self.last_send_ms is not None:
                d = abs((recv_time_ms - self.last_recv_ms)
                        - (send_ms - self.last_send_ms))
                self.jitter += (d - self.jitter) / 16.0
                self.jitter_samples.append(self.jitter)
            self.last_recv_ms = recv_time_ms
            self.last_send_ms = send_ms

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
        lat = self.all_latencies
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

        if lat:
            avg = sum(lat) / len(lat)
            var = sum((x - avg) ** 2 for x in lat) / len(lat)
            sd = math.sqrt(var)
            print(
                f"  Latency:     min={min(lat):.1f}  "
                f"avg={avg:.1f}  max={max(lat):.1f}  "
                f"sd={sd:.1f} ms"
            )
            print(
                f"               p50={self._pct(lat, 50):.1f}  "
                f"p95={self._pct(lat, 95):.1f}  "
                f"p99={self._pct(lat, 99):.1f} ms"
            )

        if self.jitter_samples:
            javg = (sum(self.jitter_samples)
                    / len(self.jitter_samples))
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
  https://host:port/endpoint  H3/WT (port default 443)
  host:port                   H3/WT
  host                        H3/WT, port 443, endpoint /moq

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
        '-Q', '--force-quic', action='store_true',
        help='Force raw QUIC even for https:// URLs')
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

    relay = parse_relay_url(
        args.relay, force_quic=args.force_quic)
    stats = BenchStats(report_interval=args.interval)
    print_banner(relay, args)

    client = MOQTClient(
        relay.host, relay.port,
        endpoint=relay.endpoint,
        use_quic=relay.use_quic,
        verify_tls=not args.insecure,
        draft_version=args.draft,
        debug=args.debug,
        keylog_filename=args.keylogfile,
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

                await track.wait_closed(timeout=args.duration)

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


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
