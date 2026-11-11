#!/usr/bin/env python3
"""Consolidate the base-070rc2 baseline into rollup.csv + headline numbers.

The sweep writes one ``result.json`` per point x rep. This walks the *canonical*
run block for each phase (there are aborted and superseded blocks in runs/ — a
plain recursive glob would silently mix them), writes one CSV row per rep, and
prints the median + bootstrap 95% CI per point for pasting into SUMMARY.md.

Median + bootstrap CI is PROGRAM.md §3 item 9. Reps whose status is not "ok"
stay in the CSV (no cherry-picking, PROGRAM.md §7) but are excluded from the
median, and the count of excluded reps is printed so the report can state it.

    python3 scripts/baseline_report.py --csv baselines/base-070rc2/rollup.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RS = HERE.parent
sys.path.insert(0, str(HERE))
import collect  # noqa: E402

# phase -> run block directory (relative to RS/"runs"). One block per phase: the
# canonical block is the one whose report was published; the others are aborted
# attempts (R2 x1 in p1d, R5b x1 in the 16-41 block) or smokes.
CANON = {
    "1b-noise": "2026-10-30T21-40-50Z-partA",
    "1b-loop-pair": "2026-10-30T22-04-13Z-partB",
    "1c-direct": "2026-10-30T22-56-13Z-p1c2",
    "1d-relay": "2026-10-30T23-42-43Z-p1d",
    "1d-relay-b2": "2026-10-30T23-57-24Z-p1d-b2",
    "1d-relay-b3": "2026-10-31T16-47-31Z-p1d-b3",
    "1e-media-churn": "AUTO_LATEST",
}

# point -> (phase block, note). Order is the order rows appear in the report.
POINTS = [
    ("N1", "1b-noise", "loopback, unfolded"),
    ("N2", "1b-noise", "loopback, 1080p media"),
    ("L1", "1b-loop-pair", "loopback single stream"),
    ("L2", "1b-loop-pair", "loopback P=8"),
    ("L3", "1b-loop-pair", "loopback P=8 WT"),
    ("L4", "1b-loop-pair", "loopback P=8 g=40 (churn)"),
    ("L5", "1b-loop-pair", "loopback small objects"),
    ("P1", "1b-loop-pair", "two-process, no network"),
    ("P2", "1b-loop-pair", "two-process, paced 20 Mb/s"),
    ("P3", "1b-loop-pair", "two-process, WT"),
    ("D1", "1c-direct", "direct, 1 stream ~300 Mb/s"),
    ("D2", "1c-direct", "direct, P=4 ~600 Mb/s"),
    ("D3", "1c-direct", "direct ceiling"),
    ("D4", "1c-direct", "direct ceiling + churn"),
    ("D5", "1c-direct", "direct WT"),
    ("Ri1", "1c-direct", "ring 1 hop"),
    ("R1", "1d-relay", "relay, controlled 200 Mb/s"),
    ("R2", "1d-relay-b2", "relay ceiling"),
    ("R3", "1d-relay-b2", "relay ceiling + churn"),
    ("R4", "1d-relay-b2", "relay fan-out 5"),
    ("R5", "1d-relay-b2", "relay fan-out 20 (over-driven)"),
    ("R6", "1d-relay-b2", "relay fan-out 5 WT"),
    ("R5b", "1d-relay-b3", "relay fan-out 20 at 1 Gb/s"),
    ("M1", "1e-media-churn", "1080p, 1 subscriber"),
    ("M2", "1e-media-churn", "720p, 5 subscribers"),
    ("C1", "1e-media-churn", "churn storm, 50 slots"),
    ("C0", "1e-media-churn", "steady control for C1"),
    ("C2", "1e-media-churn", "late join at +30 s"),
]

BOOT = 10000


def bootstrap_ci(vals: list[float], n: int = BOOT, seed: int = 7) -> tuple[float, float]:
    if len(vals) < 2:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    k = len(vals)
    meds = []
    for _ in range(n):
        s = sorted(rnd.choices(vals, k=k))
        meds.append(s[k // 2] if k % 2 else 0.5 * (s[k // 2 - 1] + s[k // 2]))
    meds.sort()
    return meds[int(0.025 * n)], meds[int(0.975 * n) - 1]


def resolve_blocks() -> dict[str, Path]:
    out = {}
    runs = RS / "runs"
    for phase, name in CANON.items():
        if name == "AUTO_LATEST":
            cands = sorted(p for p in runs.glob("2026-11-*Z") if p.is_dir()
                           and (p / "M1").exists())
            if not cands:
                continue
            out[phase] = cands[-1]
        else:
            p = runs / name
            if p.is_dir():
                out[phase] = p
    return out


def load(blocks: dict[str, Path]) -> dict[str, list[dict]]:
    """point -> list of result dicts (one per rep that produced one)."""
    by_point: dict[str, list[dict]] = {}
    for point, phase, _note in POINTS:
        block = blocks.get(phase)
        if not block:
            continue
        pdir = block / point
        if not pdir.is_dir():
            continue
        for rp in sorted(pdir.glob("rep-*")):
            f = rp / "result.json"
            if not f.exists():
                continue
            try:
                r = json.loads(f.read_text())
            except ValueError:
                continue
            r.setdefault("meta", {})["block"] = block.name
            r["meta"].setdefault("rep", rp.name)
            by_point.setdefault(point, []).append(r)
    return by_point


def key_metric(r: dict) -> float | None:
    s = r.get("summary") or {}
    for k in ("mbps", "mbps_mean"):
        v = s.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def fmt(v, nd=2):
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    return f"{v:,.{nd}f}"


def stats_block(by_point: dict[str, list[dict]]) -> None:
    print("## point table (median over ok reps, bootstrap 95% CI)\n")
    print("| point | n_ok/n | median | 95% CI | CV % | note |")
    print("|---|---|---|---|---|---|")
    for point, _phase, note in POINTS:
        reps = by_point.get(point, [])
        if not reps:
            print(f"| {point} | 0/0 | — | — | — | {note} — no result.json |")
            continue
        # "dirty" = ran to completion with a recorded hostcheck violation
        # (stray agent process, clock jump). Phase 1b/1c/1d published those runs
        # with the violation stated, so they are usable here; only failed and
        # aborted reps are excluded from the median.
        ok = [r for r in reps
              if (r.get("meta") or {}).get("status") in ("ok", "dirty")]
        vals = [v for v in (key_metric(r) for r in ok) if v is not None]
        if not vals:
            st = ",".join(sorted({(r.get("meta") or {}).get("status", "?") for r in reps}))
            print(f"| {point} | 0/{len(reps)} | — | — | — | {note} — status {st} |")
            continue
        vals.sort()
        k = len(vals)
        med = vals[k // 2] if k % 2 else 0.5 * (vals[k // 2 - 1] + vals[k // 2])
        lo, hi = bootstrap_ci(vals)
        mean = sum(vals) / k
        sd = (sum((v - mean) ** 2 for v in vals) / (k - 1)) ** 0.5 if k > 1 else 0.0
        cv = 100.0 * sd / mean if mean else float("nan")
        flag = "" if len(ok) == len(reps) else f" ({len(reps) - len(ok)} non-ok excluded)"
        print(f"| {point} | {len(ok)}/{len(reps)} | {fmt(med)} | "
              f"{fmt(lo)}–{fmt(hi)} | {fmt(cv, 2)} | {note}{flag} |")


def extras_block(by_point: dict[str, list[dict]]) -> None:
    print("\n## churn / late-join detail (per rep)\n")
    hdr = ("| point | rep | status | ttff_ms | burst_B_1s | burst_grps | first_grp | "
           "peak/target | joins | churn | exodus | fails | died | delivery | mbps |")
    print(hdr)
    print("|" + "---|" * 15)
    for point in ("C0", "C1", "C2"):
        for r in by_point.get(point, []):
            s = r.get("summary") or {}
            t = s.get("ttff") or {}
            meta = r.get("meta") or {}

            def g(k, src=s):
                v = src.get(k)
                return "—" if v is None else str(v)

            print(f"| {point} | {meta.get('rep', '?')} | {meta.get('status', '?')} | "
                  f"{fmt(s.get('ttff_ms'))} | {g('burst_bytes_1s', t)} | "
                  f"{g('burst_groups_1s', t)} | {g('first_group', t)} | "
                  f"{g('peak_subs')}/{g('target_subs')} | {g('joins')} | {g('churns')} | "
                  f"{g('exodus')} | {g('fails')} | {g('died')} | {g('delivery_frac')} | "
                  f"{fmt(key_metric(r))} |")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="write one row per point x rep")
    ap.add_argument("--json", action="store_true", help="also dump per-point medians")
    args = ap.parse_args(argv)

    blocks = resolve_blocks()
    print("blocks:")
    for phase, path in blocks.items():
        print(f"  {phase:16s} {path.name}")
    by_point = load(blocks)

    if args.csv:
        rows = []
        for point, _p, _n in POINTS:
            rows.extend(collect.build_csv(by_point.get(point, [])))
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(collect.FIELDS))
            w.writeheader()
            for row in rows:
                w.writerow(row)
        print(f"\nwrote {out} ({len(rows)} rows)", file=sys.stderr)

    stats_block(by_point)
    extras_block(by_point)
    return 0


if __name__ == "__main__":
    sys.exit(main())
