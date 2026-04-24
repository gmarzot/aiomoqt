#!/usr/bin/env python3
"""aiomoqt adaptive bench — single-process feedback-driven ramp.

Runs a publisher + subscriber pair in one Python process (loopback
self-test) or against a relay. The publisher's rate is live-mutated
by a controller task reading subscriber-side samples (p90 latency,
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
import uuid
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

from qh3.asyncio.server import serve
from qh3.h3.connection import H3_ALPN
from qh3.quic.configuration import QuicConfiguration

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTPeer, MOQTSession
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import (MOQT_TIMESTAMP_EXT, FilterType, MOQTMessageType,
                           ObjectStatus)
from aiomoqt.utils.format import fmt_bps, fmt_ms
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
# Sample / shared state
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """Subscriber-observed metrics over the current rolling window."""
    t: float
    target_mbps: float       # what the controller is asking for
    rx_rate_ops: float          # objects/sec arriving at subscriber
    rx_bitrate_mbps: float      # equivalent bitrate
    mean_ms: float
    p90_ms: float
    loss_pct: float
    jitter_ms: float


@dataclass
class Signal:
    """Mode-agnostic observation passed from actuator to controller.

    All the pieces the controller needs to decide 'ramp / hold /
    back-off' without knowing whether the load axis is Mbps or
    subscriber count. target/tx/rx_mbps fields are for logging only —
    the controller uses latency + loss + shortfall for decisions.
    """
    t: float
    # Controller scalar — unit matches the actuator (Mbps in bw, count in subs).
    # Used by the controller's ramp/back-off decisions.
    level: float
    latency_mean_ms: float
    latency_p90_ms: float
    loss_pct: float
    shortfall: bool
    # Logging-only fields (Mbps for uniform display):
    target_mbps: float      # controller's ask
    tx_mbps: float          # publisher-side measured (delta bytes / s)
    # subscriber-side rx (sum across active subs in subs-mode):
    rx_mbps: float
    # Liveness — meaningful in subs-mode; BWActuator fills defaults.
    target_subs: int = 1    # count the actuator is trying to maintain
    active_subs: int = 1    # subs currently receiving data
    failed_subs: int = 0    # new subscribe/reset failures in this interval
    health: str = "ok"      # "ok" | "degraded" | "failing"


@dataclass
class BenchState:
    """Shared state between loader, subscriber, and controller tasks.

    Single-writer rule per field:
      rate_ops       — writer: controller
      latest/history — writer: subscriber snapshot loop
      stop/ceiling_* — writer: main or controller
    """
    rate_ops: float = 0.0           # aggregate objects/sec commanded
    target_mbps: float = 0.0        # mirror of rate_ops in Mbps
    tx_rate_mbps: float = 0.0       # writer: publisher (rolling tx rate)
    latest: Optional[Sample] = None
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

    def sample(self, target_mbps: float) -> Sample:
        t = time.monotonic()
        cutoff = t - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        if not self._events:
            return Sample(t=t, target_mbps=target_mbps,
                          rx_rate_ops=0.0, rx_bitrate_mbps=0.0,
                          mean_ms=0.0, p90_ms=0.0,
                          loss_pct=0.0, jitter_ms=self._jitter)

        window_t = max(0.01, self._events[-1][0] - self._events[0][0])
        n = len(self._events)
        total_bytes = sum(e[1] for e in self._events)
        lats = sorted(e[2] for e in self._events if e[2] is not None)
        mean = sum(lats) / len(lats) if lats else 0.0
        p90 = lats[min(int(len(lats) * 0.90), len(lats) - 1)] if lats else 0.0

        total_expected = n + self._lost
        loss_pct = 100.0 * self._lost / total_expected if total_expected else 0.0

        return Sample(
            t=t,
            target_mbps=target_mbps,
            rx_rate_ops=n / window_t,
            rx_bitrate_mbps=(total_bytes * 8) / (window_t * 1e6),
            mean_ms=mean,
            p90_ms=p90,
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
        self.state.target_mbps = level
        self.state.rate_ops = self.scenario.aggregate_ops(level)

    async def observe(self) -> Signal:
        sample = self.stats.sample(target_mbps=self.state.target_mbps)
        shortfall = (sample.target_mbps > 0
                     and sample.rx_bitrate_mbps
                     < self.shortfall_ratio * sample.target_mbps)
        return Signal(
            t=sample.t,
            level=sample.target_mbps,
            latency_mean_ms=sample.mean_ms,
            latency_p90_ms=sample.p90_ms,
            loss_pct=sample.loss_pct,
            shortfall=shortfall,
            target_mbps=sample.target_mbps,
            tx_mbps=self.state.tx_rate_mbps,
            rx_mbps=sample.rx_bitrate_mbps,
        )

    async def shutdown(self) -> None:
        pass


class SubsActuator:
    """Actuator for subscriber-ramp mode.

    level == commanded count of live subscribers. apply(level) spawns
    or terminates multiprocessing workers to reach round(level) healthy
    subs. observe() drains events from the worker queue and aggregates:

      rx_mbps      = sum of per-sub rx_mbps (from last 1s each)
      latency      = worst-sub p50 (SLA is 'no sub is unhappy')
      loss_pct     = max per-sub loss_pct
      shortfall    = active_subs < target_subs for 2+ intervals
      failed_subs  = number of subscribe_failed / stream_reset events
                     since the last observe()
      active_subs  = subs that posted stats in the last window

    Each sub is its own Process (GIL-isolated). Publisher stays
    in-process in the parent (single publisher, fixed rate from
    --sub-mbps — relay does fan-out, so pub is not the bottleneck).
    """
    unit = "subs"

    def __init__(self, relay_url: str,
                 namespace: str, trackname: str,
                 draft, insecure: bool, force_quic: bool,
                 min_subs: int, max_subs: int,
                 start_subs: int, step_subs: int,
                 object_size: int, per_sub_mbps: float,
                 sub_filter: FilterType = FilterType.LATEST_OBJECT,
                 interval_s: float = 5.0):
        self.relay_url = relay_url
        self.namespace = namespace
        self.trackname = trackname
        self.draft = draft
        self.insecure = insecure
        self.force_quic = force_quic
        self.min_level = float(min_subs)
        self.max_level = float(max_subs)
        self.step_floor = 1.0
        self.initial_step = float(step_subs)
        self.initial_level = float(start_subs)
        self.object_size = object_size
        self.per_sub_mbps = per_sub_mbps
        self.sub_filter = sub_filter
        self.interval_s = interval_s

        import multiprocessing as mp
        self._mp = mp
        self._events_q = mp.Queue(maxsize=10000)
        self._workers: dict = {}    # sub_id -> (Process, stop_event)
        self._next_sub_id = 0
        # Per-sub rolling stats (last 'stats' event received):
        self._latest_stats: dict = {}
        # Lifecycle counters for summary + shortfall detection:
        self._subscribed_ever: set = set()
        self._failed_window = 0     # resets each observe()
        self._last_stats_t: dict = {}    # last 'stats' event time per sub
        self._last_bytes_t: dict = {}    # last time we saw rx_bytes > 0 per sub
        self._shortfall_streak = 0
        self._t_last_observe = time.monotonic()

    async def apply(self, level: float) -> None:
        target = max(int(self.min_level), min(int(self.max_level),
                                              int(round(level))))
        # spawn up
        while len(self._workers) < target:
            sid = self._next_sub_id
            self._next_sub_id += 1
            stop_ev = self._mp.Event()
            cfg = dict(
                sub_id=sid,
                relay_url=self.relay_url,
                namespace=self.namespace,
                trackname=self.trackname,
                draft=self.draft,
                insecure=self.insecure,
                force_quic=self.force_quic,
                sub_filter=int(self.sub_filter),
            )
            from aiomoqt.examples._bench_workers import sub_worker_entry
            p = self._mp.Process(
                target=sub_worker_entry,
                args=(cfg, stop_ev, self._events_q),
                daemon=True,
            )
            p.start()
            self._workers[sid] = (p, stop_ev)
        # wind down
        while len(self._workers) > target:
            sid = next(iter(self._workers))
            self._kill_sub(sid)

    def _kill_sub(self, sid):
        proc, stop_ev = self._workers.pop(sid, (None, None))
        if proc is None:
            return
        stop_ev.set()
        # Best-effort: give it 1s to exit cleanly then terminate.
        proc.join(timeout=1.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=1.0)
        self._latest_stats.pop(sid, None)
        self._last_stats_t.pop(sid, None)
        self._last_bytes_t.pop(sid, None)

    async def observe(self) -> Signal:
        # Drain all pending events from workers.
        from queue import Empty
        failed_this_window = 0
        while True:
            try:
                msg = self._events_q.get_nowait()
            except Empty:
                break
            kind = msg.get('kind')
            sid = msg.get('sub_id')
            if kind == 'stats':
                self._latest_stats[sid] = msg
                self._last_stats_t[sid] = time.monotonic()
                if msg.get('rx_bytes', 0) > 0:
                    self._last_bytes_t[sid] = self._last_stats_t[sid]
            elif kind == 'health':
                state = msg.get('state')
                if state == 'subscribed':
                    self._subscribed_ever.add(sid)
                elif state in ('subscribe_failed', 'stream_reset'):
                    failed_this_window += 1
                    # Reap the process — it's done.
                    proc, stop_ev = self._workers.pop(sid, (None, None))
                    if proc is not None:
                        stop_ev.set()
                        proc.join(timeout=0.5)

        # Aggregate from latest stats per sub. "Active" = delivered bytes
        # recently. A worker whose QUIC conn went idle can still post
        # zero-byte stats events and look alive without actually
        # contributing — those subs must not be counted as delivering.
        now = time.monotonic()
        bytes_cutoff = now - 3.0
        active_ids = [sid for sid, t in self._last_bytes_t.items()
                      if t >= bytes_cutoff and sid in self._workers]

        # Starvation reap: any spawned worker that has never received
        # bytes OR hasn't received bytes for 2 * interval is dead.
        # Scaled with interval so fast-tick configs reap quickly and
        # slow-tick configs stay patient. Kill + score as failed so
        # watch/clamp back-off engages.
        starve_window = 2.0 * self.interval_s
        starve_cutoff = now - starve_window
        starved = []
        for sid, (proc, stop_ev) in list(self._workers.items()):
            last_bytes = self._last_bytes_t.get(sid)
            spawned = self._last_stats_t.get(sid, now)
            # Skip freshly spawned subs: no bytes yet AND not older than
            # the starve window (grace for QUIC handshake + subscribe).
            if last_bytes is None and (now - spawned) < starve_window:
                continue
            if last_bytes is None or last_bytes <= starve_cutoff:
                starved.append(sid)
        for sid in starved:
            proc, stop_ev = self._workers.pop(sid, (None, None))
            if proc is not None:
                stop_ev.set()
                proc.join(timeout=0.5)
            self._latest_stats.pop(sid, None)
            self._last_stats_t.pop(sid, None)
            self._last_bytes_t.pop(sid, None)
            failed_this_window += 1

        rx_bps_total = 0.0
        worst_p90 = 0.0
        worst_mean = 0.0
        max_loss_pct = 0.0

        self._t_last_observe = now

        for sid in active_ids:
            s = self._latest_stats.get(sid, {})
            # rx_bytes is a rolling-window (5s) byte count; convert to Mbps.
            rx_bytes = s.get('rx_bytes', 0)
            per_sub_mbps = (rx_bytes * 8) / (5.0 * 1e6)
            rx_bps_total += per_sub_mbps * 1e6
            p90 = s.get('lat_p90_ms', 0.0)
            mean = s.get('lat_mean_ms', 0.0)
            loss_ct = s.get('loss', 0)
            total = s.get('total_objs', 0) or 1
            lpct = 100.0 * loss_ct / (loss_ct + total) if (loss_ct + total) else 0.0
            worst_p90 = max(worst_p90, p90)
            worst_mean = max(worst_mean, mean)
            max_loss_pct = max(max_loss_pct, lpct)

        target_subs = len(self._workers)
        active_subs = len(active_ids)

        if active_subs < target_subs:
            self._shortfall_streak += 1
        else:
            self._shortfall_streak = 0
        shortfall = self._shortfall_streak >= 2 or failed_this_window > 0

        if failed_this_window > 0:
            health = "failing"
        elif active_subs < target_subs:
            health = "degraded"
        else:
            health = "ok"

        target_mbps = target_subs * self.per_sub_mbps
        return Signal(
            t=now,
            level=float(target_subs),
            latency_mean_ms=worst_mean,
            latency_p90_ms=worst_p90,
            loss_pct=max_loss_pct,
            shortfall=shortfall,
            target_mbps=target_mbps,
            tx_mbps=target_mbps,   # single publisher; assume delivers
            rx_mbps=rx_bps_total / 1e6,
            target_subs=target_subs,
            active_subs=active_subs,
            failed_subs=failed_this_window,
            health=health,
        )

    async def shutdown(self) -> None:
        for sid in list(self._workers):
            self._kill_sub(sid)


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
        * p90 > latency_threshold_ms for 2 consecutive intervals
        * achieved rx_bitrate < shortfall_ratio * commanded for 2 intervals
    - Tracks high_water_mbps = highest stable rate seen so far.
    """

    def __init__(self, actuator, state: BenchState,
                 interval_s: float, writer=None,
                 latency_threshold_ms: float = 100.0,
                 backoff_factor: float = 0.9,
                 loss_threshold_pct: float = 0.5):
        self.actuator = actuator
        self.state = state
        self.interval_s = interval_s
        self.writer = writer
        self.latency_threshold_ms = latency_threshold_ms
        self.backoff_factor = backoff_factor
        self.loss_threshold_pct = loss_threshold_pct
        self.shortfall_streak = 0
        self.pressure_streak = 0
        self.hold_intervals = 0
        self.high_water = actuator.initial_level
        self.samples = 0
        self.draining = False
        self.t0 = time.monotonic()
        # Equilibrium learning: record every direction flip (ramp↔back-off)
        # as a pivot. Step size shrinks with pivot count, and eq_level is an
        # EWMA of pivots so later steps converge around the true ceiling.
        self.pivots: list[float] = []
        self.eq_level: float = actuator.initial_level
        self.direction: int = +1   # +1 ramping, -1 backed-off

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
                    f"{sig.target_mbps:.2f}",
                    f"{sig.tx_mbps:.2f}",
                    f"{sig.rx_mbps:.2f}",
                    f"{sig.latency_mean_ms:.2f}",
                    f"{sig.latency_p90_ms:.2f}",
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

            level = sig.level  # controller's scalar, unit matches actuator

            # P-control over latency: headroom ∈ [-∞, 1].
            #   1.0 = idle (p90 = 0), 0.0 = at SLA, <0 = overshoot.
            # p90 is more sensitive than p50 — catches queue build-up
            # sooner while being less noisy than p99.
            headroom = (self.latency_threshold_ms - sig.latency_p90_ms) \
                / self.latency_threshold_ms

            # Unified back-off signal with watch + clamped retreat:
            # any of {loss, sustained shortfall, p90 overshoot} increments
            # a pressure streak. First bad interval just 'watches' — we
            # don't kill anything unless the signal persists. Keeps
            # transient spikes (GC pause, QUIC RTT jitter) from
            # triggering a sub-reap cliff.
            over_p90 = headroom < 0
            over_loss = sig.loss_pct > self.loss_threshold_pct
            if sig.shortfall:
                self.shortfall_streak += 1
            else:
                self.shortfall_streak = 0
            over_shortfall = self.shortfall_streak >= 2
            if over_p90 or over_loss or over_shortfall:
                self.pressure_streak += 1
                reasons = []
                if over_p90:
                    reasons.append("p90 over")
                if over_loss:
                    reasons.append(f"loss {sig.loss_pct:.1f}%")
                if over_shortfall:
                    reasons.append("shortfall")
                if self.pressure_streak < 2:
                    self._print_row(sig, f"watch ({', '.join(reasons)})")
                    continue
                # Persistent pressure — back off, but clamp to at most
                # one --step so we never drop 30% of subs in one swing.
                if over_p90:
                    retreat = max(self.backoff_factor, 1.0 + headroom)
                    new_level = level * retreat
                else:
                    new_level = level * self.backoff_factor
                new_level = max(a.min_level, new_level)
                # Symmetric clamp with ramp: drop at most initial_step.
                new_level = max(new_level, level - a.initial_step)
                self.pressure_streak = 0
                self.shortfall_streak = 0
                await self._apply(new_level, sig,
                                  f"back-off ({', '.join(reasons)})")
                self.draining = True
                continue
            self.pressure_streak = 0

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
                latency_gain = 1.0
            elif headroom >= 0.05:
                latency_gain = (headroom - 0.05) / 0.45
            else:
                latency_gain = 0.0
            # Pivot-count gain: 1.0 → 0.71 → 0.55 → 0.45 → ... floored at 0.05.
            # Each direction flip observed shrinks the step, so later passes
            # converge inside the bracket we've already bisected.
            pivot_gain = 1.0 / (1.0 + 0.4 * len(self.pivots))
            step = a.initial_step * latency_gain * pivot_gain
            if step < a.step_floor:
                # Held by pivot-gain convergence. If headroom is plentiful
                # (conditions clearly healthy), probe upward by step_floor
                # every few intervals — watch+clamp protect us from a bad
                # probe. Prevents the controller from sitting forever at
                # a stale equilibrium when the network has freed up.
                self.hold_intervals += 1
                if headroom >= 0.5 and self.hold_intervals >= 5:
                    self.hold_intervals = 0
                    await self._apply(
                        min(a.max_level, level + a.step_floor),
                        sig, "probe"
                    )
                    continue
                self._print_row(sig, "hold")
                continue
            self.hold_intervals = 0
            await self._apply(min(a.max_level, level + step), sig, "ramp")

    async def _apply(self, new_level: float, sig: Signal, action: str) -> None:
        # Record a pivot whenever direction flips (ramp ↔ back-off).
        # Updates eq_level as an EWMA so future decisions converge on
        # the learned equilibrium point.
        new_direction = +1 if new_level > sig.level else -1
        if new_direction != self.direction:
            self.pivots.append(sig.level)
            if len(self.pivots) > 10:
                self.pivots.pop(0)
            alpha = 0.3
            self.eq_level = (1 - alpha) * self.eq_level + alpha * sig.level
            self.direction = new_direction
        self._print_row(sig, action)
        await self.actuator.apply(new_level)

    def _print_header(self) -> None:
        has_subs = self.actuator.unit == "subs"
        # Group spans — must match widths used by _print_row.
        # Target:  BW(7) + gap(2) + [Nsubs(5) + gap(2)] if subs
        # Actual:  Tx(7) + gap(2) + Rx(7) + gap(2) + [Nsubs(5) + gap(2)]
        # Latency: mean(6) + gap(2) + p90(6)
        target_span = 7 + (3 + 5 if has_subs else 0)
        actual_span = 7 + 2 + 7 + (3 + 5 if has_subs else 0)
        latency_span = 6 + 2 + 6
        time_pad = 10    # 'time' column + breathing space before BW
        print(
            f"  {' '*time_pad}"
            f"{'Target'.center(target_span)}    │  "
            f"{'Actual'.center(actual_span)}  │  "
            f"{'Latency'.center(latency_span)}  │"
        )
        if has_subs:
            print(
                f"  {'time':>6}      "
                f"{'BW':<7}   {'Nsub':<5}  │  "
                f"{'Tx':<7}  {'Rx':<7}   {'Nsub':<5}  │  "
                f"{'mean':<6}  {'p90':<6}  │  "
                f"loss   action"
            )
        else:
            print(
                f"  {'time':>6}      "
                f"{'BW':<7}  │  "
                f"{'Tx':<7}  {'Rx':<7}  │  "
                f"{'mean':<6}  {'p90':<6}  │  "
                f"loss   action"
            )
        total_w = time_pad + 2 + target_span + 5 + actual_span + 5 + latency_span + 5 + 14
        print("─" * total_w)

    def _print_row(self, sig: Signal, action: str) -> None:
        if not getattr(self, '_hdr_done', False):
            self._print_header()
            self._hdr_done = True
        t_rel = sig.t - self.t0
        bw = fmt_bps(sig.target_mbps * 1e6)
        tx = fmt_bps(sig.tx_mbps * 1e6)
        rx = fmt_bps(sig.rx_mbps * 1e6)
        mean = fmt_ms(sig.latency_mean_ms)
        p90 = fmt_ms(sig.latency_p90_ms)
        if self.actuator.unit == "subs":
            print(
                f"  {t_rel:>5.1f}s     "
                f"{bw:<7}    {sig.target_subs:<5}  │  "
                f"{tx:<7}  {rx:<7}   {sig.active_subs:<5}  │  "
                f"{mean:<6}  {p90:<6}  │  "
                f"{f'{sig.loss_pct:.1f}%':<5}  {action}",
                flush=True,
            )
        else:
            print(
                f"  {t_rel:>5.1f}s     "
                f"{bw:<7}  │  "
                f"{tx:<7}  {rx:<7}  │  "
                f"{mean:<6}  {p90:<6}  │  "
                f"{f'{sig.loss_pct:.1f}%':<5}  {action}",
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
            last_bytes = track._total_bytes
            last_t = time.monotonic()
            tick = 0
            while not state.stop.is_set():
                await asyncio.sleep(0.05)
                if state.rate_ops != last:
                    track.rate = (state.rate_ops
                                  / max(1, args.scenario.subgroups))
                    last = state.rate_ops
                tick += 1
                if tick >= 20:  # ~1s at 50ms tick
                    tick = 0
                    now = time.monotonic()
                    cur_bytes = track._total_bytes
                    dt = now - last_t
                    if dt > 0:
                        state.tx_rate_mbps = (cur_bytes - last_bytes) \
                            * 8.0 / (dt * 1e6)
                    last_t = now
                    last_bytes = cur_bytes

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
    peer.endpoint = ""
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
                last_bytes = track._total_bytes
                last_t = time.monotonic()
                tick = 0
                while not state.stop.is_set():
                    await asyncio.sleep(0.05)
                    if state.rate_ops != last:
                        track.rate = (state.rate_ops
                                      / max(1, args.scenario.subgroups))
                        last = state.rate_ops
                    tick += 1
                    if tick >= 20:  # ~1s at 50ms tick
                        tick = 0
                        now = time.monotonic()
                        cur_bytes = track._total_bytes
                        dt = now - last_t
                        if dt > 0:
                            state.tx_rate_mbps = (cur_bytes - last_bytes) \
                                * 8.0 / (dt * 1e6)
                        last_t = now
                        last_bytes = cur_bytes

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
    p.add_argument("-n", "--namespace", default="aiomoqt",
                   help="MoQT namespace (default: aiomoqt)")
    p.add_argument("--trackname", default=None,
                   help="MoQT trackname (default: adaptive-bench-<rand4>)")
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
                   help="p90 latency SLA in ms; back off after 2 intervals "
                        "over (default: 100)")
    p.add_argument("--backoff-factor", type=float, default=0.9,
                   dest="backoff_factor", metavar="F",
                   help="multiplicative back-off factor on ceiling signal "
                        "(default: 0.9). Set closer to 1.0 for gentler steps.")
    p.add_argument("--shortfall-ratio", type=float, default=0.85,
                   dest="shortfall_ratio", metavar="R",
                   help="flag shortfall when achieved/commanded < R "
                        "(default: 0.85)")
    p.add_argument("--mode", choices=["bw", "subs"], default="bw",
                   help="ramp axis: bandwidth (default) or subscriber count")
    p.add_argument("--start-subs", type=int, default=1,
                   help="subs-mode: initial subscriber count (default: 1)")
    p.add_argument("--step-subs", type=int, default=1,
                   help="subs-mode: subs added per ramp step (default: 1)")
    p.add_argument("--max-subs", type=int, default=200,
                   help="subs-mode: cap (default: 200)")
    p.add_argument("--sub-mbps", type=float, default=10.0,
                   help="subs-mode: fixed per-track publisher bitrate "
                        "(default: 10 Mbps)")
    # CLI names from FilterType enum: kebab-case of the member name.
    # NEXT_GROUP_START → next-group-start, LATEST_OBJECT → latest-object,
    # etc. ABSOLUTE_START/RANGE are enumerated here but rejected below
    # because they require a Start Location we don't plumb yet.
    filter_choices = {
        f.name.lower().replace("_", "-"): f for f in FilterType
    }
    p.add_argument("--sub-filter",
                   choices=sorted(filter_choices),
                   default="latest-object",
                   help="subscribe filter (default: latest-object). "
                        "'next-group-start' skips current-group replay "
                        "some relays do on latest-object.")
    p.add_argument("-d", "--debug", action="store_true")
    args = p.parse_args()
    args.sub_filter = filter_choices[args.sub_filter]
    if args.sub_filter in (FilterType.ABSOLUTE_START,
                           FilterType.ABSOLUTE_RANGE):
        p.error(f"--sub-filter {args.sub_filter.name.lower()} requires "
                "a Start Location which is not yet plumbed; use "
                "latest-object or next-group-start.")

    if not args.pub_ns and not args.pub_both:
        args.pub_both = True

    if args.trackname is None:
        args.trackname = f"adaptive-bench-{uuid.uuid4().hex[:4]}"

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
    draft_s = f"draft-{args.draft}" if args.draft else "draft-auto"
    print("─" * 68)
    print("  aiomoqt adaptive bench")
    print("─" * 68)
    print(f"  host:               {platform.node()} ({platform.machine()})")
    if args.relay_url:
        from aiomoqt.utils.url import parse_relay_url
        relay = parse_relay_url(args.relay_url)
        transport = "QUIC" if relay.use_quic else "H3/WebTransport"
        print(f"  relay:              {args.relay_url} ({transport}/{draft_s})")
    else:
        print(f"  relay:              loopback self-test ({draft_s})")
    print(f"  trackname:          {args.namespace}/{args.trackname}")
    publish_parts = [pub_mode, f"obj={sc.object_size}B", f"P={sc.subgroups}"]
    if args.mode == "subs":
        publish_parts.append(f"rate={args.sub_mbps:.1f}Mbps")
    print(f"  publish:            {', '.join(publish_parts)}")
    if args.mode == "subs":
        cli_name = args.sub_filter.name.lower().replace("_", "-")
        print(f"  subscribe filter:   {cli_name}")
    print(f"  latency threshold:  {args.latency_threshold:.0f} ms")
    print("─" * 68)
    print()
    print()


