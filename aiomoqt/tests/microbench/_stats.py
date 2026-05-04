"""Shared stat helpers for microbenches.

Stats: sample collector with running min/max/sum and percentile readout
on `summary()`. Cheap; appends to a list, sort-once at end.
"""
from __future__ import annotations

import bisect
import time


class Stats:
    __slots__ = ("name", "samples", "_t0")

    def __init__(self, name: str = ""):
        self.name = name
        self.samples: list[float] = []
        self._t0 = 0.0

    def record(self, v: float) -> None:
        self.samples.append(v)

    def time_block_start(self) -> None:
        self._t0 = time.perf_counter_ns()

    def time_block_end(self) -> None:
        self.samples.append((time.perf_counter_ns() - self._t0) / 1e6)

    @staticmethod
    def _pct(sorted_samples: list[float], p: float) -> float:
        if not sorted_samples:
            return 0.0
        # nearest-rank
        idx = max(0, min(len(sorted_samples) - 1,
                         int(round((p / 100.0) * len(sorted_samples) - 0.5))))
        return sorted_samples[idx]

    def summary(self, unit: str = "ms") -> str:
        n = len(self.samples)
        if n == 0:
            return f"{self.name}: no samples"
        s = sorted(self.samples)
        mn = s[0]
        mx = s[-1]
        avg = sum(s) / n
        p50 = self._pct(s, 50)
        p95 = self._pct(s, 95)
        p99 = self._pct(s, 99)
        return (
            f"{self.name}: n={n} avg={avg:.3f}{unit} p50={p50:.3f} "
            f"p95={p95:.3f} p99={p99:.3f} min={mn:.3f} max={mx:.3f}"
        )


def time_loop(fn, *, target_seconds: float = 5.0,
              warmup_seconds: float = 0.5) -> tuple[int, float]:
    """Run fn() in a tight loop until target_seconds elapsed; return
    (iterations, elapsed_seconds). Burns warmup_seconds first."""
    end_warmup = time.perf_counter() + warmup_seconds
    while time.perf_counter() < end_warmup:
        fn()
    t0 = time.perf_counter()
    end = t0 + target_seconds
    n = 0
    while time.perf_counter() < end:
        fn()
        n += 1
    return n, time.perf_counter() - t0


def now_ns() -> int:
    return time.perf_counter_ns()
