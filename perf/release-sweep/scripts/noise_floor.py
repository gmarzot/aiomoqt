#!/usr/bin/env python3
"""noise_floor.py — mean/stdev/CV/MDE per metric for a noise-floor run block.

MDE formula (as specified by the P1b card): for a two-sample comparison of
means with n per arm, alpha=0.05 two-sided and 80% power, the minimum
detectable effect is about (z_0.975 + z_0.80) * sqrt(2/n) * sigma
= 2.80 * sqrt(2/n) * sigma.  With CV = sigma/mean this is reported as a
*fraction of the mean*:  MDE(n) = 2.77 * CV / sqrt(n)  (the card's constant,
2.77, is that same expression rounded after using t_0.975,df=4 ~ 2.776 for
small n).  All MDEs below are relative to the measured mean, so a later claim
must show a relative delta larger than the printed percentage.

Usage: noise_floor.py RUN_BLOCK_DIR [--json OUT] [--md OUT]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics as st
import sys

METRICS = ["mbps", "obj_s", "p50", "p99", "interval_cv"]


def load(run_dir: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for path in sorted(glob.glob(os.path.join(run_dir, "*", "rep-*", "result.json"))):
        with open(path) as fh:
            r = json.load(fh)
        pid = (r.get("point_id") or os.path.basename(os.path.dirname(os.path.dirname(path))))
        out.setdefault(pid, []).append(r)
    return out


def stats_block(vals: list[float], n: int) -> dict:
    if len(vals) < 2:
        return {"n": len(vals)}
    mean = st.mean(vals)
    sd = st.stdev(vals)
    cv = sd / mean if mean else float("nan")
    return {
        "n": len(vals),
        "mean": mean,
        "stdev": sd,
        "cv": cv,
        "min": min(vals),
        "max": max(vals),
        "mde_n3": 2.77 * cv / math.sqrt(3),
        "mde_n5": 2.77 * cv / math.sqrt(5),
    }


def analyse(runs: dict[str, list[dict]]) -> dict:
    report = {}
    for pid, results in runs.items():
        ok = [r for r in results if (r.get("meta") or {}).get("status") in ("ok", "dirty")]
        per = {}
        for m in METRICS:
            vals = [float(r["summary"][m]) for r in ok
                    if isinstance(r.get("summary", {}).get(m), (int, float))]
            per[m] = stats_block(vals, len(ok))
        per["_statuses"] = [f"{(r.get('meta') or {}).get('status')}" for r in results]
        per["_notes"] = sorted({n for r in ok for n in ((r.get("meta") or {}).get("notes") or [])})
        per["_lost"] = [r["summary"].get("lost") for r in ok]
        report[pid] = per
    return report


def md_table(report: dict) -> str:
    head = ("| metric | n | mean | stdev | CV % | MDE n=3 % | MDE n=5 % |\n"
            "|---|---|---|---|---|---|---|\n")
    chunks = []
    for pid, per in report.items():
        rows = [f"### {pid}", "", head]
        for m in METRICS:
            s = per[m]
            if "mean" not in s:
                rows.append(f"| {m} | {s.get('n', 0)} | — | — | — | — | — |")
                continue
            rows.append(
                f"| {m} | {s['n']} | {s['mean']:.4g} | {s['stdev']:.4g} | "
                f"{100*s['cv']:.2f} | {100*s['mde_n3']:.2f} | {100*s['mde_n5']:.2f} |")
        rows.append("")
        if per["_notes"]:
            rows.append("notes: " + "; ".join(per["_notes"]))
            rows.append("")
        chunks.append("\n".join(rows))
    return "\n".join(chunks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--json")
    ap.add_argument("--md")
    args = ap.parse_args(argv)
    report = analyse(load(args.run_dir))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2)
    text = md_table(report)
    if args.md:
        with open(args.md, "w") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
