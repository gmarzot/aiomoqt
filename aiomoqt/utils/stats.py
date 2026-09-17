"""Track statistics — one implementation for every aiomoqt tool.

`TrackStats` serves both consumers that previously had their own copy:

  live snapshots  — `snapshot()` returns per-call DELTAS plus a windowed
                    latency view, for controllers aggregating across
                    worker processes (adaptive_bench, load_sim).
  console reports — `interval_row()` / `summary()` return the cumulative
                    view with reservoir-sampled percentiles, for tools
                    printing a table (sub_bench, loopback_bench).

Both views come from the same counters, so identical traffic reports
identical numbers regardless of which tool observed it.

Receivers get latency and jitter (they need the peer's send timestamp
from the MOQT_TIMESTAMP_EXT extension and a local receive clock).
Publishers use `SenderStats`, which shares the rate/group accounting but
has no latency view — there is no round trip to measure.

Memory is bounded regardless of run length: latency percentiles come
from fixed-size reservoirs (Algorithm L), never a full history.

on_object runs per received object, so its cost shows up directly in
every throughput number the tools report. `minimal=True` reduces it to
integer counters — exact bytes and rates, no latency/jitter/loss — for
measuring a delivery ceiling without the measurement in the way.
"""
import math
import random
import time
from collections import deque
from typing import Optional

from .format import fmt_bps, fmt_ms, fmt_rate
from ..types import ObjectStatus

MOQT_TIMESTAMP_EXT = 0x20

# Latency sanity window: reject clock-skew / deframer garbage. Real
# under-load latency can be seconds, so the ceiling is generous; the
# floor allows small negative skew before discarding.
_LAT_MIN_US = -1_000_000
_LAT_MAX_US = 600_000_000


def _pct(sorted_data: list, p: float) -> float:
    if not sorted_data:
        return 0.0
    return sorted_data[min(int(len(sorted_data) * p / 100),
                           len(sorted_data) - 1)]


class _Reservoir:
    """Fixed-size uniform sample (Algorithm L) — unbiased percentiles
    from a stream of unknown length at constant memory.

    Algorithm R draws a random number for every item once full;
    Algorithm L draws a geometric skip and then ignores that many items
    without touching the RNG, so draws fall from O(n) to O(k log(n/k)).
    Same distribution, and on a per-object receive path the RNG was
    costing more than the rest of the accounting.
    """

    def __init__(self, cap: int):
        self.cap = cap
        self.items: list = []
        self.seen = 0
        self._w = 1.0
        self._next = 0      # index (1-based) of the next item to accept

    def _reskip(self, i: int) -> None:
        u = random.random() or 1e-12
        self._w *= math.exp(math.log(u) / self.cap)
        u = random.random() or 1e-12
        # log1p(-w) < 0 and log(u) < 0, so the skip is positive.
        self._next = i + int(math.log(u) / math.log1p(-self._w)) + 1

    def add(self, v: float) -> None:
        self.seen += 1
        i = self.seen
        if len(self.items) < self.cap:
            self.items.append(v)
            if len(self.items) == self.cap:
                self._w = 1.0
                self._reskip(i)
            return
        if i < self._next:
            return
        self.items[int(random.random() * self.cap)] = v
        self._reskip(i)

    def clear(self) -> None:
        self.items = []
        self.seen = 0
        self._w = 1.0
        self._next = 0

    def percentiles(self, *ps) -> tuple:
        s = sorted(self.items)
        return tuple(_pct(s, p) for p in ps)


class SenderStats:
    """Publisher-side accounting: objects, bytes, groups, and the rates
    derived from them. No latency/jitter — a sender has no receive
    clock for the objects it emits."""

    def __init__(self):
        self.start_time = 0.0
        self.total_objects = 0
        self.total_bytes = 0
        self.total_groups = 0
        self._iv_objects = 0
        self._iv_bytes = 0
        self._iv_groups = 0
        self._iv_start = 0.0

    def on_group(self) -> None:
        self.total_groups += 1
        self._iv_groups += 1

    def on_object(self, size_bytes: int) -> None:
        now = time.monotonic()
        if self.start_time == 0.0:
            self.start_time = now
            self._iv_start = now
        self.total_objects += 1
        self.total_bytes += size_bytes
        self._iv_objects += 1
        self._iv_bytes += size_bytes

    @staticmethod
    def header() -> str:
        return (f"  {'Interval':<10}{'Grps':<7}{'GrpRate':<10}{'Objs':<10}"
                f"{'ObjRate':<10}{'Bitrate':<10}")

    def interval_row(self, now: Optional[float] = None) -> str:
        now = now or time.monotonic()
        dt = now - self._iv_start
        if dt <= 0:
            return ""
        elapsed = now - self.start_time
        row = (f"  {f'{elapsed - dt:.0f}-{elapsed:.0f}s':<10}"
               f"{self.total_groups:<7}"
               f"{fmt_rate(self._iv_groups / dt):<10}"
               f"{self.total_objects:<10}"
               f"{fmt_rate(self._iv_objects / dt):<10}"
               f"{fmt_bps(self._iv_bytes * 8 / dt):<10}")
        self._iv_objects = self._iv_bytes = self._iv_groups = 0
        self._iv_start = now
        return row


