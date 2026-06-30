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
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import (MOQT_TIMESTAMP_EXT, FilterType, MOQTMessageType,
                           ObjectStatus, parse_draft_spec)
from aiomoqt.utils.format import fmt_bps, fmt_ms
from aiomoqt.utils.logger import set_log_level


def _try_install_uvloop() -> bool:
    """Drop in uvloop if available — typically 2-4× over stock asyncio
    on selector-heavy workloads. Stock CPython falls back if the
    import fails. Caller decides when to call (must be before any
    asyncio.run / loop creation)."""
    try:
        import uvloop
        uvloop.install()
        return True
    except ImportError:
        return False

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
    # RFC 3550 jitter — exponential smooth of inter-arrival deviation.
    jitter_ms: float = 0.0
    # Liveness — meaningful in subs-mode; BWActuator fills defaults.
    target_subs: int = 1    # count the actuator is trying to maintain
    active_subs: int = 1    # subs currently receiving data
    failed_subs: int = 0    # new subscribe/reset failures in this interval
    health: str = "ok"      # "ok" | "degraded" | "failing"
    # aiopquic SPSC TX ring depth (bytes queued in the publisher's
    # send rings, summed across streams). Sustained growth means
    # picoquic's worker isn't draining fast enough for the commanded
    # rate — bytes accumulate in our ring instead of going on the wire.
    tx_ring_depth_bytes: int = 0
    tx_ring_depth_max_bytes: int = 0
    tx_ring_streams: int = 0


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

    def on_object(self, msg, size_bytes, recv_time_us,
                  group_id=None, subgroup_id=None):
        """Timestamps are microseconds since epoch on the wire;
        latency stats are float ms (sub-millisecond resolution)."""
        t = time.monotonic()

        # Skip status (non-NORMAL) objects
        is_status = (hasattr(msg, 'status')
                     and getattr(msg, 'status', ObjectStatus.NORMAL)
                     != ObjectStatus.NORMAL)

        send_us = None
        if getattr(msg, 'extensions', None):
            send_us = msg.extensions.get(MOQT_TIMESTAMP_EXT)
        latency = None
        if send_us is not None:
            raw_us = recv_time_us - send_us
            # Accept up to 10 minutes (under-load latency can exceed
            # 60s); reject negatives and varint-garbage outliers.
            if -1_000_000 <= raw_us <= 600_000_000:
                latency = raw_us / 1000.0  # float ms
                if self._last_recv_ms and self._last_send_ms:
                    d = abs((recv_time_us - self._last_recv_ms)
                            - (send_us - self._last_send_ms)) / 1000.0
                    self._jitter += (d - self._jitter) / 16.0
                self._last_recv_ms = recv_time_us
                self._last_send_ms = send_us

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

    Loopback mode (no -r): publisher + subscriber tasks share the main
    asyncio loop; rate is mutated by writing self.state.rate_ops which
    the in-process publisher polls. Stats from the in-process LiveStats.

    Relay mode (-r URL): publisher and subscriber each run in their own
    multiprocessing.Process. Rate flows pub-ward via a rate_queue;
    stats flow subscriber-ward via events_queue. Aggregated by the
    actuator into the same Signal shape so the controller is mode-blind.
    """
    unit = "Mbps"

    def __init__(self, state: 'BenchState', stats: LiveStats,
                 scenario: Scenario,
                 shortfall_ratio: float = 0.85,
                 mp_pub_proc=None, mp_sub_proc=None,
                 mp_rate_queue=None, mp_events_queue=None,
                 mp_stop_event=None):
        self.state = state
        self.stats = stats          # used in loopback mode only
        self.scenario = scenario
        self.shortfall_ratio = shortfall_ratio
        self.min_level = scenario.start_mbps
        self.max_level = scenario.max_mbps
        self.step_floor = 0.5
        self.initial_step = scenario.step_mbps
        self.initial_level = scenario.start_mbps
        # multiprocess plumbing (None in loopback mode)
        self._pub_proc = mp_pub_proc
        self._sub_proc = mp_sub_proc
        self._rate_q = mp_rate_queue
        self._events_q = mp_events_queue
        self._stop_ev = mp_stop_event
        # rolling stats aggregated from sub-worker events
        self._mp_lat_window = []  # (t, lat_ms)
        self._mp_rx_window = []   # (t, rx_bytes)
        self._mp_total_lost = 0
        self._mp_total_objs = 0
        self._mp_window_s = 5.0
        # publisher rates from pub_stats events.
        # tx_bytes  = bytes the publisher queued via send_stream_data.
        # wire_bytes = bytes picoquic actually placed on the wire
        #              (slow-start + cwnd ground truth).
        self._mp_tx_window = []     # (t, tx_bytes)
        self._mp_wire_window = []   # (t, wire_bytes)
        # Latest snapshot of aiopquic SPSC TX ring depth (bytes
        # queued in the per-stream send rings, summed across streams).
        # Sustained growth = picoquic worker can't drain fast enough
        # for the commanded rate; this is the cleanest "memory bloat"
        # signal we have on the publisher side.
        self._mp_tx_ring_depth = 0
        self._mp_tx_ring_depth_max = 0
        self._mp_tx_ring_streams = 0
        # Per-sub-id latest jitter; aggregate = mean across active
        # subs so multi-sub mode reports avg-of-subs not last-of-subs.
        self._mp_jitter_per_sub: dict[int, float] = {}

    @property
    def is_mp(self) -> bool:
        return self._rate_q is not None

    async def apply(self, level: float) -> None:
        self.state.target_mbps = level
        rate_ops = self.scenario.aggregate_ops(level)
        self.state.rate_ops = rate_ops
        if self.is_mp:
            try:
                self._rate_q.put_nowait({'rate_ops': rate_ops})
            except Exception:
                pass

    def _drain_events(self) -> None:
        """Pull every available event from the worker events queue
        and update rolling windows. Called from observe()."""
        from queue import Empty
        while True:
            try:
                ev = self._events_q.get_nowait()
            except Empty:
                break
            kind = ev.get('kind')
            t = ev.get('t', time.monotonic())
            if kind == 'pub_stats':
                self._mp_tx_window.append((t, ev.get('tx_bytes', 0)))
                self._mp_wire_window.append((t, ev.get('wire_bytes', 0)))
                self._mp_tx_ring_depth = ev.get(
                    'tx_ring_depth_bytes', 0)
                self._mp_tx_ring_depth_max = ev.get(
                    'tx_ring_depth_max_bytes', 0)
                self._mp_tx_ring_streams = ev.get(
                    'tx_ring_streams', 0)
            elif kind == 'stats':
                rx_bytes = ev.get('rx_bytes', 0)
                if rx_bytes:
                    self._mp_rx_window.append((t, rx_bytes))
                # _RollingStats.snapshot() exposes mean/p99 plus a
                # raw 'lat_samples' tuple if present; otherwise we
                # synthesize a single sample at the reported mean.
                samples = ev.get('lat_samples') or ()
                if samples:
                    for lat in samples:
                        self._mp_lat_window.append((t, lat))
                else:
                    m = ev.get('lat_mean_ms')
                    if m is not None:
                        self._mp_lat_window.append((t, m))
                self._mp_total_objs += ev.get('rx_objs', 0)
                self._mp_total_lost += ev.get('iv_lost', 0)
                # Per-sub-id smoothed jitter; aggregate is averaged
                # across active subs in observe().
                jit = ev.get('jitter_ms')
                if jit is not None:
                    sid = ev.get('sub_id', 0)
                    self._mp_jitter_per_sub[sid] = jit
            # health events: no-op for stats; logged elsewhere.
        # Trim windows.
        cutoff = time.monotonic() - self._mp_window_s
        self._mp_lat_window = [
            (t, lat) for (t, lat) in self._mp_lat_window if t >= cutoff
        ]
        self._mp_rx_window = [
            (t, b) for (t, b) in self._mp_rx_window if t >= cutoff
        ]
        self._mp_tx_window = [
            (t, b) for (t, b) in self._mp_tx_window if t >= cutoff
        ]
        self._mp_wire_window = [
            (t, b) for (t, b) in self._mp_wire_window if t >= cutoff
        ]

    async def observe(self) -> Signal:
        if not self.is_mp:
            sample = self.stats.sample(
                target_mbps=self.state.target_mbps)
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
                jitter_ms=sample.jitter_ms,
            )
        # Multiprocess path
        self._drain_events()
        t = time.monotonic()
        lats = sorted(lat for (_, lat) in self._mp_lat_window
                      if lat is not None)
        if lats:
            mean = sum(lats) / len(lats)
            p90 = lats[min(int(len(lats) * 0.90), len(lats) - 1)]
        else:
            mean = 0.0
            p90 = 0.0
        rx_bytes = sum(b for (_, b) in self._mp_rx_window)
        # tx column reports wire-bytes (picoquic ground truth); falls
        # back to queued bytes if the wire counter isn't available.
        wire_bytes = sum(b for (_, b) in self._mp_wire_window)
        if wire_bytes > 0:
            tx_bytes = wire_bytes
        else:
            tx_bytes = sum(b for (_, b) in self._mp_tx_window)
        win = max(0.5, self._mp_window_s)
        rx_mbps = (rx_bytes * 8) / (win * 1e6)
        tx_mbps = (tx_bytes * 8) / (win * 1e6)
        loss_pct = (100.0 * self._mp_total_lost
                    / max(1, self._mp_total_objs))
        shortfall = (self.state.target_mbps > 0
                     and rx_mbps
                     < self.shortfall_ratio * self.state.target_mbps)
        # avg jitter across active subs (BW mode = 1 sub; subs mode = N).
        jitter_ms = (sum(self._mp_jitter_per_sub.values())
                     / len(self._mp_jitter_per_sub)
                     if self._mp_jitter_per_sub else 0.0)
        return Signal(
            t=t,
            level=self.state.target_mbps,
            latency_mean_ms=mean,
            latency_p90_ms=p90,
            loss_pct=loss_pct,
            shortfall=shortfall,
            target_mbps=self.state.target_mbps,
            tx_mbps=tx_mbps,
            rx_mbps=rx_mbps,
            jitter_ms=jitter_ms,
            tx_ring_depth_bytes=self._mp_tx_ring_depth,
            tx_ring_depth_max_bytes=self._mp_tx_ring_depth_max,
            tx_ring_streams=self._mp_tx_ring_streams,
        )

    async def shutdown(self) -> None:
        if not self.is_mp:
            return
        try:
            self._stop_ev.set()
        except Exception:
            pass
        for proc in (self._pub_proc, self._sub_proc):
            if proc is None:
                continue
            try:
                proc.join(timeout=2.0)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=1.0)
            except Exception:
                pass


class SubsActuator:
    """Actuator for subscriber-ramp mode.

    level == targeted count of live subscribers. Subs are hosted in
    BATCHES: each worker process runs K = --step-subs subscriptions and
    self-heals individual drops internally, so the relay still sees one
    QUIC connection per sub without one OS process per sub. apply(level)
    spawns/terminates whole workers to reach ceil(level / K). observe()
    drains batch_stats events and aggregates:

      rx_mbps      = sum of per-worker batch rx_mbps (last 1s each)
      latency      = worst batch p90 across workers
      loss_pct     = max per-batch loss_pct
      shortfall    = active_subs < target_subs for 2+ intervals
      failed_subs  = subs lost to reaped (dead/silent) workers since
                     the last observe()
      active_subs  = sum of workers' currently-subscribed slot counts

    Each worker is GIL-isolated. The publisher runs in its own process
    (spawned by the subs-mode entrypoint) so its event loop is never
    starved by the controller's batch-stats drain.
    """
    unit = "subs"

    def __init__(self, relay_url: str,
                 namespace: str, trackname: str,
                 draft, insecure: bool, force_quic: bool,
                 min_subs: int, max_subs: int,
                 start_subs: int, step_subs: int,
                 object_size: int, per_sub_mbps: float,
                 sub_filter: FilterType = FilterType.LATEST_OBJECT,
                 interval_s: float = 5.0,
                 stagger: float = 0.1,
                 no_uvloop: bool = False,
                 keep_alive_interval: float | None = None):
        self.relay_url = relay_url
        self.namespace = namespace
        self.trackname = trackname
        self.draft = draft
        self.insecure = insecure
        self.force_quic = force_quic
        self.min_level = float(min_subs)
        self.max_level = float(max_subs)
        # A group (one worker) hosts K = step_subs self-healing subs and
        # can't be resized after spawn, so the smallest meaningful change
        # in load is ONE GROUP. Probing/ramping by less than a group is a
        # no-op until the targeted count crosses a K-boundary (apply()
        # spawns workers by ceil(target / K)). Floor the step at the group
        # size so every probe adds a whole worker that then ramps its K
        # slots up — the worker-granular form of "trend up to --step-subs".
        self.step_floor = float(max(1, int(step_subs)))
        self.initial_step = float(step_subs)
        self.initial_level = float(start_subs)
        self.object_size = object_size
        self.per_sub_mbps = per_sub_mbps
        self.sub_filter = sub_filter
        self.interval_s = interval_s
        # Seconds between consecutive handshakes in a spawn batch —
        # decoupled from interval_s so handshake spacing is tunable
        # without changing the report cadence.
        self.stagger = stagger
        self.no_uvloop = no_uvloop
        self.keep_alive_interval = keep_alive_interval
        # Each worker process hosts a BATCH of subscriptions and
        # self-heals individual drops internally. --step-subs doubles as
        # the batch size K: one worker == one group of K subs, so the
        # controller adding a group per --join-rate spawns one worker.
        self.batch_size = max(1, int(step_subs))

        import multiprocessing as mp
        self._mp = mp
        self._events_q = mp.Queue(maxsize=10000)
        self._workers: dict = {}    # worker_id -> (Process, stop_event)
        self._next_worker_id = 0
        # Targeted setpoint, in SUBS — the count the controller aims for.
        # Distinct from hosted capacity (workers x K): reaping a dead
        # worker drops live subs but NOT the setpoint, so the next
        # apply() respawns. observe() reports against this, not the live
        # count, so a lost worker never silently lowers the target.
        self._targeted = float(start_subs)
        # Per-worker latest batch_stats event:
        self._latest_batch: dict = {}
        # Lifecycle counters for summary + shortfall detection:
        self._subscribed_ever: set = set()
        self._failed_window = 0     # resets each observe()
        self._last_batch_t: dict = {}    # last batch_stats time per worker
        self._last_bytes_t: dict = {}    # last batch rx_bytes>0 per worker
        self._spawned_t: dict = {}       # spawn time per worker (grace)
        self._shortfall_streak = 0
        self._t_last_observe = time.monotonic()

    def _reap_dead(self) -> int:
        """Remove workers whose process died or whose batch stats went
        silent, so apply() respawns replacements. Individual sub drops
        are self-healed inside the worker; only a dead/silent worker
        process reaches here. Without this a lost worker pins active <
        targeted forever and the controller holds on permanent
        shortfall."""
        now = time.monotonic()
        dead = []
        for wid, (p, _ev) in self._workers.items():
            if not p.is_alive():
                dead.append(wid)
                continue
            t = self._last_batch_t.get(wid)
            if t is not None and now - t > 15.0:
                dead.append(wid)
        for wid in dead:
            self._kill_worker(wid)
        return len(dead)

    async def apply(self, level: float) -> None:
        self._reap_dead()
        target = max(int(self.min_level), min(int(self.max_level),
                                              int(round(level))))
        # Quantize the setpoint to whole groups (workers * K) so reported
        # Target matches the capacity ceil(target / K) actually hosts — a
        # sub-group back-off would otherwise leave it fractional.
        max_workers = max(1, -(-int(self.max_level) // self.batch_size))
        workers_target = max(1, min(max_workers,
                                    -(-target // self.batch_size)))
        target = workers_target * self.batch_size
        self._targeted = float(target)
        # Spawn-up is BOUNDED to one worker (one group of K) per call:
        # the controller adds a group every --join-rate, so an unbounded
        # respawn can't fire dozens of simultaneous batches at a
        # struggling relay (the death-spiral amplifier). Each worker
        # spaces its own K handshakes internally by --stagger.
        if len(self._workers) < workers_target:
            wid = self._next_worker_id
            self._next_worker_id += 1
            stop_ev = self._mp.Event()
            cfg = dict(
                worker_id=wid,
                batch_size=self.batch_size,
                stagger=self.stagger,
                relay_url=self.relay_url,
                namespace=self.namespace,
                trackname=self.trackname,
                draft=self.draft,
                insecure=self.insecure,
                force_quic=self.force_quic,
                sub_filter=int(self.sub_filter),
                no_uvloop=self.no_uvloop,
                keep_alive_interval=self.keep_alive_interval,
            )
            from aiomoqt.examples._bench_workers import sub_batch_worker_entry
            p = self._mp.Process(
                target=sub_batch_worker_entry,
                args=(cfg, stop_ev, self._events_q),
                daemon=True,
            )
            p.start()
            self._workers[wid] = (p, stop_ev)
            self._spawned_t[wid] = time.monotonic()
        # wind down
        while len(self._workers) > workers_target:
            wid = next(iter(self._workers))
            self._kill_worker(wid)

    def _kill_worker(self, wid):
        proc, stop_ev = self._workers.pop(wid, (None, None))
        if proc is None:
            return
        stop_ev.set()
        # Best-effort: give it 1s to exit cleanly then terminate.
        proc.join(timeout=1.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=1.0)
        self._latest_batch.pop(wid, None)
        self._last_batch_t.pop(wid, None)
        self._last_bytes_t.pop(wid, None)
        self._spawned_t.pop(wid, None)

    async def observe(self) -> Signal:
        # Drain all pending batch_stats events from workers.
        from queue import Empty
        failed_this_window = 0
        while True:
            try:
                msg = self._events_q.get_nowait()
            except Empty:
                break
            if msg.get('kind') != 'batch_stats':
                continue
            wid = msg.get('worker_id')
            self._latest_batch[wid] = msg
            self._last_batch_t[wid] = time.monotonic()
            if msg.get('rx_bytes', 0) > 0:
                self._last_bytes_t[wid] = self._last_batch_t[wid]
            if msg.get('active', 0) > 0:
                self._subscribed_ever.add(wid)

        # "Active" subs = subs delivering recently. A worker self-heals
        # individual drops, so its reported active count is the truth of
        # how many of its K slots are currently subscribed; we still gate
        # the worker on recent bytes so a stalled connection's batch
        # isn't counted as delivering.
        now = time.monotonic()
        # ONE window governs both "active" (counted as delivering) and
        # "kept" (not reaped) — eliminates the limbo band where a worker
        # is neither active nor reaped and pins shortfall forever. Scaled
        # with interval, floored so sub-second intervals don't reap on
        # normal jitter.
        keep_window = max(3.0, 1.5 * self.interval_s)
        bytes_cutoff = now - keep_window
        live_ids = [wid for wid, t in self._last_bytes_t.items()
                    if t >= bytes_cutoff and wid in self._workers]

        # Startup grace is a FIXED floor, not interval-scaled: QUIC
        # handshake + SUBSCRIBE is a ~constant cost (and a batch opens K
        # of them staggered), so a small --interval must not reap a
        # worker before it can come online.
        startup_grace = max(8.0, keep_window
                            + self.batch_size * max(0.0, self.stagger))
        starve_cutoff = now - keep_window
        starved = []
        for wid, (proc, stop_ev) in list(self._workers.items()):
            last_bytes = self._last_bytes_t.get(wid)
            # REAL spawn time — never refreshed by stats events, so a
            # never-delivering zombie can't make itself immortal by
            # posting zero-byte batches.
            spawned = self._spawned_t.get(wid, now)
            if last_bytes is None and (now - spawned) < startup_grace:
                continue
            if last_bytes is None or last_bytes <= starve_cutoff:
                starved.append(wid)
        for wid in starved:
            lost = self._latest_batch.get(wid, {}).get('active',
                                                       self.batch_size)
            proc, stop_ev = self._workers.pop(wid, (None, None))
            if proc is not None:
                stop_ev.set()
                proc.join(timeout=0.5)
            self._latest_batch.pop(wid, None)
            self._last_batch_t.pop(wid, None)
            self._last_bytes_t.pop(wid, None)
            self._spawned_t.pop(wid, None)
            failed_this_window += max(1, int(lost))

        rx_bps_total = 0.0
        worst_p90 = 0.0
        worst_mean = 0.0
        max_loss_pct = 0.0
        jitter_sum = 0.0
        jitter_n = 0
        active_subs = 0

        self._t_last_observe = now

        for wid in live_ids:
            s = self._latest_batch.get(wid, {})
            active_subs += int(s.get('active', 0))
            # rx_bytes is the batch's per-snapshot delta (~1s); convert
            # to Mbps using STATS_INTERVAL_S.
            rx_bytes = s.get('rx_bytes', 0)
            rx_bps_total += rx_bytes * 8
            # Latency drives the back-off, so only MATURE workers feed it:
            # a worker still in its startup grace is catching up (old
            # send-timestamps -> seconds-scale p90) and would trip the SLA
            # back-off on every group add. Sustained congestion still
            # registers once it matures.
            if (now - self._spawned_t.get(wid, now)) >= startup_grace:
                worst_p90 = max(worst_p90, s.get('lat_p90_ms', 0.0))
                worst_mean = max(worst_mean, s.get('lat_mean_ms', 0.0))
            loss_ct = s.get('loss', 0)
            total = s.get('total_objs', 0) or 1
            lpct = 100.0 * loss_ct / (loss_ct + total) if (loss_ct + total) else 0.0
            max_loss_pct = max(max_loss_pct, lpct)
            jit = s.get('jitter_ms')
            if jit is not None:
                jitter_sum += jit
                jitter_n += 1

        # Setpoint is the TARGETED count, not live capacity — reaping a
        # worker must not silently lower the target the controller ramps
        # against (else a lost worker permanently decays the run instead
        # of triggering a respawn).
        target_subs = int(self._targeted)

        # Shortfall is judged only against MATURE workers — those past
        # their startup grace. A freshly-spawned group spends its first
        # `startup_grace` bringing K slots online (staggered by --stagger)
        # and self-healing the stragglers; holding it to its full K
        # immediately would read that ramp-up as a deficit and trip a
        # false shortfall the instant the controller adds a group. So a
        # still-settling group is excluded from BOTH sides of the test
        # until its slots have had time to come up. Capped at the setpoint
        # so a partial final batch isn't over-expected. A group that never
        # delivers is still caught by the starve reaper above
        # (failed_this_window), which forces the shortfall independently.
        mature_active = 0
        mature_target = 0
        for wid in self._workers:
            if (now - self._spawned_t.get(wid, now)) < startup_grace:
                continue
            mature_active += int(self._latest_batch.get(wid, {}).get('active', 0))
            mature_target += self.batch_size
        mature_target = min(mature_target, target_subs)

        if mature_active < mature_target:
            self._shortfall_streak += 1
        else:
            self._shortfall_streak = 0
        shortfall = self._shortfall_streak >= 2 or failed_this_window > 0

        if failed_this_window > 0:
            health = "failing"
        elif mature_active < mature_target:
            health = "degraded"
        else:
            health = "ok"

        target_mbps = target_subs * self.per_sub_mbps
        avg_jitter_ms = jitter_sum / jitter_n if jitter_n else 0.0
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
            jitter_ms=avg_jitter_ms,
            target_subs=target_subs,
            active_subs=active_subs,
            failed_subs=failed_this_window,
            health=health,
        )

    async def shutdown(self) -> None:
        # Parallel shutdown: signal all stops at once, join with one
        # shared deadline, terminate stragglers, then a final short
        # join. Serial _kill_worker at 1s each would block ~Nworkers s
        # at large fan-out and starve the asyncio loop.
        items = list(self._workers.items())
        self._workers.clear()
        for _sid, (_p, stop_ev) in items:
            stop_ev.set()
        deadline = time.monotonic() + 2.0
        for _sid, (proc, _ev) in items:
            remaining = max(0.0, deadline - time.monotonic())
            proc.join(timeout=remaining)
        for _sid, (proc, _ev) in items:
            if proc.is_alive():
                proc.terminate()
        for _sid, (proc, _ev) in items:
            proc.join(timeout=1.0)
        self._latest_batch.clear()
        self._last_batch_t.clear()
        self._last_bytes_t.clear()
        self._spawned_t.clear()
        # Don't let the parent's atexit hook block on the events queue
        # feeder thread — workers may have left bytes pending.
        try:
            self._events_q.cancel_join_thread()
            self._events_q.close()
        except Exception:
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
        * p90 > latency_threshold_ms for 2 consecutive intervals
        * achieved rx_bitrate < shortfall_ratio * commanded for 2 intervals
    - Tracks high_water_mbps = highest stable rate seen so far.
    """

    def __init__(self, actuator, state: BenchState,
                 interval_s: float, writer=None,
                 latency_threshold_ms: float = 100.0,
                 backoff_factor: float = 0.9,
                 loss_threshold_pct: float = 0.5,
                 duration_s: float | None = None,
                 tick_s: float | None = None):
        self.actuator = actuator
        self.state = state
        self.interval_s = interval_s
        # tick_s = how often we observe / decide / add a group; report_s
        # = how often we print a row + write CSV. Subs mode passes
        # tick_s=join_rate so a group is added every --join-rate while
        # rows print every --interval. BW mode leaves tick_s=interval,
        # so every tick reports — byte-identical to the old behavior.
        self.report_s = interval_s
        self.tick_s = tick_s if (tick_s and tick_s > 0) else interval_s
        # Streaks are wall-clock windows; express them in ticks so the
        # dead-air / back-off / shortfall reaction times stay constant
        # when tick_s < report_s (otherwise a fast --join-rate would
        # make a dead-air abort fire in ~2s instead of ~3 intervals).
        self._tpr = max(1, round(self.report_s / self.tick_s))
        self._last_report_t = 0.0
        self.writer = writer
        self.latency_threshold_ms = latency_threshold_ms
        self.backoff_factor = backoff_factor
        self.loss_threshold_pct = loss_threshold_pct
        self.duration_s = duration_s
        self.shortfall_streak = 0
        self.pressure_streak = 0
        self.hold_intervals = 0
        # Tracks the *delivered* metric (bw: rx_mbps; subs: active count).
        # Starts at 0 — initial_level is a *target*, not a measurement.
        self.high_water = 0.0
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
                                       timeout=self.tick_s)
                break
            except asyncio.TimeoutError:
                pass

            # Hard wall-clock cap. Checked at tick boundaries so the
            # in-progress sample completes and gets reported.
            if (self.duration_s is not None
                    and time.monotonic() - self.t0 >= self.duration_s):
                state.ceiling_reason = (
                    f"duration cap reached ({self.duration_s:.0f}s)")
                state.stop.set()
                break

            sig = await a.observe()
            self.samples += 1

            if self.samples < 2:
                self._emit(sig, "warming")
                continue

            # Dead-air guard: rx=0 for ~3 report intervals means no data
            # is flowing (subscribe failed, publisher stuck, etc.).
            # Don't climb a phantom ramp.
            if sig.rx_mbps <= 0:
                self.no_data_streak = getattr(self, 'no_data_streak', 0) + 1
                if self.no_data_streak >= 3 * self._tpr:
                    state.ceiling_reason = ("no data received — subscribe "
                                            "failed or publisher stalled")
                    self._emit(sig, "ABORT: rx=0", force=True)
                    state.stop.set()
                    return
                self._emit(sig, "rx=0 (no data)")
                continue
            self.no_data_streak = 0

            level = sig.level  # controller's scalar, unit matches actuator

            # P-control over latency: headroom ∈ [-∞, 1].
            #   1.0 = idle (p90 = 0), 0.0 = at SLA, <0 = overshoot.
            # p90 is more sensitive than p50 — catches queue build-up
            # sooner while being less noisy than p99.
            headroom = (self.latency_threshold_ms - sig.latency_p90_ms) \
                / self.latency_threshold_ms

            # Back-off triggers ONLY on sustained p90 latency overshoot.
            # Loss is reported in the row but never drives the ramp —
            # we want the bench to reveal the actual throughput/latency
            # curve, not throttle on transient dips QUIC will recover
            # from on its own. Shortfall is tolerated for transients
            # but HOLDS the ramp when sustained (see below): delivery
            # structurally below command means a producer/CPU wall, and
            # ramping past it only inflates the target into fiction.
            # First bad latency interval just 'watches' — back off
            # after two consecutive overshoots so transient RTT spikes
            # don't trigger a cliff.
            if sig.shortfall:
                self.shortfall_streak += 1
            else:
                self.shortfall_streak = 0
            over_p90 = headroom < 0
            if over_p90:
                self.pressure_streak += 1
                if self.pressure_streak < 2 * self._tpr:
                    self._emit(sig, "watch (p90 over)")
                    continue
                retreat = max(self.backoff_factor, 1.0 + headroom)
                new_level = level * retreat
                new_level = max(a.min_level, new_level)
                # Symmetric clamp with ramp: drop at most initial_step.
                new_level = max(new_level, level - a.initial_step)
                self.pressure_streak = 0
                await self._apply(new_level, sig, "back-off (p90 over)",
                                  force=True)
                self.draining = True
                continue
            self.pressure_streak = 0

            if self.draining:
                self._emit(sig, "drain")
                if headroom >= 0.2:
                    self.draining = False
                continue

            # Shortfall = delivery has fallen behind command (queue
            # building, though latency may still be under the SLA).
            # Stop ramping the MOMENT we fall behind — climbing further
            # over a growing queue only inflates the target. Hold to
            # let the relay catch up; back off only if it stays behind
            # for the sustained window.
            if sig.shortfall:
                if self.shortfall_streak < 3 * self._tpr:
                    # Onset: hold and watch. apply(level) is a reconcile
                    # (subs respawns lost workers); BW it's a no-op.
                    self._emit(sig, "hold (watch)")
                    await a.apply(level)
                    continue
                # Sustained — the relay isn't catching up.
                if a.unit == "Mbps":
                    # BW: back off the target toward what's actually
                    # delivered, so Target settles at the real ceiling
                    # instead of sitting inflated over a growing TX
                    # queue. Drop by at least backoff_factor, no higher
                    # than the achieved rate; record the pivot so
                    # eq_level converges. Drain until latency recovers.
                    new_level = max(a.min_level,
                                    min(level * self.backoff_factor,
                                        sig.rx_mbps))
                    self.shortfall_streak = 0
                    await self._apply(new_level, sig,
                                      "back-off (shortfall)", force=True)
                    self.draining = True
                else:
                    # Subs: a sustained shortfall is usually a lost
                    # subscriber the reaper will replace — hold +
                    # reconcile (apply at the SAME target respawns it).
                    # Backing off here would kill more subs and
                    # re-introduce the setpoint-decay-on-loss bug.
                    self._emit(sig, "hold (shortfall)")
                    await a.apply(level)
                continue

            if level >= a.max_level:
                state.ceiling_mbps = self.high_water
                state.ceiling_reason = f"reached max {a.max_level} {a.unit}"
                self._emit(sig, "max", force=True)
                state.stop.set()
                return
            # High-water tracks the *delivered* metric (bw: rx_mbps;
            # subs: active sub count), not the commanded target —
            # a target the system can't sustain shouldn't pollute the
            # mark. Only count intervals with healthy headroom and
            # no shortfall.
            if a.unit == "Mbps":
                observed = sig.rx_mbps
            else:
                observed = float(sig.active_subs)
            if (observed > self.high_water
                    and headroom >= 0.2
                    and not sig.shortfall):
                self.high_water = observed
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
            if a.unit == "subs":
                # Group-quantize: the actuator only adds whole workers, so
                # round the step to whole groups. It ramps one group/tick
                # until pivot-gain rounds it to zero, then the probe path
                # below converges — instead of sticky sub-group holds.
                step = round(step / a.step_floor) * a.step_floor
            if step < a.step_floor:
                # Held by pivot-gain convergence. If headroom is plentiful
                # (conditions clearly healthy), probe upward by step_floor
                # every few intervals — watch+clamp protect us from a bad
                # probe. Prevents the controller from sitting forever at
                # a stale equilibrium when the network has freed up.
                self.hold_intervals += 1
                if headroom >= 0.5 and self.hold_intervals >= 5 * self._tpr:
                    self.hold_intervals = 0
                    await self._apply(
                        min(a.max_level, level + a.step_floor),
                        sig, "probe"
                    )
                    continue
                self._emit(sig, "hold")
                await a.apply(level)   # reconcile (no level change)
                continue

            # Ceiling cap: once a back-off has revealed the sustainable
            # ceiling (>=1 pivot), don't ramp command past the best
            # delivered rate by more than a small margin. Hold there and
            # probe up only occasionally — otherwise the ramp re-climbs
            # to the same overshoot and crashes back every cycle (a wide
            # saw-tooth around the ceiling). A probe that delivers more
            # raises high_water, lifting the cap naturally if the path
            # frees up.
            ceiling_margin = 1.05
            if (self.pivots
                    and self.high_water > 0
                    and (level + step) > self.high_water * ceiling_margin):
                self.hold_intervals += 1
                if headroom >= 0.5 and self.hold_intervals >= 4 * self._tpr:
                    self.hold_intervals = 0
                    await self._apply(min(a.max_level, level + a.step_floor),
                                      sig, "probe (ceiling)")
                    continue
                self._emit(sig, "hold (ceiling)")
                await a.apply(level)
                continue
            self.hold_intervals = 0
            await self._apply(min(a.max_level, level + step), sig, "ramp")

    def _emit(self, sig: Signal, action: str, force: bool = False) -> None:
        """Throttle console + CSV output to report_s. The control loop
        ticks at tick_s (= --join-rate in subs mode), but rows print at
        --interval; force=True always emits (terminal + back-off rows
        the user must not miss)."""
        now = time.monotonic()
        if not (force or (now - self._last_report_t) >= self.report_s):
            return
        self._last_report_t = now
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
        self._print_row(sig, action)

    async def _apply(self, new_level: float, sig: Signal, action: str,
                     force: bool = False) -> None:
        # Record a pivot whenever direction flips (ramp ↔ back-off).
        # Updates eq_level as an EWMA so future decisions converge on
        # the learned equilibrium point.
        new_direction = +1 if new_level > sig.level else -1
        if new_direction != self.direction:
            # First pivot SETS eq_level (the initial_level seed is a
            # target, not a measurement, and must not pollute the
            # learned value); later pivots refine it as an EWMA.
            if not self.pivots:
                self.eq_level = sig.level
            else:
                alpha = 0.3
                self.eq_level = ((1 - alpha) * self.eq_level
                                 + alpha * sig.level)
            self.pivots.append(sig.level)
            if len(self.pivots) > 10:
                self.pivots.pop(0)
            self.direction = new_direction
        self._emit(sig, action, force=force)
        await self.actuator.apply(new_level)

    def _print_header(self) -> None:
        has_subs = self.actuator.unit == "subs"
        # Group spans — must match widths used by _print_row.
        # Target:  BW(7) + gap(2) + [Nsubs(5) + gap(2)] if subs
        # Actual:  Tx(7) + gap(2) + Rx(7) + gap(2) + [Nsubs(5) + gap(2)]
        # Latency: mean(6) + gap(2) + p90(6) + gap(2) + jitter(6)
        target_span = 7 + (3 + 5 if has_subs else 0)
        actual_span = 7 + 2 + 7 + (3 + 5 if has_subs else 0)
        latency_span = 6 + 2 + 6 + 2 + 6
        time_pad = 10
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
                f"{'mean':<6}  {'p90':<6}  {'jitter':<6}  │  "
                f"loss   action"
            )
        else:
            print(
                f"  {'time':>6}      "
                f"{'BW':<7}  │  "
                f"{'Tx':<7}  {'Rx':<7}  │  "
                f"{'mean':<6}  {'p90':<6}  {'jitter':<6}  │  "
                f"loss   txQ      action"
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
        jit = fmt_ms(sig.jitter_ms)
        if self.actuator.unit == "subs":
            print(
                f"  {int(round(t_rel)):>5d}s     "
                f"{bw:<7}    {sig.target_subs:<5}  │  "
                f"{tx:<7}  {rx:<7}   {sig.active_subs:<5}  │  "
                f"{mean:<6}  {p90:<6}  {jit:<6}  │  "
                f"{f'{sig.loss_pct:.1f}%':<5}  {action}",
                flush=True,
            )
        else:
            ring_kb = sig.tx_ring_depth_bytes / 1024.0
            ring_col = (f"{ring_kb:>6,.0f}KB"
                        if sig.tx_ring_streams else "      --")
            print(
                f"  {int(round(t_rel)):>5d}s     "
                f"{bw:<7}  │  "
                f"{tx:<7}  {rx:<7}  │  "
                f"{mean:<6}  {p90:<6}  {jit:<6}  │  "
                f"{f'{sig.loss_pct:.1f}%':<5}  "
                f"{ring_col}  {action}",
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

    On each SUBSCRIBE, create a PublishedTrack whose rate is driven by
    the controller via an in-process sync task that polls
    state.rate_ops and mutates track.rate.
    """

    async def _on_subscribe(session, msg):
        # PublishedTrack.rate is AGGREGATE across subgroups (the track
        # divides by num_subgroups in its send loop) — pass rate_ops
        # straight through.
        track = PublishedTrack(
            session,
            namespace=args.namespace,
            trackname=args.trackname,
            object_size=args.scenario.object_size,
            group_size=args.group_size,
            num_subgroups=args.scenario.subgroups,
            rate=state.rate_ops,
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
                    track.rate = state.rate_ops
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

    server = MOQTServer(
        host="localhost", port=args.port,
        certificate=args.cert, private_key=args.key,
        path="",
        congestion_control_algorithm=args.cc_algo,
        tx_max_queued_bytes=args.max_queued_bytes,
        **({'tx_max_inflight_bytes':
            (None if args.max_inflight_bytes == 0
             else args.max_inflight_bytes)}
           if args.max_inflight_bytes is not None else {}),
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE, partial(_on_subscribe))
    return await server.serve()


# ---------------------------------------------------------------------------
# Client: subscriber role, feeds LiveStats that the controller reads
# ---------------------------------------------------------------------------

async def run_subscriber_client(host: str, port: int, path: str,
                                use_quic: bool, verify_tls: bool,
                                args,
                                state: BenchState,
                                stats: LiveStats):
    client = MOQTClient(
        host, port,
        path=path,
        use_quic=use_quic,
        verify_tls=verify_tls,
        supported_drafts=args.draft,
        congestion_control_algorithm=args.cc_algo,
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

async def run_publisher_client(host: str, port: int, path: str,
                               use_quic: bool, verify_tls: bool,
                               args,
                               state: BenchState):
    client = MOQTClient(
        host, port,
        path=path,
        use_quic=use_quic,
        verify_tls=verify_tls,
        supported_drafts=args.draft,
        congestion_control_algorithm=args.cc_algo,
        tx_max_queued_bytes=args.max_queued_bytes,
        **({'tx_max_inflight_bytes':
            (None if args.max_inflight_bytes == 0
             else args.max_inflight_bytes)}
           if args.max_inflight_bytes is not None else {}),
    )
    try:
        async with client.connect() as session:
            await session.client_session_init()
            # PublishedTrack.rate is AGGREGATE across subgroups — no
            # pre-division (the track divides in its send loop).
            track = PublishedTrack(
                session,
                namespace=args.namespace,
                trackname=args.trackname,
                object_size=args.scenario.object_size,
                group_size=args.group_size,
                num_subgroups=args.scenario.subgroups,
                rate=state.rate_ops,
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
        add_help=False,
        description="aiomoqt adaptive bench — feedback-driven bitrate ramp",
    )
    p.add_argument("-s", "--object-size", type=int, default=4096,
                   metavar="B", help="bytes per MoQT object (default: 4096)")
    p.add_argument("-P", "--streams", type=int, default=1,
                   help="parallel subgroup streams (default: 1)")
    p.add_argument("-g", "--group-size", type=int, default=10000,
                   dest="group_size",
                   help="objects per group (default: 10000)")
    p.add_argument("--start-mbps", type=float, default=10.0,
                   help="initial aggregate bitrate (default: 10)")
    p.add_argument("--step-mbps", type=float, default=10.0,
                   help="ramp step size (default: 10)")
    p.add_argument("--max-mbps", type=float, default=10000.0,
                   help="cap; controller stops on signal first (default: 10000)")
    p.add_argument("--interval", type=float, default=5.0,
                   metavar="S", help="controller tick period seconds (default: 5)")
    p.add_argument("--draft", type=parse_draft_spec, default=None,
                   help="MoQT draft version: 14, 16, or 18")
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
    p.add_argument("--mp-loopback", action="store_true", dest="mp_loopback",
                   help="loopback self-test with publisher and subscriber "
                        "in separate processes (use when measuring tx "
                        "ceilings — single-process loopback is GIL-bound).")
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
                   help="subs-mode: subscribers per group (default: 1). "
                        "Also the batch size: one worker process hosts a "
                        "group of this many self-healing subscriptions.")
    p.add_argument("--max-subs", type=int, default=200,
                   help="subs-mode: cap (default: 200)")
    p.add_argument("--sub-mbps", type=float, default=10.0,
                   help="subs-mode: fixed per-track publisher bitrate "
                        "(default: 10 Mbps)")
    p.add_argument("-j", "--join-rate", type=float, default=None,
                   dest="join_rate", metavar="SEC",
                   help="subs-mode: seconds between subscriber GROUPS "
                        "(a group = --step-subs subs). The ramp cadence, "
                        "independent of --interval (reporting). Default: "
                        "--interval (one group per report).")
    p.add_argument("-S", "--stagger", type=float, default=0.1,
                   help="subs-mode: seconds between individual joins "
                        "WITHIN a group (default: 0.1), applied inside "
                        "the worker as it opens its batch. Spaces QUIC "
                        "Initials so a group doesn't burst the relay. "
                        "0 = open the batch simultaneously.")
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
    p.add_argument("-t", "--duration", type=float, default=None,
                   metavar="SECONDS",
                   help="hard wall-clock cap; controller exits after "
                        "this many seconds even if --max-mbps / --max-subs "
                        "hasn't been reached. Useful for fixed-budget "
                        "regression runs.")
    p.add_argument("--keylogfile", default=None,
                   help="TLS secrets log path (NSS Key Log Format) for "
                        "Wireshark decryption of pcap captures. Each MP "
                        "worker (publisher / subscriber) gets a "
                        "process-suffixed file: PATH.pub.<pid>, "
                        "PATH.sub.<pid> — combined when decrypting.")
    p.add_argument("--uvloop", action="store_true",
                   help="Install uvloop instead of stock asyncio "
                        "(experimental test option — measured marginal "
                        "on this stack, where per-event Python work "
                        "dominates loop turnaround. Default: stock "
                        "asyncio.)")
    p.add_argument("--cc-algo", type=str, default=None,
                   help="Congestion control algorithm "
                        "(bbr | bbr1 | newreno | cubic | dcubic | "
                        "prague | fast). Default: aiopquic default "
                        "(bbr1)")
    p.add_argument("--keepalive", type=float, default=None, metavar="SEC",
                   help="QUIC keep-alive interval in seconds (PING) so a "
                        "flow-controlled, consumer-stalled connection "
                        "isn't dropped on the idle timeout. Default: off.")
    p.add_argument("--max-queued-bytes", type=int, default=None,
                   help="Aggregate publisher byte budget across ALL "
                        "streams (QuicConfiguration.tx_max_queued_bytes): "
                        "producer parks at stream rollover while total "
                        "un-transmitted TX bytes exceed this. "
                        "Steady-state latency ~ value / throughput. "
                        "Default: aiopquic default (4 MiB). "
                        "Pass 0 to disable.")
    p.add_argument("--max-inflight-bytes", type=int, default=None,
                   help="Per-stream TX budget (aiomoqt "
                        "tx_max_inflight_bytes): producer pauses while "
                        "one stream's un-transmitted bytes exceed this. "
                        "Default: aiomoqt default (1 MiB). "
                        "Pass 0 to disable.")
    p.add_argument(
        '-?', '--help', action='help',
        help='Show this help message and exit')
    args = p.parse_args()
    # Internal plumbing (workers, actuators) still carries no_uvloop;
    # the CLI is opt-in (--uvloop), so invert once here.
    args.no_uvloop = not args.uvloop
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
    elif args.mp_loopback:
        print(f"  relay:              loopback self-test, mp ({draft_s})")
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
    if unit == "Mbps":
        from aiomoqt.utils.format import fmt_bps
        print(f"  High-water:  {fmt_bps(controller.high_water * 1e6)} "
              f"(rx delivered)")
    else:
        print(f"  High-water:  {controller.high_water:.1f} {unit}")
    if controller.pivots:
        print(f"  Equilibrium: {controller.eq_level:.1f} {unit} "
              f"({len(controller.pivots)} pivots)")
    else:
        print("  Equilibrium: not reached — no ramp/back-off pivot "
              "(ended while still climbing)")
    if state.ceiling_reason:
        print(f"  Reason:      {state.ceiling_reason}")
    print(f"  Samples:     {samples}")
    print("═" * 68)


async def main():
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

    mp_cleanup_queues: list = []
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
            stagger=args.stagger,
            no_uvloop=args.no_uvloop,
            keep_alive_interval=args.keepalive,
        )
    else:
        # BW mode. SP loopback runs publisher + subscriber as in-process
        # asyncio tasks (fast smoke test, GIL-bound). Relay mode and
        # --mp-loopback spawn them in separate processes so neither
        # blocks the other's loop — single-process coupling masks
        # real stack tail latency and caps throughput at one core.
        mp_loopback = (not relay_mode) and args.mp_loopback
        if relay_mode or mp_loopback:
            import multiprocessing as mp
            from aiomoqt.examples._bench_workers import (
                pub_worker_entry, sub_worker_entry, loopback_server_entry,
            )
            mp_stop_event = mp.Event()
            mp_rate_q = mp.Queue(maxsize=128)
            mp_events_q = mp.Queue(maxsize=10000)
            mp_cleanup_queues.extend([mp_rate_q, mp_events_q])
            if mp_loopback:
                # Server proc replaces the relay+pub_proc pair: it both
                # listens for the subscriber and generates data on
                # subscribe (acts as publisher). Sub proc connects to
                # localhost over WT.
                sub_relay_url = (
                    f"https://localhost:{loopback_port}/")
                # Self-test: server and sub must agree on a draft. The WT
                # client auto-resolves to the newest draft (d16), so pin
                # the server to match — leaving it None keeps the server
                # at d14 while the sub speaks d16, and the server's
                # CLIENT_SETUP parse reads out of bounds.
                mp_draft = args.draft if args.draft is not None else 16
                pub_cfg = dict(
                    host="localhost", port=loopback_port,
                    cert=args.cert, key=args.key, path="",
                    draft=mp_draft,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    object_size=args.scenario.object_size,
                    group_size=args.group_size,
                    num_subgroups=args.scenario.subgroups,
                    initial_rate_ops=args.scenario.aggregate_ops(
                        args.scenario.start_mbps),
                    debug=args.debug,
                    no_uvloop=args.no_uvloop,
                )
                sub_cfg = dict(
                    sub_id=0,
                    relay_url=sub_relay_url,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    draft=mp_draft,
                    insecure=True,  # loopback self-signed cert
                    force_quic=False,
                    sub_filter=int(FilterType.LATEST_OBJECT),
                    debug=args.debug,
                    no_uvloop=args.no_uvloop,
                )
                pub_proc = mp.Process(
                    target=loopback_server_entry,
                    args=(pub_cfg, mp_stop_event, mp_rate_q, mp_events_q),
                    daemon=True,
                )
            else:
                pub_cfg = dict(
                    relay_url=args.relay_url,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    draft=args.draft,
                    insecure=args.insecure,
                    force_quic=False,
                    object_size=args.scenario.object_size,
                    group_size=args.group_size,
                    num_subgroups=args.scenario.subgroups,
                    initial_rate_ops=args.scenario.aggregate_ops(
                        args.scenario.start_mbps),
                    pub_ns=args.pub_ns,
                    pub_both=args.pub_both,
                    debug=args.debug,
                    no_uvloop=args.no_uvloop,
                    keylogfile=(f"{args.keylogfile}.pub"
                                if args.keylogfile else None),
                )
                sub_cfg = dict(
                    sub_id=0,
                    relay_url=args.relay_url,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    draft=args.draft,
                    insecure=args.insecure,
                    force_quic=False,
                    sub_filter=int(FilterType.LATEST_OBJECT),
                    debug=args.debug,
                    no_uvloop=args.no_uvloop,
                    keylogfile=(f"{args.keylogfile}.sub"
                                if args.keylogfile else None),
                )
                pub_proc = mp.Process(
                    target=pub_worker_entry,
                    args=(pub_cfg, mp_stop_event, mp_rate_q, mp_events_q),
                    daemon=True,
                )
            sub_proc = mp.Process(
                target=sub_worker_entry,
                args=(sub_cfg, mp_stop_event, mp_events_q),
                daemon=True,
            )
        else:
            mp_stop_event = mp_rate_q = mp_events_q = None
            pub_proc = sub_proc = None
        actuator = BWActuator(
            state, stats, args.scenario,
            shortfall_ratio=args.shortfall_ratio,
            mp_pub_proc=pub_proc, mp_sub_proc=sub_proc,
            mp_rate_queue=mp_rate_q, mp_events_queue=mp_events_q,
            mp_stop_event=mp_stop_event,
        )
    # Subs mode adds a group (--step-subs) every --join-rate while rows
    # print every --interval; BW mode ticks at --interval (tick_s=None).
    controller = AIMDController(
        actuator, state,
        interval_s=args.scenario.interval_s,
        writer=csv_writer,
        latency_threshold_ms=args.latency_threshold,
        backoff_factor=args.backoff_factor,
        duration_s=args.duration,
        tick_s=(args.join_rate if args.mode == "subs" else None),
    )

    server = None
    pub_task = None
    sub_task = None
    # Subs-mode isolated publisher (own process). Distinct names from the
    # BW-mode pub_proc/sub_proc above — reusing `pub_proc` here clobbered
    # the BW start gate, dropping BW relay runs into this subs branch
    # (publisher started, but no subscriber → rx stays 0).
    subs_pub_proc = None
    subs_pub_stop = None
    try:
        if relay_mode or (args.mode == "bw" and args.mp_loopback):
            if args.mode == "bw" and pub_proc is not None and sub_proc is not None:
                # Multiprocess: pub and sub each in their own process.
                # Sequence the spawn — publisher must reach 'published'
                # before the subscriber subscribes, otherwise the relay
                # races the subscribe against the announce and replies
                # SubscribeOk + SubscribeDone(upstream disconnect)
                # before the publisher's track is registered.
                pub_proc.start()
                # Wait up to 5s for pub_health: published — drain
                # events into the actuator's pre-published peek path.
                from queue import Empty
                deadline = time.monotonic() + 5.0
                published = False
                while time.monotonic() < deadline and not published:
                    try:
                        ev = mp_events_q.get(timeout=0.1)
                    except Empty:
                        continue
                    if (ev.get('kind') == 'pub_health'
                            and ev.get('state') == 'published'):
                        published = True
                        break
                    # Re-queue any non-target event so the actuator's
                    # observe() drains them later.
                    mp_events_q.put(ev)
                if not published:
                    print("  warning: publisher didn't report "
                          "'published' within 5s; subscribing anyway "
                          "— may race", file=sys.stderr)
                sub_proc.start()
                await asyncio.sleep(0.3)
            else:
                # Subs mode: publisher in its OWN process. As an
                # in-process task it shared the parent event loop with
                # the controller's per-worker stats drain; past ~600
                # subs that drain monopolized the loop (measured loop
                # lag spiking to 500ms+), starved the publisher task,
                # and the relay reset it. Isolating it removes the
                # coupling. Sub workers are still spawned by
                # SubsActuator; the publisher runs at a fixed
                # --sub-mbps rate (no rate_queue updates needed).
                import multiprocessing as mp
                from aiomoqt.examples._bench_workers import pub_worker_entry
                subs_pub_stop = mp.Event()
                pub_events_q = mp.Queue(maxsize=10000)
                pub_rate_q = mp.Queue(maxsize=8)
                mp_cleanup_queues.extend([pub_events_q, pub_rate_q])
                pub_cfg = dict(
                    relay_url=args.relay_url,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    draft=args.draft,
                    insecure=args.insecure,
                    force_quic=False,
                    object_size=args.scenario.object_size,
                    group_size=args.group_size,
                    num_subgroups=args.scenario.subgroups,
                    initial_rate_ops=args.scenario.aggregate_ops(
                        args.sub_mbps),
                    pub_ns=args.pub_ns,
                    pub_both=args.pub_both,
                    debug=args.debug,
                    no_uvloop=args.no_uvloop,
                    keep_alive_interval=args.keepalive,
                )
                subs_pub_proc = mp.Process(
                    target=pub_worker_entry,
                    args=(pub_cfg, subs_pub_stop, pub_rate_q, pub_events_q),
                    daemon=True,
                )
                subs_pub_proc.start()
                # Wait for 'published' before subs subscribe, else the
                # relay races subscribe against announce.
                from queue import Empty
                deadline = time.monotonic() + 5.0
                published = False
                while time.monotonic() < deadline and not published:
                    # Non-blocking poll: get_nowait + await sleep yields
                    # to the loop. A blocking get(timeout=) would stall
                    # the event loop for the whole deadline if the
                    # publisher is slow (or the relay is down).
                    try:
                        ev = pub_events_q.get_nowait()
                    except Empty:
                        await asyncio.sleep(0.1)
                        continue
                    if (ev.get("kind") == "pub_health"
                            and ev.get("state") == "published"):
                        published = True
                        break
                if not published:
                    print("  warning: publisher didn't report "
                          "'published' within 5s; subscribing anyway "
                          "— may race", file=sys.stderr)
                await asyncio.sleep(0.3)
        else:
            args.port = loopback_port  # server code still reads args.port
            server = await run_loopback_server(args, state)
            await asyncio.sleep(0.3)
            sub_task = asyncio.create_task(run_subscriber_client(
                "localhost", loopback_port, "", False, False,
                args, state, stats))

        # Loop-lag probe (AIOMOQT_LOOP_LAG=1): measures how late the
        # parent event loop services a 0.1s timer. In subs mode the
        # publisher task and the 590-worker stats drain share this one
        # loop; if lag climbs with sub count and spikes when the
        # publisher resets, the wall is single-loop tail latency
        # (harness coupling), not the pico/mvfst stacks. Prints peak +
        # mean lag each report interval.
        lag_task = None
        if os.environ.get("AIOMOQT_LOOP_LAG") == "1":
            async def _loop_lag_probe():
                period = 0.1
                report_every = max(1.0, args.interval)
                peak = 0.0
                acc = 0.0
                n = 0
                t_last = time.monotonic()
                t_report = t_last
                while not state.stop.is_set():
                    await asyncio.sleep(period)
                    now = time.monotonic()
                    lag = (now - t_last) - period
                    t_last = now
                    if lag > 0:
                        peak = max(peak, lag)
                        acc += lag
                        n += 1
                    if now - t_report >= report_every:
                        mean = (acc / n * 1000) if n else 0.0
                        print(f"  [loop-lag] peak={peak * 1000:.1f}ms "
                              f"mean={mean:.1f}ms over {report_every:.0f}s")
                        peak = acc = 0.0
                        n = 0
                        t_report = now
            lag_task = asyncio.create_task(_loop_lag_probe())

        ctrl_task = asyncio.create_task(controller.run())
        try:
            await ctrl_task
        except asyncio.CancelledError:
            pass
    finally:
        state.stop.set()
        if lag_task is not None:
            lag_task.cancel()
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
        # Subs-mode publisher process: signal stop, then join/terminate.
        if subs_pub_stop is not None:
            try:
                subs_pub_stop.set()
            except Exception:
                pass
        if subs_pub_proc is not None:
            try:
                subs_pub_proc.join(timeout=5.0)
                if subs_pub_proc.is_alive():
                    subs_pub_proc.terminate()
                    subs_pub_proc.join(timeout=2.0)
            except Exception:
                pass
        # BW-mode mp queues: detach feeder threads so atexit doesn't
        # block on Queue.put_thread joins after a pub/sub child has
        # been terminated mid-write.
        for q in mp_cleanup_queues:
            try:
                q.cancel_join_thread()
                q.close()
            except Exception:
                pass
        if server is not None:
            server.close()
        if csv_file:
            csv_file.close()
        _print_summary(state, controller, controller.samples, args)
    return 0


if __name__ == "__main__":
    # Install uvloop in the controller process before asyncio.run.
    # Worker processes (publisher / subscriber MP) install it in
    # _bench_workers.{pub,sub}_worker_entry — they're separate
    # interpreters and can't inherit the policy.
    if "--uvloop" in sys.argv:
        if _try_install_uvloop():
            print("  uvloop: enabled (experimental)")
        else:
            print("  uvloop: requested but not installed "
                  "(using stock asyncio)")
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
