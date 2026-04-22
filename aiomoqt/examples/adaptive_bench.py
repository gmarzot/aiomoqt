#!/usr/bin/env python3
"""aiomoqt adaptive bench — single-process feedback-driven ramp.

Runs a publisher + subscriber pair in one Python process (loopback
self-test) or against a relay. The publisher's rate is live-mutated
by a controller task reading subscriber-side snapshots (p99 latency,
loss, achieved throughput) out of a shared BenchState. One continuous
steady-state publisher whose pacing the controller nudges up or down
per interval — no subprocess-per-step loop.

Load axes are independent CLI flags: --object-size, -P streams,
--start-mbps, --step-mbps, --max-mbps. With P > 1, each subgroup gets
aggregate_ops / P so total offered load matches the commanded Mbps
regardless of parallelism.

Usage:
  python -m aiomoqt.examples.adaptive_bench
  python -m aiomoqt.examples.adaptive_bench -P 4 --step-mbps 20
  python -m aiomoqt.examples.adaptive_bench -r moqt://host:4433 -k --max-mbps 500
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
# Load config: pinned axes, ramp aggregate bitrate
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    object_size: int            # bytes per MoQT object
    subgroups: int              # parallel subgroup streams (P)
    start_mbps: float           # initial aggregate commanded bitrate
    step_mbps: float            # controller ramp step
    max_mbps: float             # cap
    interval_s: float           # controller tick period
    settle_s: float             # hold last stable before stopping

    def aggregate_ops(self, mbps: float) -> float:
        return (mbps * 1e6) / (self.object_size * 8)


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

    Keeps probing forever. QUIC windows widen under pressure, so a
    rate that tripped a signal once may be fine a minute later. The
    controller never locks in a ceiling — it just backs off on a
    signal and immediately resumes ramping from the lower rate. Exit
    is user-driven (Ctrl-C) or --max-mbps.

    - Every `interval_s`, snapshot metrics and decide.
    - Ramp: +step_mbps while loss==0 and p50 stays under SLA.
    - Back off: multiply aggregate by backoff_factor on any signal.
    - Signals:
        * loss_pct > loss_threshold_pct in the last window
        * p50 > p50_threshold_ms for 2 consecutive intervals
        * achieved rx_bitrate < shortfall_ratio * commanded for 2 intervals
    - Tracks high_water_mbps = highest stable rate seen so far.
    """

    def __init__(self, scenario: Scenario, state: BenchState,
                 stats: LiveStats, writer=None,
                 p50_threshold_ms: float = 100.0,
                 backoff_factor: float = 0.9,
                 shortfall_ratio: float = 0.85,
                 loss_threshold_pct: float = 0.5):
        self.scenario = scenario
        self.state = state
        self.stats = stats
        self.writer = writer
        self.p50_threshold_ms = p50_threshold_ms
        self.backoff_factor = backoff_factor
        self.shortfall_ratio = shortfall_ratio
        self.loss_threshold_pct = loss_threshold_pct
        self.high_latency_streak = 0
        self.shortfall_streak = 0
        self.high_water_mbps = scenario.start_mbps
        self.samples = 0
        # Bracket-narrowing state:
        #   last_bad_mbps: rate that last tripped a signal (upper bound)
        #   draining:      holding rate while p50 returns below threshold
        self.last_bad_mbps = None
        self.draining = False

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

            # Dead-air guard: rx=0 for 3 consecutive intervals means
            # no data is flowing (subscribe failed, publisher stuck,
            # etc.). Don't climb a phantom ramp.
            if snap.rx_bitrate_mbps <= 0:
                self.no_data_streak = getattr(self, 'no_data_streak', 0) + 1
                if self.no_data_streak >= 3:
                    state.ceiling_reason = "no data received — subscribe failed or publisher stalled"
                    self._print_row(snap, "ABORT: rx=0 for 3 intervals")
                    state.stop.set()
                    return
                self._print_row(snap, f"rx=0 (streak {self.no_data_streak}/3)")
                continue
            self.no_data_streak = 0

            # Ceiling signal evaluation
            reason = None
            if snap.loss_pct > self.loss_threshold_pct:
                reason = f"loss={snap.loss_pct:.1f}%"

            if snap.p50_ms > self.p50_threshold_ms:
                self.high_latency_streak += 1
                if self.high_latency_streak >= 2 and reason is None:
                    reason = (f"p50={snap.p50_ms:.0f}ms > "
                              f"{self.p50_threshold_ms:.0f}ms")
            else:
                self.high_latency_streak = 0

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
                # Record the bad rate so the next ramp narrows toward it.
                self.last_bad_mbps = snap.commanded_mbps
                new_mbps = max(sc.start_mbps,
                               snap.commanded_mbps * self.backoff_factor)
                self._print_row(snap,
                                f"back-off → {new_mbps:.1f} Mbps ({reason}); "
                                f"bad={self.last_bad_mbps:.1f} "
                                f"hi-water={self.high_water_mbps:.1f}")
                self._set_rate(new_mbps)
                self.high_latency_streak = 0
                self.shortfall_streak = 0
                self.draining = True
                continue

            # Drain phase: hold rate until p50 falls back below threshold.
            # Otherwise we'd stack more objects on top of a queue that's
            # still bleeding off from the last over-commit.
            if self.draining:
                if snap.p50_ms > self.p50_threshold_ms:
                    self._print_row(snap,
                                    f"drain: p50={snap.p50_ms:.0f}ms > "
                                    f"{self.p50_threshold_ms:.0f}ms; "
                                    f"hold {snap.commanded_mbps:.1f} Mbps")
                    continue
                self.draining = False

            # Stable — advance high-water mark and compute next step.
            if snap.commanded_mbps > self.high_water_mbps:
                self.high_water_mbps = snap.commanded_mbps
            if snap.commanded_mbps >= sc.max_mbps:
                state.ceiling_mbps = self.high_water_mbps
                state.ceiling_reason = f"reached --max-mbps {sc.max_mbps}"
                self._print_row(snap, "MAX reached")
                state.stop.set()
                return
            # Narrow the step when we have a known-bad upper bound:
            # target = midpoint(current, bad) — standard bisect step.
            # Capped by --step-mbps so we never jump more than the user
            # configured. Floor 0.5 Mbps to avoid infinite micro-steps.
            if self.last_bad_mbps is not None \
                    and self.last_bad_mbps > snap.commanded_mbps:
                bracket_step = (self.last_bad_mbps - snap.commanded_mbps) * 0.5
                step = max(0.5, min(sc.step_mbps, bracket_step))
            else:
                step = sc.step_mbps
            new_mbps = min(sc.max_mbps, snap.commanded_mbps + step)
            self._print_row(snap,
                            f"ramp → {new_mbps:.1f} Mbps (step=+{step:.1f} "
                            f"bad={self.last_bad_mbps or 0:.1f} "
                            f"hi-water={self.high_water_mbps:.1f})")
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
            namespace=args.namespace,
            trackname=args.trackname,
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
                                args,
                                state: BenchState,
                                stats: LiveStats):
    client = MOQTClient(
        host, port,
        endpoint=endpoint,
        use_quic=use_quic,
        verify_tls=verify_tls,
        draft_version=args.draft,
    )
    try:
        async with client.connect() as session:
            await session.client_session_init()
            deadline = time.monotonic() + 15.0
            track = None
            while time.monotonic() < deadline and not state.stop.is_set():
                track = SubscribedTrack(
                    session,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    on_object=stats.on_object,
                )
                try:
                    await track.subscribe()
                    break
                except Exception as e:
                    msg = str(e)
                    if "code=4" in msg or "does not exist" in msg \
                            or "no such namespace" in msg:
                        await asyncio.sleep(0.3)
                        continue
                    raise
            else:
                print(f"  subscriber error: publisher never announced "
                      f"{args.namespace}/{args.trackname} within 15s")
                state.stop.set()
                return
            while not state.stop.is_set():
                await asyncio.sleep(0.2)
            session.close()
    except Exception as e:
        print(f"  subscriber error: {e}")
        state.stop.set()


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
        draft_version=args.draft,
    )
    try:
        async with client.connect() as session:
            await session.client_session_init()
            per_subgroup = state.rate_ops / max(1, args.scenario.subgroups)
            track = PublishedTrack(
                session,
                namespace=args.namespace,
                trackname=args.trackname,
                object_size=args.scenario.object_size,
                group_size=10000,
                num_subgroups=args.scenario.subgroups,
                rate=per_subgroup,
                draft=args.draft,
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
                await track.publish(
                    announce_namespace=(args.pub_ns or args.pub_both),
                    publish_track=(not args.pub_ns or args.pub_both),
                )
                # Keep the session alive until the controller stops us.
                # track.publish() only emits PUB_NS / PUBLISH; data flow
                # happens in response to incoming SUBSCRIBE.
                while not state.stop.is_set():
                    await asyncio.sleep(0.2)
            finally:
                sync.cancel()
                try:
                    await sync
                except (asyncio.CancelledError, Exception):
                    pass
                session.close()
    except Exception as e:
        print(f"  publisher error: {e}")
        state.stop.set()


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="aiomoqt adaptive bench — feedback-driven bitrate ramp",
    )
    p.add_argument("-s", "--object-size", type=int, default=4096,
                   metavar="B", help="bytes per MoQT object (default: 4096)")
    p.add_argument("-P", "--streams", type=int, default=1,
                   help="parallel subgroup streams (default: 1)")
    p.add_argument("--start-mbps", type=float, default=10.0,
                   help="initial aggregate bitrate (default: 10)")
    p.add_argument("--step-mbps", type=float, default=10.0,
                   help="ramp step size (default: 10)")
    p.add_argument("--max-mbps", type=float, default=10000.0,
                   help="cap; controller stops on signal first (default: 10000)")
    p.add_argument("--interval", type=float, default=5.0,
                   metavar="S", help="controller tick period seconds (default: 5)")
    p.add_argument("--draft", type=int, default=None,
                   help="MoQT draft version (14 or 16)")
    p.add_argument("-n", "--namespace", default="bench.load",
                   help="MoQT namespace (default: bench.load)")
    p.add_argument("--trackname", default=None,
                   help="MoQT trackname (default: auto-unique)")
    pub_mode = p.add_mutually_exclusive_group()
    pub_mode.add_argument("--pub-ns", action="store_true",
                          dest="pub_ns",
                          help="publish via PUB_NS only")
    pub_mode.add_argument("--pub-both", action="store_true",
                          dest="pub_both",
                          help="publish via PUB_NS+PUBLISH (moqx/moq-rs default)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("-r", "--relay-url", default=None, metavar="URL",
                      dest="relay_url",
                      help="relay URL (moqt://... or https://...)")
    mode.add_argument("-p", "--loopback-port", type=int, default=None,
                      metavar="PORT", dest="loopback_port",
                      help="UDP port for the in-process self-test loopback")
    p.add_argument("-k", "--insecure", action="store_true",
                   help="skip TLS verification")
    p.add_argument("--cert", default=CERT)
    p.add_argument("--key", default=KEY)
    p.add_argument("--report", metavar="PATH",
                   help="write per-sample metrics as CSV to PATH")
    p.add_argument("-l", "--latency-threshold", type=float, default=100.0,
                   metavar="MS", dest="latency_threshold",
                   help="p50 latency SLA in ms; back off after 2 intervals "
                        "over (default: 100)")
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

    if not args.pub_ns and not args.pub_both:
        args.pub_both = True

    if args.trackname is None:
        args.trackname = f"bench-{os.getpid()}-{int(time.time()) % 100000}"

    args.scenario = Scenario(
        object_size=args.object_size,
        subgroups=args.streams,
        start_mbps=args.start_mbps,
        step_mbps=args.step_mbps,
        max_mbps=args.max_mbps,
        interval_s=args.interval,
        settle_s=15.0,
    )
    return args


