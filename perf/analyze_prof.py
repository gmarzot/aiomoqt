#!/usr/bin/env python3
"""Summarize a cProfile .prof file.

Top-N by self-time (tottime), with cumulative time and call count.
Used to standardize how we report perf changes across releases.

Usage:
    python perf/analyze_prof.py <file.prof> [top_n]

Example:
    python perf/analyze_prof.py perf/baselines/released-0.3.1-0.9.3/A-r5000-sub.prof 20
"""
from __future__ import annotations

import os
import pstats
import sys


def fmt_func(path_lineno_name: tuple[str, int, str]) -> str:
    path, lineno, name = path_lineno_name
    if not path or path == "~":
        return name
    base = os.path.basename(path)
    return f"{base}:{lineno}:{name}"


def summarize(prof_path: str, top_n: int = 20) -> None:
    ps = pstats.Stats(prof_path)
    total = ps.total_tt
    print(f"{prof_path}")
    print(f"  total_tt = {total:.2f}s")
    print()
    print(f"  {'tottime':>8} {'cumtime':>8} {'ncalls':>10}   function")
    print(f"  {'-'*8} {'-'*8} {'-'*10}   {'-'*40}")

    rows = []
    for key, (cc, nc, tt, ct, _callers) in ps.stats.items():
        rows.append((tt, ct, nc, key))
    rows.sort(reverse=True)
    for tt, ct, nc, key in rows[:top_n]:
        print(f"  {tt:8.3f} {ct:8.3f} {nc:10d}   {fmt_func(key)}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    prof_path = argv[1]
    top_n = int(argv[2]) if len(argv) > 2 else 20
    summarize(prof_path, top_n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
