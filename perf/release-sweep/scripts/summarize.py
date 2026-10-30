#!/usr/bin/env python3
"""summarize.py — one markdown row per point from a sweep run block.

Per point: median across reps of mbps, obj_s, grp_s, p50, p99, interval_cv,
lost, cpu_pub, cpu_sub, plus the rep count and any meta notes.  Median is the
point estimate per PROGRAM §6.9; the full per-rep values stay in runs/.

Usage: summarize.py RUN_BLOCK_DIR[,RUN_BLOCK_DIR...] [--md OUT]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics as st
import sys

COLS = ["mbps", "obj_s", "grp_s", "p50", "p99", "lost", "interval_cv",
        "cpu_pub", "cpu_sub"]


def collect(run_dirs: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for d in run_dirs:
        for path in sorted(glob.glob(os.path.join(d, "*", "rep-*", "result.json"))):
            with open(path) as fh:
                r = json.load(fh)
            pid = r.get("point_id") or os.path.basename(os.path.dirname(os.path.dirname(path)))
            out.setdefault(pid, []).append(r)
    return out


def med(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return st.median(vals) if vals else None


def fmt(v, digits=1):
    if v is None:
        return "—"
    if isinstance(v, float) and (abs(v) < 0.01 and v != 0):
        return f"{v:.4g}"
    if float(v).is_integer():
        return f"{int(v)}"
    return f"{v:.{digits}f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", help="comma-separated run block dirs")
    ap.add_argument("--md")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    runs = collect([p for p in args.run_dirs.split(",") if p])
    order = ["N1", "N2", "L1", "L2", "L3", "L4", "L5", "P1", "P2", "P3"]
    pids = [p for p in order if p in runs] + [p for p in runs if p not in order]
    lines = ["| point | reps | " + " | ".join(COLS) + " | note |",
             "|---|---|" + "---|" * len(COLS) + "---|"]
    payload = {}
    for pid in pids:
        rs = runs[pid]
        ok = [r for r in rs if (r.get("meta") or {}).get("status") in ("ok", "dirty")]
        meds = {c: med([(r.get("summary") or {}).get(c) for r in ok]) for c in COLS}
        spread = [ (r.get("summary") or {}).get("mbps") for r in ok ]
        notes = sorted({n for r in ok for n in ((r.get("meta") or {}).get("notes") or [])})
        statuses = sorted({(r.get("meta") or {}).get("status", "?") for r in rs})
        note = "; ".join(notes) if notes else ""
        if any(s not in ("ok", "dirty") for s in statuses):
            note = ("statuses=" + ",".join(statuses) + "; " + note).strip("; ")
        row = [pid, f"{len(ok)}/{len(rs)}"] + [fmt(meds[c]) for c in COLS] + [note]
        lines.append("| " + " | ".join(row) + " |")
        payload[pid] = {"median": meds, "mbps_all": spread, "notes": notes,
                        "statuses": statuses, "n": len(ok)}
    text = "\n".join(lines)
    print(text)
    if args.md:
        with open(args.md, "w") as fh:
            fh.write(text + "\n")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
