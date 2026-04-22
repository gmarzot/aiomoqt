#!/usr/bin/env python3
"""aiomoqt adaptive bench v2 — single-process feedback-driven loopback.

Runs a publisher + subscriber pair in one Python process over qh3
loopback. The publisher's rate is live-mutated by a controller task
reading subscriber-side snapshots (p99 latency, loss, achieved
throughput) out of a shared BenchState. No subprocess-per-step loop
like v1 — one continuous steady-state publisher whose pacing the
controller nudges up or down per interval.

Scenarios pin some load axes (object size, number of parallel
subgroup streams P) and ramp the aggregate bitrate. With P > 1, each
subgroup gets aggregate_ops / P so the total offered load matches the
scenario's commanded Mbps regardless of parallelism.

Usage:
  python -m aiomoqt.examples.adaptive_bench --scenario single-high
  python -m aiomoqt.examples.adaptive_bench --scenario parallel-streams
  python -m aiomoqt.examples.adaptive_bench --scenario single-high \\
      --report /tmp/bench.csv --max-mbps 300
"""
import argparse
import asyncio
import csv
import os
import platform
import ssl
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

from qh3.quic.configuration import QuicConfiguration
from qh3.asyncio.server import serve
from qh3.h3.connection import H3_ALPN

from aiomoqt.types import MOQTMessageType, MOQT_TIMESTAMP_EXT, ObjectStatus
from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTPeer, MOQTSession
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.utils.logger import set_log_level


# ---------------------------------------------------------------------------
# Scenarios: pin some axes, ramp aggregate bitrate
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    object_size: int            # bytes per MoQT object
    subgroups: int              # parallel subgroup streams (P)
    start_mbps: float           # initial aggregate commanded bitrate
    step_mbps: float            # controller ramp step
    max_mbps: float             # cap
    interval_s: float           # controller tick period
    settle_s: float             # hold last stable before stopping
    description: str = ""

    def aggregate_ops(self, mbps: float) -> float:
        """Convert aggregate Mbps to aggregate objects/sec."""
        return (mbps * 1e6) / (self.object_size * 8)


SCENARIOS = {
    "single-high": Scenario(
        name="single-high",
        object_size=4096, subgroups=1,
        start_mbps=10.0, step_mbps=5.0, max_mbps=500.0,
        interval_s=5.0, settle_s=15.0,
        description="1 sub, 1 stream, 4KB objects; ramp bitrate",
    ),
    "parallel-streams": Scenario(
        name="parallel-streams",
        object_size=4096, subgroups=4,
        start_mbps=10.0, step_mbps=10.0, max_mbps=500.0,
        interval_s=5.0, settle_s=15.0,
        description="1 sub, 4 streams, 4KB objects; aggregate split P-ways",
    ),
    "small-fast": Scenario(
        name="small-fast",
        object_size=512, subgroups=1,
        start_mbps=1.0, step_mbps=1.0, max_mbps=100.0,
        interval_s=5.0, settle_s=15.0,
        description="tiny 512B objects; tests pacing/control limits",
    ),
    "large-objects": Scenario(
        name="large-objects",
        object_size=65536, subgroups=1,
        start_mbps=20.0, step_mbps=20.0, max_mbps=1000.0,
        interval_s=5.0, settle_s=15.0,
        description="1 sub, 1 stream, 64KB objects; stream CC stress",
    ),
}


# ---------------------------------------------------------------------------
# Snapshot / shared state
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """Subscriber-observed metrics over the current rolling window."""
    t: float
    commanded_mbps: float       # what the controller asked for
    rx_rate_ops: float          # objects/sec arriving at subscriber
    rx_bitrate_mbps: float      # equivalent bitrate
    p50_ms: float
    p99_ms: float
    loss_pct: float
    jitter_ms: float