def _print_banner(args):
    sc = args.scenario
    print("─" * 68)
    print("  aiomoqt adaptive bench")
    print("─" * 68)
    print(f"  host:         {platform.node()} ({platform.machine()})")
    print(f"  object size:  {sc.object_size} B")
    print(f"  streams (P):  {sc.subgroups}")
    print(f"  start:        {sc.start_mbps} Mbps")
    print(f"  step:         +{sc.step_mbps} Mbps")
    print(f"  max:          {sc.max_mbps} Mbps")
    print(f"  interval:     {sc.interval_s} s")
    print("─" * 68)


def _print_summary(state: BenchState, controller, samples: int, args):
    print()
    print("═" * 68)
    print(f"  High-water: {controller.high_water_mbps:.1f} Mbps")
    if state.ceiling_reason:
        print(f"  Reason:     {state.ceiling_reason}")
    print(f"  Samples:    {samples}")
    print("═" * 68)


async def main():
    from aiomoqt.utils.url import parse_relay_url
    args = parse_args()
    _print_banner(args)

    relay_mode = args.relay_url is not None
    # Default to loopback self-test if neither mode flag given
    loopback_port = args.loopback_port or 4435
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
        p50_threshold_ms=args.latency_threshold,
        backoff_factor=args.backoff_factor,
        shortfall_ratio=args.shortfall_ratio,
    )

    server = None
    pub_task = None
    sub_task = None
    try:
        if relay_mode:
            relay = parse_relay_url(args.relay_url)
            host, port = relay.host, relay.port
            endpoint = relay.endpoint or "moq"
            use_quic = relay.use_quic
            verify = not args.insecure
            print(f"  relay: {args.relay_url} ({host}:{port}/{endpoint} "
                  f"via {'QUIC' if use_quic else 'H3/WT'})")
            pub_task = asyncio.create_task(run_publisher_client(
                host, port, endpoint, use_quic, verify, args, state))
            await asyncio.sleep(0.3)
            sub_task = asyncio.create_task(run_subscriber_client(
                host, port, endpoint, use_quic, verify, args, state, stats))
        else:
            args.port = loopback_port  # server code still reads args.port
            server = await run_loopback_server(args, state)
            await asyncio.sleep(0.3)
            sub_task = asyncio.create_task(run_subscriber_client(
                "localhost", loopback_port, "moq", False, False,
                args, state, stats))

        ctrl_task = asyncio.create_task(controller.run())
        try:
            await ctrl_task
        except asyncio.CancelledError:
            pass
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
        _print_summary(state, controller, controller.samples, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
