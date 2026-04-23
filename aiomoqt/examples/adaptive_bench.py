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
from aiomoqt.utils.format import fmt_bps, fmt_ms


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
    mean_ms: float
    p50_ms: float
    p99_ms: float
    loss_pct: float
    jitter_ms: float


@dataclass
class Signal:
    """Mode-agnostic observation passed from actuator to controller.

    All the pieces the brain needs to decide 'ramp / hold / back-off'
    without knowing whether the load axis is Mbps or subscriber count.
    The commanded/rx_mbps fields are for logging only — the brain uses
    latency + loss + shortfall for decisions.
    """
    t: float
    latency_mean_ms: float
    latency_p50_ms: float
    latency_p99_ms: float
    loss_pct: float
    shortfall: bool
    commanded_mbps: float
    rx_mbps: float


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
                            mean_ms=0.0, p50_ms=0.0, p99_ms=0.0,
                            loss_pct=0.0, jitter_ms=self._jitter)

        window_t = max(0.01, self._events[-1][0] - self._events[0][0])
        n = len(self._events)
        total_bytes = sum(e[1] for e in self._events)
        lats = sorted(e[2] for e in self._events if e[2] is not None)
        mean = sum(lats) / len(lats) if lats else 0.0
        p50 = lats[len(lats) // 2] if lats else 0.0
        p99 = lats[min(int(len(lats) * 0.99), len(lats) - 1)] if lats else 0.0

        total_expected = n + self._lost
        loss_pct = 100.0 * self._lost / total_expected if total_expected else 0.0

        return Snapshot(
            t=t,
            commanded_mbps=commanded_mbps,
            rx_rate_ops=n / window_t,
            rx_bitrate_mbps=(total_bytes * 8) / (window_t * 1e6),
            mean_ms=mean,
            p50_ms=p50,
            p99_ms=p99,
            loss_pct=loss_pct,
            jitter_ms=self._jitter,
        )


# ---------------------------------------------------------------------------
# LoadActuator: the "how do we apply a load level" abstraction
# ---------------------------------------------------------------------------

class BWActuator:
    """Actuator for bandwidth-ramp mode.

    level == aggregate commanded Mbps. Apply writes state.rate_ops,
    which the publisher polls to mutate its paced send rate. Observe
    pulls a rolling-window snapshot from the single subscriber's
    LiveStats and packages it as a mode-agnostic Signal.
    """
    unit = "Mbps"

    def __init__(self, state: 'BenchState', stats: LiveStats,
                 scenario: Scenario,
                 shortfall_ratio: float = 0.85):
        self.state = state
        self.stats = stats
        self.scenario = scenario
        self.shortfall_ratio = shortfall_ratio
        self.min_level = scenario.start_mbps
        self.max_level = scenario.max_mbps
        self.step_floor = 0.5
        self.initial_step = scenario.step_mbps
        self.initial_level = scenario.start_mbps

    async def apply(self, level: float) -> None:
        self.state.commanded_mbps = level
        self.state.rate_ops = self.scenario.aggregate_ops(level)

    async def observe(self) -> Signal:
        snap = self.stats.snapshot(commanded_mbps=self.state.commanded_mbps)
        shortfall = (snap.commanded_mbps > 0
                     and snap.rx_bitrate_mbps
                     < self.shortfall_ratio * snap.commanded_mbps)
        return Signal(
            t=snap.t,
            latency_mean_ms=snap.mean_ms,
            latency_p50_ms=snap.p50_ms,
            latency_p99_ms=snap.p99_ms,
            loss_pct=snap.loss_pct,
            shortfall=shortfall,
            commanded_mbps=snap.commanded_mbps,
            rx_mbps=snap.rx_bitrate_mbps,
        )

    async def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Controller: scalar P-control over a LoadActuator
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

    def __init__(self, actuator, state: BenchState,
                 interval_s: float, writer=None,
                 p50_threshold_ms: float = 100.0,
                 backoff_factor: float = 0.9,
                 loss_threshold_pct: float = 0.5):
        self.actuator = actuator
        self.state = state
        self.interval_s = interval_s
        self.writer = writer
        self.p50_threshold_ms = p50_threshold_ms
        self.backoff_factor = backoff_factor
        self.loss_threshold_pct = loss_threshold_pct
        self.shortfall_streak = 0
        self.high_water = actuator.initial_level
        self.samples = 0
        self.draining = False
        self.t0 = time.monotonic()

    async def run(self):
        state = self.state
        a = self.actuator

        await a.apply(a.initial_level)

        while not state.stop.is_set():
            try:
                await asyncio.wait_for(state.stop.wait(),
                                       timeout=self.interval_s)
                break
            except asyncio.TimeoutError:
                pass

            sig = await a.observe()
            self.samples += 1
            if self.writer:
                self.writer.writerow([
                    f"{sig.t:.3f}",
                    f"{sig.commanded_mbps:.2f}",
                    f"{sig.rx_mbps:.2f}",
                    f"{sig.latency_mean_ms:.2f}",
                    f"{sig.latency_p50_ms:.2f}",
                    f"{sig.latency_p99_ms:.2f}",
                    f"{sig.loss_pct:.3f}",
                ])

            if self.samples < 2:
                self._print_row(sig, "warming")
                continue

            # Dead-air guard: rx=0 for 3 consecutive intervals means
            # no data is flowing (subscribe failed, publisher stuck,
            # etc.). Don't climb a phantom ramp.
            if sig.rx_mbps <= 0:
                self.no_data_streak = getattr(self, 'no_data_streak', 0) + 1
                if self.no_data_streak >= 3:
                    state.ceiling_reason = ("no data received — subscribe "
                                            "failed or publisher stalled")
                    self._print_row(sig, "ABORT: rx=0 for 3 intervals")
                    state.stop.set()
                    return
                self._print_row(sig, f"rx=0 (streak {self.no_data_streak}/3)")
                continue
            self.no_data_streak = 0

            level = sig.commanded_mbps  # same scalar space the actuator uses

            # P-control over latency: headroom ∈ [-∞, 1].
            #   1.0 = idle (p50 = 0), 0.0 = at SLA, <0 = overshoot.
            headroom = (self.p50_threshold_ms - sig.latency_p50_ms) \
                / self.p50_threshold_ms

            # Hard signals override headroom.
            if sig.loss_pct > self.loss_threshold_pct:
                await self._apply(level * self.backoff_factor, sig, "back-off")
                self.draining = True
                continue
            if sig.shortfall:
                self.shortfall_streak += 1
            else:
                self.shortfall_streak = 0
            if self.shortfall_streak >= 2:
                await self._apply(level * self.backoff_factor, sig, "back-off")
                self.shortfall_streak = 0
                self.draining = True
                continue

            if headroom < 0:
                # Overshoot: retreat proportional to how far over we are.
                retreat = max(self.backoff_factor, 1.0 + headroom)
                new_level = max(a.min_level, level * retreat)
                await self._apply(new_level, sig, "back-off")
                self.draining = True
                continue

            if self.draining:
                self._print_row(sig, "drain")
                if headroom >= 0.2:
                    self.draining = False
                continue

            if level >= a.max_level:
                state.ceiling_mbps = self.high_water
                state.ceiling_reason = f"reached max {a.max_level} {a.unit}"
                self._print_row(sig, "max")
                state.stop.set()
                return
            if level > self.high_water:
                self.high_water = level
            # Gain curve: full step when headroom > 0.5, linearly taper
            # to zero at headroom = 0.05. Below that → hold.
            if headroom >= 0.5:
                gain = 1.0
            elif headroom >= 0.05:
                gain = (headroom - 0.05) / 0.45
            else:
                gain = 0.0
            step = a.initial_step * gain
            if step < a.step_floor:
                self._print_row(sig, "hold")
                continue
            await self._apply(min(a.max_level, level + step), sig, "ramp")

    async def _apply(self, new_level: float, sig: Signal, action: str) -> None:
        self._print_row(sig, action)
        await self.actuator.apply(new_level)

    def _print_header(self) -> None:
        print(
            f"  {'time':>6}  "
            f"{'Tx':>7}  {'Rx':>7}  │  "
            f"{'mean':>6}  {'p50':>6}  {'p99':>6}  │  "
            f"{'loss':>6}  action"
        )
        print("─" * 68)

    def _print_row(self, sig: Signal, action: str) -> None:
        if not getattr(self, '_hdr_done', False):
            self._print_header()
            self._hdr_done = True
        t_rel = sig.t - self.t0
        print(
            f"  {t_rel:>5.1f}s  "
            f"{fmt_bps(sig.commanded_mbps * 1e6):>7}  "
            f"{fmt_bps(sig.rx_mbps * 1e6):>7}  │  "
            f"{fmt_ms(sig.latency_mean_ms):>6}  "
            f"{fmt_ms(sig.latency_p50_ms):>6}  "
            f"{fmt_ms(sig.latency_p99_ms):>6}  │  "
            f"{sig.loss_pct:>5.2f}%  {action}",
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
    pub_mode = ("PUB-BOTH" if args.pub_both
                else "PUB-NS" if args.pub_ns
                else "PUBLISH")
    print("─" * 68)
    print("  aiomoqt adaptive bench")
    print("─" * 68)
    print(f"  host:         {platform.node()} ({platform.machine()})")
    print(f"  namespace:    {args.namespace}")
    print(f"  trackname:    {args.trackname}")
    print(f"  draft:        {args.draft or 'auto'}")
    print(f"  pub mode:     {pub_mode}")
    print(f"  object size:  {sc.object_size} B")
    print(f"  streams (P):  {sc.subgroups}")
    print(f"  start:        {sc.start_mbps:.1f} Mbps")
    print(f"  step:         +{sc.step_mbps:.1f} Mbps")
    print(f"  max:          {sc.max_mbps:.1f} Mbps")
    print(f"  interval:     {sc.interval_s:.1f} s")
    print(f"  p50 SLA:      {args.latency_threshold:.0f} ms")
    print("─" * 68)


def _print_summary(state: BenchState, controller, samples: int, args):
    print()
    print("═" * 68)
    print(f"  High-water: {controller.high_water:.1f} {controller.actuator.unit}")
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
            "t_monotonic", "commanded_mbps", "rx_mbps",
            "mean_ms", "p50_ms", "p99_ms", "loss_pct",
        ])

    actuator = BWActuator(
        state, stats, args.scenario,
        shortfall_ratio=args.shortfall_ratio,
    )
    controller = AIMDController(
        actuator, state,
        interval_s=args.scenario.interval_s,
        writer=csv_writer,
        p50_threshold_ms=args.latency_threshold,
        backoff_factor=args.backoff_factor,
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