@dataclass
class BenchState:
    """Shared state between loader, subscriber, and controller tasks.

    Single-writer rule per field:
      rate_ops       — writer: controller
      latest/history — writer: subscriber snapshot loop
      stop/ceiling_* — writer: main or controller
    """
    rate_ops: float = 0.0           # aggregate objects/sec commanded
    commanded_mbps: float = 0.0     # mirror of rate_ops in Mbps
    latest: Optional[Snapshot] = None
    history: deque = field(default_factory=lambda: deque(maxlen=64))
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    ceiling_reason: Optional[str] = None
    ceiling_mbps: Optional[float] = None


# ---------------------------------------------------------------------------
# Subscriber-side rolling-window stats
# ---------------------------------------------------------------------------

class LiveStats:
    """Rolling-window metrics collector. One instance per bench run."""

    def __init__(self, object_size: int, window_s: float = 5.0):
        self.object_size = object_size
        self.window_s = window_s
        # Each event: (monotonic_t, bytes, latency_ms_or_None)
        self._events: deque = deque()
        # Loss tracking per (group_id, subgroup_id). Auto-detect stride
        # so multi-subgroup streams (PublishedTrack strides object IDs
        # by num_subgroups per subgroup) aren't falsely reported as loss.
        self._last_seen: dict = {}
        self._stride: dict = {}
        self._lost = 0
        # Jitter (RFC 3550)
        self._last_recv_ms = 0
        self._last_send_ms = 0
        self._jitter = 0.0

    def on_object(self, msg, size_bytes, recv_time_ms,
                  group_id=None, subgroup_id=None):
        t = time.monotonic()

        # Skip status (non-NORMAL) objects
        is_status = (hasattr(msg, 'status')
                     and getattr(msg, 'status', ObjectStatus.NORMAL)
                     != ObjectStatus.NORMAL)

        send_ms = None
        if getattr(msg, 'extensions', None):
            send_ms = msg.extensions.get(MOQT_TIMESTAMP_EXT)
        latency = None
        if send_ms is not None:
            raw = recv_time_ms - send_ms
            if abs(raw) < 60000:
                latency = raw
                if self._last_recv_ms and self._last_send_ms:
                    d = abs((recv_time_ms - self._last_recv_ms)
                            - (send_ms - self._last_send_ms))
                    self._jitter += (d - self._jitter) / 16.0
                self._last_recv_ms = recv_time_ms
                self._last_send_ms = send_ms

        self._events.append((t, size_bytes, latency))
        cutoff = t - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

        if not is_status:
            gid = getattr(msg, 'group_id', group_id)
            sid = subgroup_id if subgroup_id is not None else 0
            oid = msg.object_id
            key = (gid, sid)
            last = self._last_seen.get(key)
            if last is None:
                # First object seen on this (group, subgroup)
                self._last_seen[key] = oid
            else:
                stride = self._stride.get(key)
                if stride is None and oid > last:
                    # Auto-detect stride from second object
                    stride = oid - last
                    self._stride[key] = stride
                if stride is not None and stride > 0 and oid > last + stride:
                    # Objects missing on this subgroup's stride
                    self._lost += (oid - last - stride) // stride
                if oid > last:
                    self._last_seen[key] = oid
                # else: reorder/duplicate within this subgroup — ignore

    def snapshot(self, commanded_mbps: float) -> Snapshot:
        t = time.monotonic()
        cutoff = t - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        if not self._events:
            return Snapshot(t=t, commanded_mbps=commanded_mbps,
                            rx_rate_ops=0.0, rx_bitrate_mbps=0.0,
                            p50_ms=0.0, p99_ms=0.0,
                            loss_pct=0.0, jitter_ms=self._jitter)

        window_t = max(0.01, self._events[-1][0] - self._events[0][0])
        n = len(self._events)
        total_bytes = sum(e[1] for e in self._events)
        lats = sorted(e[2] for e in self._events if e[2] is not None)
        p50 = lats[len(lats) // 2] if lats else 0.0
        p99 = lats[min(int(len(lats) * 0.99), len(lats) - 1)] if lats else 0.0

        total_expected = n + self._lost
        loss_pct = 100.0 * self._lost / total_expected if total_expected else 0.0

        return Snapshot(
            t=t,
            commanded_mbps=commanded_mbps,
            rx_rate_ops=n / window_t,
            rx_bitrate_mbps=(total_bytes * 8) / (window_t * 1e6),
            p50_ms=p50,
            p99_ms=p99,
            loss_pct=loss_pct,
            jitter_ms=self._jitter,
        )


# ---------------------------------------------------------------------------
# Controller: AIMD-ish policy
# ---------------------------------------------------------------------------

class AIMDController:
    """Simple additive-increase / multiplicative-decrease policy.

    - Every `interval_s`, snapshot metrics and decide.
    - Ramp: +step_mbps while loss==0 and p99 stays near baseline.
    - Back off: multiply aggregate by 0.9 on any ceiling signal.
    - Ceiling signals (any one trips it):
        * loss_pct > 0.5% in the last window
        * p99 > baseline * 1.5 for 2 consecutive intervals
        * achieved rx_bitrate < 0.85 * commanded for 2 consecutive intervals
    - After 2 consecutive back-offs from the same rate, lock in ceiling
      and settle for `settle_s`.
    """

    def __init__(self, scenario: Scenario, state: BenchState,
                 stats: LiveStats, writer=None,
                 p99_threshold_ms: Optional[float] = None,
                 backoff_factor: float = 0.9,
                 shortfall_ratio: float = 0.85,
                 loss_threshold_pct: float = 0.5):
        self.scenario = scenario
        self.state = state
        self.stats = stats
        self.writer = writer
        # Absolute SLA mode: reject once p99 exceeds this. If None, fall
        # back to the relative "p99 > baseline * 1.5 for 2 intervals" rule.
        self.p99_threshold_ms = p99_threshold_ms
        self.backoff_factor = backoff_factor
        self.shortfall_ratio = shortfall_ratio
        self.loss_threshold_pct = loss_threshold_pct
        self.baseline_p99 = None
        self.high_p99_streak = 0
        self.shortfall_streak = 0
        self.backoff_streak = 0
        self.last_stable_mbps = scenario.start_mbps
        self.samples = 0

    async def run(self):
        state = self.state
        sc = self.scenario

        # Prime the first commanded rate.
        self._set_rate(sc.start_mbps)

        while not state.stop.is_set():
            try:
                await asyncio.wait_for(state.stop.wait(),
                                       timeout=sc.interval_s)
                break
            except asyncio.TimeoutError:
                pass

            snap = self.stats.snapshot(commanded_mbps=state.commanded_mbps)
            state.latest = snap
            state.history.append(snap)
            self.samples += 1
            if self.writer:
                self.writer.writerow([
                    f"{snap.t:.3f}",
                    f"{snap.commanded_mbps:.2f}",
                    f"{snap.rx_bitrate_mbps:.2f}",
                    f"{snap.rx_rate_ops:.2f}",
                    f"{snap.p50_ms:.2f}",
                    f"{snap.p99_ms:.2f}",
                    f"{snap.loss_pct:.3f}",
                    f"{snap.jitter_ms:.3f}",
                ])

            # Wait a few samples to let pacing settle before deciding.
            if self.samples < 2:
                self._print_row(snap, "warming")
                continue

            # Establish baseline p99 from the first stable sample
            if self.baseline_p99 is None:
                self.baseline_p99 = snap.p99_ms
                self.last_stable_mbps = snap.commanded_mbps

            # Ceiling signal evaluation
            reason = None
            if snap.loss_pct > self.loss_threshold_pct:
                reason = f"loss={snap.loss_pct:.1f}%"

            # p99 ceiling: absolute SLA if --p99-threshold is set,
            # otherwise relative-to-baseline (requires 2 consecutive
            # high samples to avoid reacting to one-off spikes).
            if self.p99_threshold_ms is not None:
                if snap.p99_ms > self.p99_threshold_ms:
                    self.high_p99_streak += 1
                    if self.high_p99_streak >= 2 and reason is None:
                        reason = (f"p99={snap.p99_ms:.0f}ms > "
                                  f"threshold {self.p99_threshold_ms:.0f}ms")
                else:
                    self.high_p99_streak = 0
            else:
                if snap.p99_ms > self.baseline_p99 * 1.5:
                    self.high_p99_streak += 1
                    if self.high_p99_streak >= 2 and reason is None:
                        reason = (f"p99={snap.p99_ms:.0f}ms > "
                                  f"baseline*1.5 ({self.baseline_p99*1.5:.0f})")
                else:
                    self.high_p99_streak = 0

            if snap.commanded_mbps > 0 and snap.rx_bitrate_mbps > 0:
                ratio = snap.rx_bitrate_mbps / snap.commanded_mbps
                if ratio < self.shortfall_ratio:
                    self.shortfall_streak += 1
                    if self.shortfall_streak >= 2 and reason is None:
                        reason = (f"achieved {snap.rx_bitrate_mbps:.1f} < "
                                  f"{self.shortfall_ratio:.0%}*commanded "
                                  f"({self.shortfall_ratio*snap.commanded_mbps:.1f})")
                else:
                    self.shortfall_streak = 0

            if reason is not None:
                self.backoff_streak += 1
                new_mbps = max(sc.start_mbps,
                               snap.commanded_mbps * self.backoff_factor)
                self._print_row(snap, f"BACK-OFF ({reason})")
                if self.backoff_streak >= 2:
                    # Ceiling confirmed
                    state.ceiling_mbps = self.last_stable_mbps
                    state.ceiling_reason = reason
                    self._print_row(snap,
                                    f"CEILING @ {self.last_stable_mbps:.1f} Mbps "
                                    f"({reason}); settling")
                    await asyncio.sleep(sc.settle_s)
                    state.stop.set()
                    return
                self._set_rate(new_mbps)
                continue

            # Stable — remember and ramp up
            self.backoff_streak = 0
            self.last_stable_mbps = snap.commanded_mbps
            if snap.commanded_mbps >= sc.max_mbps:
                # Hit configured ceiling without a signal
                state.ceiling_mbps = None
                state.ceiling_reason = f"no ceiling up to {sc.max_mbps} Mbps"
                self._print_row(snap, "MAX reached; no ceiling signal")
                state.stop.set()
                return
            new_mbps = min(sc.max_mbps, snap.commanded_mbps + sc.step_mbps)
            self._print_row(snap, f"ramp → {new_mbps:.1f} Mbps")
            self._set_rate(new_mbps)

    def _set_rate(self, mbps: float) -> None:
        self.state.commanded_mbps = mbps
        self.state.rate_ops = self.scenario.aggregate_ops(mbps)

    @staticmethod
    def _print_row(snap: Snapshot, note: str) -> None:
        print(
            f"  {snap.t:>8.1f}s  "
            f"cmd={snap.commanded_mbps:>6.1f}Mbps  "
            f"rx={snap.rx_bitrate_mbps:>6.1f}Mbps  "
            f"p50={snap.p50_ms:>5.1f}ms  "
            f"p99={snap.p99_ms:>6.1f}ms  "
            f"loss={snap.loss_pct:>4.1f}%  "
            f"{note}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Loopback server: consumer role, hosts the PublishedTrack.
# ---------------------------------------------------------------------------

def _find_default_cert():
    candidates = [
        os.path.join(os.path.dirname(__file__),
                     '..', '..', 'certs', 'cert.pem'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.realpath(c)
    return None


CERT = _find_default_cert()
KEY = CERT.replace('cert.pem', 'key.pem') if CERT else None


async def run_loopback_server(args, state: BenchState):
    """Server publishes bench.load at a rate controlled by state.rate_ops.

    On each SUBSCRIBE, create a PublishedTrack whose per-subgroup rate
    is driven by the controller via an in-process sync task that
    polls state.rate_ops and mutates track.rate.
    """

    async def _on_subscribe(session, msg):
        per_subgroup = state.rate_ops / max(1, args.scenario.subgroups)
        track = PublishedTrack(
            session,
            namespace="bench.load",
            trackname="track",
            object_size=args.scenario.object_size,
            group_size=10000,
            num_subgroups=args.scenario.subgroups,
            rate=per_subgroup,
        )
        # Silence the per-track stats printer — we print our own.
        track._quiet = True
        track._stats_header_printed = True
        ok = session.subscribe_ok(request_msg=msg)
        track.track_alias = ok.track_alias
        track._generating = True

        async def _rate_sync():
            last = -1.0
            while not state.stop.is_set():
                await asyncio.sleep(0.05)
                if state.rate_ops != last:
                    track.rate = (state.rate_ops
                                  / max(1, args.scenario.subgroups))
                    last = state.rate_ops

        sync = asyncio.create_task(_rate_sync())
        try:
            await track.generate(session, ok.track_alias)
        finally:
            sync.cancel()
            try:
                await sync
            except (asyncio.CancelledError, Exception):
                pass

    peer = MOQTPeer()
    peer.endpoint = "moq"
    peer.register_handler(MOQTMessageType.SUBSCRIBE,
                          partial(_on_subscribe))

    config = QuicConfiguration(
        is_client=False,
        alpn_protocols=H3_ALPN,
        verify_mode=ssl.CERT_NONE,
        max_data=2**24,
        max_stream_data=2**24,
        max_datagram_frame_size=64 * 1024,
    )
    config.load_cert_chain(args.cert, args.key)

    return await serve(
        "localhost", args.port,
        configuration=config,
        create_protocol=lambda *a, **kw:
            MOQTSession(*a, **kw, session=peer),
    )


# ---------------------------------------------------------------------------
# Client: subscriber role, feeds LiveStats that the controller reads
# ---------------------------------------------------------------------------

async def run_subscriber_client(host: str, port: int, endpoint: str,
                                use_quic: bool, verify_tls: bool,
                                state: BenchState,
                                stats: LiveStats):
    client = MOQTClient(
        host, port,
        endpoint=endpoint,
        use_quic=use_quic,
        verify_tls=verify_tls,
    )
    try:
        async with client.connect() as session:
            await session.client_session_init()
            track = SubscribedTrack(
                session,
                namespace="bench.load",
                trackname="track",
                on_object=stats.on_object,
            )
            await track.subscribe()
            # Loaded; keep running until the controller sets stop.
            while not state.stop.is_set():
                await asyncio.sleep(0.2)
            session.close()
    except Exception as e:
        print(f"  subscriber error: {e}")


# ---------------------------------------------------------------------------
# Relay-mode publisher: a second MOQTClient that publishes bench.load at
# a rate driven by state.rate_ops. Used when -r URL is passed (no local
# loopback server).
# ---------------------------------------------------------------------------

async def run_publisher_client(host: str, port: int, endpoint: str,
                               use_quic: bool, verify_tls: bool,
                               args,
                               state: BenchState):
    client = MOQTClient(
        host, port,
        endpoint=endpoint,
        use_quic=use_quic,
        verify_tls=verify_tls,
    )
    try:
        async with client.connect() as session:
            await session.client_session_init()
            per_subgroup = state.rate_ops / max(1, args.scenario.subgroups)
            track = PublishedTrack(
                session,
                namespace="bench.load",
                trackname="track",
                object_size=args.scenario.object_size,
                group_size=10000,
                num_subgroups=args.scenario.subgroups,
                rate=per_subgroup,
            )
            track._quiet = True
            track._stats_header_printed = True

            async def _rate_sync():
                last = -1.0
                while not state.stop.is_set():
                    await asyncio.sleep(0.05)
                    if state.rate_ops != last:
                        track.rate = (state.rate_ops
                                      / max(1, args.scenario.subgroups))
                        last = state.rate_ops

            sync = asyncio.create_task(_rate_sync())
            try:
                # publish() sends PUBLISH_NAMESPACE + PUBLISH and then
                # produces data when a SUBSCRIBE arrives.
                await track.publish(
                    announce_namespace=True, publish_track=True)
            finally:
                sync.cancel()
                try:
                    await sync
                except (asyncio.CancelledError, Exception):
                    pass
                session.close()
    except Exception as e:
        print(f"  publisher error: {e}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="aiomoqt adaptive bench v2 — feedback-driven loopback",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Scenarios:\n"
                + "\n".join(f"  {n:<20} {s.description}"
                            for n, s in SCENARIOS.items())),
    )
    p.add_argument("--scenario", required=True, choices=list(SCENARIOS),
                   help="load profile (see epilog)")
    p.add_argument("--start-mbps", type=float, default=None,
                   help="override scenario start bitrate")
    p.add_argument("--step-mbps", type=float, default=None,
                   help="override scenario step size")
    p.add_argument("--max-mbps", type=float, default=None,
                   help="override scenario max bitrate")
    p.add_argument("--interval", type=float, default=None,
                   metavar="S", help="controller tick period seconds")
    p.add_argument("--object-size", type=int, default=None,
                   help="override scenario object size")
    p.add_argument("--streams", "-P", type=int, default=None,
                   help="override scenario subgroup stream count")
    p.add_argument("-r", "--relay", default=None, metavar="URL",
                   help="relay URL (moqt://... or https://...). "
                        "Publisher and subscriber both connect to this "
                        "relay. This is the right mode for real throughput "
                        "measurements.")
    p.add_argument("-k", "--insecure", action="store_true",
                   help="skip TLS verification for relay mode")
    p.add_argument("-p", "--port", type=int, default=4435,
                   help="(loopback mode only) UDP port for the in-process "
                        "qh3 server. Ignored when -r is given. Loopback "
                        "mode is primarily a self-test: its ceiling "
                        "reflects single-process Python overhead, not a "
                        "network or relay limit.")
    p.add_argument("--cert", default=CERT)
    p.add_argument("--key", default=KEY)
    p.add_argument("--report", metavar="PATH",
                   help="write per-sample metrics as CSV to PATH")
    p.add_argument("-l", "--latency-threshold", type=float, default=None,
                   metavar="MS", dest="latency_threshold",
                   help="absolute p99 latency SLA in ms; back off once "
                        "exceeded. If unset, uses relative "
                        "'p99 > baseline * 1.5' policy")
    p.add_argument("--backoff-factor", type=float, default=0.9,
                   dest="backoff_factor", metavar="F",
                   help="multiplicative back-off factor on ceiling signal "
                        "(default: 0.9). Set closer to 1.0 for gentler steps.")
    p.add_argument("--shortfall-ratio", type=float, default=0.85,
                   dest="shortfall_ratio", metavar="R",
                   help="flag shortfall when achieved/commanded < R "
                        "(default: 0.85)")
    p.add_argument("-d", "--debug", action="store_true")
    args = p.parse_args()

    # Assemble the effective scenario from overrides.
    base = SCENARIOS[args.scenario]
    args.scenario = Scenario(
        name=base.name,
        object_size=args.object_size or base.object_size,
        subgroups=args.streams if args.streams is not None else base.subgroups,
        start_mbps=args.start_mbps if args.start_mbps is not None else base.start_mbps,
        step_mbps=args.step_mbps if args.step_mbps is not None else base.step_mbps,
        max_mbps=args.max_mbps if args.max_mbps is not None else base.max_mbps,
        interval_s=args.interval if args.interval is not None else base.interval_s,
        settle_s=base.settle_s,
        description=base.description,
    )
    return args


def _print_banner(args):
    sc = args.scenario
    print("─" * 68)
    print(f"  aiomoqt adaptive bench — scenario '{sc.name}'")
    print("─" * 68)
    print(f"  host:         {platform.node()} ({platform.machine()})")
    print(f"  object size:  {sc.object_size} B")
    print(f"  streams (P):  {sc.subgroups}")
    print(f"  start:        {sc.start_mbps} Mbps")
    print(f"  step:         +{sc.step_mbps} Mbps")
    print(f"  max:          {sc.max_mbps} Mbps")
    print(f"  interval:     {sc.interval_s} s")
    print("─" * 68)


def _print_summary(state: BenchState, samples: int, args):
    print()
    print("═" * 68)
    if state.ceiling_mbps is not None:
        print(f"  Ceiling: {state.ceiling_mbps:.1f} Mbps  "
              f"({state.ceiling_reason})")
    else:
        print(f"  {state.ceiling_reason or 'stopped before ceiling'}")
    print(f"  Samples: {samples}  scenario: {args.scenario.name}")
    print("═" * 68)


async def main():
    from aiomoqt.utils.url import parse_relay_url
    args = parse_args()
    _print_banner(args)

    relay_mode = args.relay is not None
    if not relay_mode and (not args.cert or not args.key):
        print("  error: TLS cert/key not found. Set --cert / --key or place "
              "cert.pem/key.pem under <project>/certs/",
              file=sys.stderr)
        return 2

    set_log_level(__import__('logging').WARNING)

    state = BenchState()
    stats = LiveStats(object_size=args.scenario.object_size,
                      window_s=max(args.scenario.interval_s, 2.0))
    state.commanded_mbps = args.scenario.start_mbps
    state.rate_ops = args.scenario.aggregate_ops(args.scenario.start_mbps)

    csv_file = None
    csv_writer = None
    if args.report:
        csv_file = open(args.report, "w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow([
            "t_monotonic", "commanded_mbps", "rx_mbps", "rx_ops",
            "p50_ms", "p99_ms", "loss_pct", "jitter_ms",
        ])

    controller = AIMDController(
        args.scenario, state, stats, writer=csv_writer,
        p99_threshold_ms=args.latency_threshold,
        backoff_factor=args.backoff_factor,
        shortfall_ratio=args.shortfall_ratio,
    )

    server = None
    pub_task = None
    sub_task = None
    try:
        if relay_mode:
            relay = parse_relay_url(args.relay)
            host, port = relay.host, relay.port
            endpoint = relay.endpoint or "moq"
            use_quic = relay.use_quic
            verify = not args.insecure
            print(f"  relay: {args.relay} ({host}:{port}/{endpoint} "
                  f"via {'QUIC' if use_quic else 'H3/WT'})")
            # Publisher first so PUBLISH_NAMESPACE is in flight before
            # the subscriber's SUBSCRIBE arrives at the relay.
            pub_task = asyncio.create_task(run_publisher_client(
                host, port, endpoint, use_quic, verify, args, state))
            await asyncio.sleep(0.3)
            sub_task = asyncio.create_task(run_subscriber_client(
                host, port, endpoint, use_quic, verify, state, stats))
        else:
            server = await run_loopback_server(args, state)
            await asyncio.sleep(0.3)
            # Loopback cert is self-signed; always verify_tls=False.
            sub_task = asyncio.create_task(run_subscriber_client(
                "localhost", args.port, "moq", False, False,
                state, stats))

        ctrl_task = asyncio.create_task(controller.run())
        await ctrl_task
    finally:
        state.stop.set()
        for t in (sub_task, pub_task):
            if t is None:
                continue
            try:
                await asyncio.wait_for(t, timeout=5.0)
            except asyncio.TimeoutError:
                t.cancel()
            except Exception:
                pass
        if server is not None:
            server.close()
        if csv_file:
            csv_file.close()

    _print_summary(state, controller.samples, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
