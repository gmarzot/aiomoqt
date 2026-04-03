#!/usr/bin/env python3
"""moqperf subscriber - receives MoQT objects and reports stats.

Usage:
  # H3/WebTransport (default)
  python -m aiomoqt.examples.bench_sub https://relay.example.com:4433/moq

  # Raw QUIC
  python -m aiomoqt.examples.bench_sub moqt://relay.example.com:4443

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
from aiomoqt.messages import (
    ObjectHeader, ObjectDatagram,
)
from aiomoqt.utils.logger import set_log_level, get_logger
from aiomoqt.utils.url import parse_relay_url


class BenchStats:
    """Collects and reports benchmark statistics."""

    def __init__(self, report_interval: float = 5.0):
        self.report_interval = report_interval
        self.start_time: float = 0
        self.last_report_time: float = 0

        # Per-interval (reset each report)
        self.iv_objects: int = 0
        self.iv_bytes: int = 0
        self.iv_latencies: list = []

        # Cumulative
        self.total_objects: int = 0
        self.total_bytes: int = 0
        self.all_latencies: list = []

        # Loss tracking per group/subgroup
        self.expected_seq: dict = {}
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
            self.iv_latencies.append(latency)
            self.all_latencies.append(latency)

            # RFC 3550 jitter
            if self.last_recv_ms > 0:
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

            expected = self.expected_seq.get(key, 0)
            if oid > expected:
                self.total_lost += (oid - expected)
            elif oid < expected:
                self.total_ooo += 1
            self.expected_seq[key] = oid + 1

        self.iv_objects += 1
        self.iv_bytes += size_bytes
        self.total_objects += 1
        self.total_bytes += size_bytes

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
            f"{'Interval':>10}  {'Obj':>7}  "
            f"{'Rate':>8}  {'Thput':>9}  "
            f"{'Lat avg':>8}  {'p99':>6}  "
            f"{'Jitter':>7}  {'Lost':>5}"
        )
        print("─" * 76)

    def _print_interval(self, now: float):
        dt = now - self.last_report_time
        if dt <= 0:
            return

        elapsed = now - self.start_time
        rate = self.iv_objects / dt
        mbps = (self.iv_bytes * 8) / (dt * 1e6)

        lat = self.iv_latencies
        if lat:
            avg = sum(lat) / len(lat)
            p99 = self._pct(lat, 99)
            lat_s = f"{avg:6.1f}ms"
            p99_s = f"{p99:.1f}"
        else:
            lat_s = f"{'--':>8}"
            p99_s = "--"

        iv = f"{elapsed - dt:.0f}-{elapsed:.0f}s"
        print(
            f"{iv:>10}  {self.iv_objects:>7}  "
            f"{rate:>6.1f}/s  {mbps:>7.2f}Mb  "
            f"{lat_s:>8}  {p99_s:>6}  "
            f"{self.jitter:>5.1f}ms  {self.total_lost:>5}"
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

        rate = self.total_objects / dur
        mbps = (self.total_bytes * 8) / (dur * 1e6)
        lat = self.all_latencies
        total_expected = self.total_objects + self.total_lost
        loss_pct = (
            self.total_lost / total_expected * 100
            if total_expected > 0 else 0
        )

        print()
        print("═" * 56)
        print(f"  moqperf results  ({dur:.1f}s)")
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
        description='moqperf subscriber - MoQT benchmark receiver',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
relay URL forms:
  moqt://host:port            raw QUIC (port default 4443)
  https://host:port/endpoint  H3/WT (port default 4433)
  host:port                   H3/WT
  host                        H3/WT, port 4433, endpoint /moq

examples:
  %(prog)s relay.example.com
  %(prog)s relay.example.com -t 60 -i 10
  %(prog)s moqt://relay.example.com:4443
""")
    parser.add_argument(
        'relay', type=str,
        help='Relay URL: moqt://host:port, '
             'https://host:port/ep, or host[:port]')
    parser.add_argument(
        '-Q', '--force-quic', action='store_true',
        help='Force raw QUIC even for https:// URLs')
    import time
    _ts = hex(int(time.time()) // 60 & 0xFFF)[2:]
    parser.add_argument(
        '-n', '--namespace', type=str, default=f'bench/{_ts}',
        help='MoQT namespace (default: bench/<time>)')
    parser.add_argument(
        '--trackname', type=str, default='track',
        help='MoQT track name (default: track)')
    parser.add_argument(
        '-t', '--duration', type=int, default=30,
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
    return parser.parse_args()


def print_banner(relay, args):
    print("─" * 56)
    print("  moqperf subscriber")
    print("─" * 56)
    print(f"  relay:       {relay}")
    print(f"  transport:   {relay.transport_name}")
    print(f"  namespace:   {args.namespace}/{args.trackname}")
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
        debug=args.debug,
        keylog_filename=args.keylogfile,
    )

    try:
        print("  Connecting...")
        async with client.connect() as session:
            session.on_object_received = stats.on_object

            try:
                await session.client_session_init()

                await session.subscribe(
                    namespace=args.namespace,
                    track_name=args.trackname,
                    parameters={
                        ParamType.MAX_CACHE_DURATION: 100,
                        ParamType.AUTH_TOKEN: b"bench-token",
                        ParamType.DELIVERY_TIMEOUT: 10,
                    },
                    wait_response=True,
                )

                print("  Subscribed, receiving...\n")

                try:
                    await asyncio.wait_for(
                        session.async_closed(),
                        timeout=args.duration,
                    )
                except asyncio.TimeoutError:
                    pass

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
        args = parse_args()
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