class TrackStats:
    """Receiver-side accounting: everything SenderStats tracks, plus
    latency, jitter, loss and out-of-order detection.

    Loss is stride-aware: objects striped across N parallel subgroups
    arrive with object_id skipping by N, so the stride is learned per
    (group, subgroup) rather than assumed to be 1. Datagrams have no
    subgroup, so they key on group alone.
    """

    def __init__(self, window_s: float = 5.0,
                 reservoir: int = 10000,
                 iv_reservoir: int = 16384,
                 windowed: bool = True,
                 minimal: bool = False,
                 latency_every: int = 1):
        # windowed=False skips the rolling-window upkeep that only
        # snapshot() reads — table-printing consumers (interval_row /
        # summary) never touch it, and this runs per object on the
        # receive callback.
        self.windowed = windowed
        # minimal=True: integer counters only. Bytes and rates stay
        # exact; latency, jitter, loss and group counts are not
        # collected and report n/a.
        self.minimal = minimal
        # Sample 1-in-N objects for latency/jitter. Loss needs every
        # object (gaps require contiguity) but latency does not — at
        # 30k obj/s even N=16 leaves ~2k samples/s for a percentile.
        self.latency_every = max(1, int(latency_every))
        self._lat_tick = 0
        self.window_s = window_s
        self.start_time = 0.0
        self.first_object_time = 0.0
        self.last_object_time = 0.0

        self.total_objects = 0
        self.total_bytes = 0
        self.total_lost = 0
        self.total_ooo = 0
        self._groups_seen: set = set()

        # Cumulative latency: running moments + bounded reservoir.
        self.lat_count = 0
        self.lat_sum = 0.0
        self.lat_sum_sq = 0.0
        self.lat_min = float('inf')
        self.lat_max = float('-inf')
        self._lat_res = _Reservoir(reservoir)
        self._iv_lat_res = _Reservoir(iv_reservoir)

        # Rolling window for snapshot() latency percentiles.
        self._window: deque = deque()      # (t, lat_ms)
        self._win_stride = 1
        self._win_n = 0

        # RFC 3550 interarrival jitter (skew-immune: difference of
        # differences, so a constant clock offset cancels).
        self.jitter = 0.0
        self._last_recv_us = 0
        self._last_send_us = 0

        # Datagram delivery has no subgroup stream, so a "group" is not
        # something we observe opening — it is inferred from group_ids
        # that happen to arrive. Under loss (expected for datagrams) a
        # fully-lost group is never counted at all, making the total a
        # lower bound rather than a measurement. Report n/a instead of a
        # number that looks authoritative.
        self._saw_datagram = False

        # Loss bookkeeping, keyed by (group, subgroup).
        self._expected: dict = {}
        self._stride: dict = {}
        # Consecutive objects almost always continue the same subgroup
        # stream, so the live key's state is held in slots and only
        # written back to the dicts when the key changes. The group and
        # subgroup are compared as scalars, so the hot path builds no
        # tuple and touches no dict. A miss costs what the dict path
        # always cost; a hit costs nothing.
        self._lk_g = -1
        self._lk_s = None
        self._lk_prev = None
        self._lk_stride = None

        # Interval + snapshot deltas
        self._iv_objects = 0
        self._iv_bytes = 0
        self._iv_groups = 0
        self._iv_start = 0.0
        self._snap_objects = 0
        self._snap_bytes = 0
        self._snap_lost = 0

    # -- ingest ------------------------------------------------------

    def on_object(self, msg, size_bytes: int, recv_time_us: int,
                  group_id: int = None, subgroup_id: int = None) -> None:
        # Delivery markers are not objects: counting them would inflate
        # object counts and goodput.
        if getattr(msg, "status", None) not in (None, ObjectStatus.NORMAL):
            return
        if self.minimal:
            # Counters only. The clock is read on the first object and
            # then 1-in-1024, which bounds the reported duration error
            # to well under a millisecond at any rate worth measuring.
            n = self.total_objects + 1
            self.total_objects = n
            self.total_bytes += size_bytes
            self._iv_objects += 1
            self._iv_bytes += size_bytes
            if n == 1 or (n & 1023) == 0:
                now = time.monotonic()
                if self.start_time == 0.0:
                    self.start_time = now
                    self._iv_start = now
                    self.first_object_time = now
                self.last_object_time = now
            return

        now = time.monotonic()
        if self.start_time == 0.0:
            self.start_time = now
            self._iv_start = now
        if self.first_object_time == 0.0:
            self.first_object_time = now
        self.last_object_time = now

        every = self.latency_every
        if every > 1:
            self._lat_tick += 1
            if self._lat_tick < every:
                send_us = None
            else:
                self._lat_tick = 0
                exts = getattr(msg, 'extensions', None)
                send_us = exts.get(MOQT_TIMESTAMP_EXT) if exts else None
        else:
            exts = getattr(msg, 'extensions', None)
            send_us = exts.get(MOQT_TIMESTAMP_EXT) if exts else None
        if send_us is not None and recv_time_us is not None:
            raw = recv_time_us - send_us
            if _LAT_MIN_US <= raw <= _LAT_MAX_US:
                lat = raw / 1000.0
                self.lat_count += 1
                self.lat_sum += lat
                self.lat_sum_sq += lat * lat
                self.lat_min = min(self.lat_min, lat)
                self.lat_max = max(self.lat_max, lat)
                self._lat_res.add(lat)
                self._iv_lat_res.add(lat)
                if self.windowed:
                    self._win_n += 1
                    if self._win_n % self._win_stride == 0:
                        self._window.append((now, lat))
                if self._last_recv_us and self._last_send_us:
                    d = abs((recv_time_us - self._last_recv_us)
                            - (send_us - self._last_send_us)) / 1000.0
                    self.jitter += (d - self.jitter) / 16.0
                self._last_recv_us = recv_time_us
                self._last_send_us = send_us

        # Status objects are wire signals, not data — excluded from
        # loss/rate accounting so counters stay data-only.
        status = getattr(msg, 'status', None)
        if status is not None and status != 0:
            return

        self.total_objects += 1
        self.total_bytes += size_bytes
        self._iv_objects += 1
        self._iv_bytes += size_bytes

        gid = getattr(msg, 'group_id', group_id)
        if gid is not None and gid not in self._groups_seen:
            self._groups_seen.add(gid)
            self._iv_groups += 1

        oid = getattr(msg, 'object_id', None)
        if oid is None or gid is None:
            return
        # Datagrams carry no subgroup; streams key per subgroup.
        has_sg = hasattr(msg, 'subgroup_id') or subgroup_id is not None
        if not has_sg:
            self._saw_datagram = True
        sg = (getattr(msg, 'subgroup_id', None)
              if subgroup_id is None else subgroup_id) if has_sg else None
        if gid == self._lk_g and sg == self._lk_s:
            prev = self._lk_prev
        else:
            self._flush_key()
            self._lk_g, self._lk_s = gid, sg
            key = (gid, sg if sg is not None else 0)
            prev = self._expected.get(key)
            self._lk_stride = self._stride.get(key)
        if prev is None:
            self._lk_prev = oid
            return
        stride = self._lk_stride
        if stride is None and oid > prev:
            stride = self._lk_stride = oid - prev
        if stride and stride > 0:
            if oid > prev + stride:
                self.total_lost += (oid - prev - stride) // stride
            elif oid < prev:
                self.total_ooo += 1
        self._lk_prev = oid if oid > prev else prev

    def _flush_key(self) -> None:
        """Write the cached subgroup state back to the dicts."""
        if self._lk_g != -1:
            key = (self._lk_g, self._lk_s if self._lk_s is not None else 0)
            self._expected[key] = self._lk_prev
            if self._lk_stride is not None:
                self._stride[key] = self._lk_stride
        self._lk_prev = None
        self._lk_stride = None

    # -- views -------------------------------------------------------

    def snapshot(self) -> dict:
        """Per-call deltas + windowed latency. For controllers summing
        across workers without double-counting."""
        self._flush_key()
        now = time.monotonic()
        cutoff = now - self.window_s
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        # Adaptive thinning keeps the percentile sort bounded so a
        # high-rate snapshot can't stall the receive loop.
        if len(self._window) > 24576:
            self._window = deque(list(self._window)[::2])
            self._win_stride *= 2
        lats = sorted(v for _, v in self._window)
        iv_objs = self.total_objects - self._snap_objects
        iv_bytes = self.total_bytes - self._snap_bytes
        iv_lost = self.total_lost - self._snap_lost
        self._snap_objects = self.total_objects
        self._snap_bytes = self.total_bytes
        self._snap_lost = self.total_lost
        return dict(
            t=now, rx_objs=iv_objs, rx_bytes=iv_bytes, iv_lost=iv_lost,
            lat_mean_ms=(sum(lats) / len(lats)) if lats else 0.0,
            lat_p90_ms=_pct(lats, 90), lat_p99_ms=_pct(lats, 99),
            jitter_ms=self.jitter, loss=self.total_lost,
            groups=len(self._groups_seen),
            total_objs=self.total_objects, total_bytes=self.total_bytes,
        )

    @staticmethod
    def header() -> str:
        return (f"  {'Interval':<10}{'Grps':<7}{'GrpRate':<10}{'Objs':<10}"
                f"{'ObjRate':<10}{'Bitrate':<10}{'Latency':<21}"
                f"{'Jitter':<9}{'Loss':<16}")

    def interval_row(self, now: Optional[float] = None) -> str:
        now = now or time.monotonic()
        dt = now - self._iv_start
        if dt <= 0:
            return ""
        elapsed = now - self.start_time
        if self._iv_lat_res.items:
            avg = sum(self._iv_lat_res.items) / len(self._iv_lat_res.items)
            p99, = self._iv_lat_res.percentiles(99)
            lat_s = f"{fmt_ms(avg)} p99:{fmt_ms(p99)}"
        else:
            lat_s = "--"
        expected = self.total_objects + self.total_lost
        loss_pct = (100 * self.total_lost / expected) if expected else 0
        blind = self._saw_datagram or self.minimal
        grps = 'n/a' if blind else str(len(self._groups_seen))
        grp_rate = ('n/a' if blind else fmt_rate(self._iv_groups / dt))
        jit_s = 'n/a' if self.minimal else fmt_ms(self.jitter)
        loss_s = ('n/a' if self.minimal
                  else f'{loss_pct:.2f}% ({self.total_lost})')
        row = (f"  {f'{elapsed - dt:.0f}-{elapsed:.0f}s':<10}"
               f"{grps:<7}"
               f"{grp_rate:<10}"
               f"{self.total_objects:<10}"
               f"{fmt_rate(self._iv_objects / dt):<10}"
               f"{fmt_bps(self._iv_bytes * 8 / dt):<10}"
               f"{lat_s:<21}{jit_s:<9}"
               f"{loss_s:<16}")
        self._iv_objects = self._iv_bytes = self._iv_groups = 0
        self._iv_start = now
        self._iv_lat_res.clear()
        return row

    def summary(self) -> dict:
        """Cumulative view for the end-of-run report. Rates use the
        active window (first→last object) so idle tail time doesn't
        dilute them."""
        if self.start_time == 0.0:
            return {}
        self._flush_key()
        dur = time.monotonic() - self.start_time
        active = (self.last_object_time - self.first_object_time
                  if self.first_object_time else 0) or dur
        mean = self.lat_sum / self.lat_count if self.lat_count else 0.0
        var = ((self.lat_sum_sq / self.lat_count) - mean * mean
               if self.lat_count else 0.0)
        p50, p95, p99 = self._lat_res.percentiles(50, 95, 99)
        expected = self.total_objects + self.total_lost
        return dict(
            duration_s=dur, active_s=active,
            datagram=self._saw_datagram,
            minimal=self.minimal,
            groups=len(self._groups_seen),
            objects=self.total_objects, bytes=self.total_bytes,
            obj_rate=self.total_objects / active,
            grp_rate=len(self._groups_seen) / active,
            mbps=(self.total_bytes * 8) / (active * 1e6),
            lat_min=self.lat_min if self.lat_count else 0.0,
            lat_max=self.lat_max if self.lat_count else 0.0,
            lat_mean=mean, lat_sd=var ** 0.5 if var > 0 else 0.0,
            lat_p50=p50, lat_p95=p95, lat_p99=p99,
            jitter_ms=self.jitter,
            lost=self.total_lost, ooo=self.total_ooo,
            loss_pct=(self.total_lost / expected * 100) if expected else 0.0,
        )