def _print_summary(state: BenchState, controller, samples: int, args):
    unit = controller.actuator.unit
    print()
    print("═" * 68)
    print(f"  High-water:  {controller.high_water:.1f} {unit}")
    print(f"  Equilibrium: {controller.eq_level:.1f} {unit}")
    if state.ceiling_reason:
        print(f"  Reason:      {state.ceiling_reason}")
    print(f"  Samples:     {samples}")
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
    state.target_mbps = args.scenario.start_mbps
    state.rate_ops = args.scenario.aggregate_ops(args.scenario.start_mbps)

    csv_file = None
    csv_writer = None
    if args.report:
        csv_file = open(args.report, "w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow([
            "t_monotonic", "target_mbps", "tx_mbps", "rx_mbps",
            "mean_ms", "p90_ms", "loss_pct",
        ])

    if args.mode == "subs":
        if not relay_mode:
            print("  error: --mode subs requires -r RELAY_URL "
                  "(loopback self-test is bw-only)", file=sys.stderr)
            return 2
        # Publisher runs fixed-rate; fold sub-mbps into scenario so the
        # in-process publisher emits at that rate.
        args.scenario.start_mbps = args.sub_mbps
        state.target_mbps = args.sub_mbps
        state.rate_ops = args.scenario.aggregate_ops(args.sub_mbps)
        actuator = SubsActuator(
            args.relay_url,
            args.namespace, args.trackname,
            args.draft, args.insecure, force_quic=False,
            min_subs=1, max_subs=args.max_subs,
            start_subs=args.start_subs, step_subs=args.step_subs,
            object_size=args.scenario.object_size,
            per_sub_mbps=args.sub_mbps,
            sub_filter=args.sub_filter,
            interval_s=args.scenario.interval_s,
        )
    else:
        actuator = BWActuator(
            state, stats, args.scenario,
            shortfall_ratio=args.shortfall_ratio,
        )
    controller = AIMDController(
        actuator, state,
        interval_s=args.scenario.interval_s,
        writer=csv_writer,
        latency_threshold_ms=args.latency_threshold,
        backoff_factor=args.backoff_factor,
    )

    server = None
    pub_task = None
    sub_task = None
    try:
        if relay_mode:
            relay = parse_relay_url(args.relay_url)
            host, port = relay.host, relay.port
            endpoint = relay.endpoint or ""
            use_quic = relay.use_quic
            verify = not args.insecure
            pub_task = asyncio.create_task(run_publisher_client(
                host, port, endpoint, use_quic, verify, args, state))
            await asyncio.sleep(0.3)
            if args.mode == "bw":
                sub_task = asyncio.create_task(run_subscriber_client(
                    host, port, endpoint, use_quic, verify, args, state, stats))
        else:
            args.port = loopback_port  # server code still reads args.port
            server = await run_loopback_server(args, state)
            await asyncio.sleep(0.3)
            sub_task = asyncio.create_task(run_subscriber_client(
                "localhost", loopback_port, "", False, False,
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
        try:
            await actuator.shutdown()
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
