#!/usr/bin/env python3
"""relay_report.py — fold a sweep run block + relay sampler into fan-out numbers.

    python3 scripts/relay_report.py runs/<block> --sampler relay-sampler.csv \
        --out relay-report.json [--markdown relay-report.md]

For every point/rep it reports:
  pace_mbps        publisher offered rate (result.json summary.pub_mbps)
  sub_mbps         per-subscriber delivered Mbps, parsed from each sub*.log
  sub_mbps_total   sum over subscribers (what the relay had to egress)
  ingress_mbps     relay QUIC bytesRead delta / wall time inside the window
  egress_mbps      relay QUIC bytesWritten delta / wall time inside the window
  fanout_ratio     egress_mbps / ingress_mbps (≈ subscriber count when nothing
                   is dropped; < N means the relay or NIC is the wall)
  relay_cpu_cores  relay CPU seconds / wall time inside the same window
  nic_tx/rx_mbps   host NIC counters as an independent cross-check

The window is [t_end-55, t_end-5] where t_end is the newest subscriber log
mtime, i.e. the steady part of the measurement window with the SIGINT teardown
excluded. Counters are monotonic; a relay restart mid-window (pid change) splits
the segments and only same-pid deltas are differenced.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

INTERVAL_RE = re.compile(r"^\s*\d+-\d+s\s+.*?(\d+(?:\.\d+)?)Mbps")
THROUGHPUT_RE = re.compile(r"Throughput:\s+(\d+(?:\.\d+)?)\s*Mbps")
OBJECTS_RE = re.compile(r"Objects:\s+([\d,]+)")
SUMMARY_RE = {
    "loss_pct": re.compile(r"Lost:\s+\d+\s+\((\d+(?:\.\d+)?)%\)"),
    "ooo": re.compile(r"Out-of-order:\s+(\d+)"),
}
NUMERIC = ("cpu_s", "q_bytes_read", "q_bytes_written", "q_conns", "q_streams",
           "moq_sessions", "pub_subs", "sub_streams", "nic_tx", "nic_rx")


def parse_utc(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sub_stats(path: Path) -> dict:
    """Mean interval bitrate (fallback: whole-run Throughput for short reps)."""
    rates, loss, ooo = [], None, None
    thr, objs = None, None
    for line in path.read_text(errors="replace").splitlines():
        m = THROUGHPUT_RE.search(line)
        if m:
            thr = float(m.group(1))
        m = OBJECTS_RE.search(line)
        if m:
            objs = int(m.group(1).replace(",", ""))
        m = INTERVAL_RE.match(line)
        m = INTERVAL_RE.match(line)
        if m:
            rates.append(float(m.group(1)))
            continue
        for key, rx in SUMMARY_RE.items():
            mm = rx.search(line)
            if mm:
                if key == "loss_pct":
                    loss = float(mm.group(1))
                else:
                    ooo = int(mm.group(1))
    mean = round(statistics.fmean(rates), 2) if rates else None
    return {"log": path.name, "mbps": mean, "throughput_mbps": thr,
            "burst_mbps": thr if mean is None else None, "objects": objs,
            "intervals": len(rates), "loss_pct": loss, "ooo": ooo}


def window_rows(rows: list[dict], t0: float, t1: float) -> list[dict]:
    out = []
    for r in rows:
        t = fnum(r["t_unix"])
        if t is not None and t0 <= t <= t1:
            out.append(r)
    return out


def relay_delta(rows: list[dict]) -> dict:
    """Difference monotonic counters per relay pid; ignore resets."""
    res = {"relay_cpu_s": 0.0, "q_bytes_read": 0.0, "q_bytes_written": 0.0,
           "nic_tx": 0.0, "nic_rx": 0.0, "pids": []}
    by_pid: dict[str, list[dict]] = {}
    for r in rows:
        pid = (r.get("relay_pid") or "").strip()
        if pid:
            by_pid.setdefault(pid, []).append(r)
    for pid, seg in by_pid.items():
        if len(seg) < 2:
            continue
        res["pids"].append(pid)
        for key, out_key in (("cpu_s", "relay_cpu_s"), ("q_bytes_read", "q_bytes_read"),
                             ("q_bytes_written", "q_bytes_written"),
                             ("nic_tx", "nic_tx"), ("nic_rx", "nic_rx")):
            a, b = fnum(seg[0][key]), fnum(seg[-1][key])
            if a is None or b is None:
                continue
            d = b - a
            if d < 0:  # counter reset inside a pid segment: skip this segment
                continue
            res[out_key] += d
    return res


def gauge(rows: list[dict], key: str, agg="max"):
    vals = [fnum(r[key]) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return max(vals) if agg == "max" else round(statistics.fmean(vals), 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("block")
    ap.add_argument("--sampler", default="relay-sampler.csv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--markdown", default=None)
    ap.add_argument("--pre", type=float, default=55.0)
    ap.add_argument("--post", type=float, default=5.0)
    args = ap.parse_args()

    block = Path(args.block)
    rows = list(csv.DictReader(Path(args.sampler).open())) if Path(args.sampler).exists() else []
    reports = []
    for result_path in sorted(block.glob("*/rep-*/result.json")):
        rep_dir = result_path.parent
        doc = json.loads(result_path.read_text())
        summary = doc.get("summary") or {}
        subs = sorted(q for q in rep_dir.glob("sub*.log") if re.fullmatch(r"sub\d+\.log", q.name))
        fanout_path = rep_dir / "fanout.json"
        if not subs:
            continue
        t_end = max(p.stat().st_mtime for p in subs)
        el = summary.get("elapsed_s")
        try:
            el = float(el)
        except (TypeError, ValueError):
            el = None
        if el is not None and el < 10:
            t0, t1 = t_end - el - 4.0, t_end + 1.0   # short rep: burst at the start
        else:
            t0, t1 = t_end - args.pre, t_end - args.post
        win = window_rows(rows, t0, t1)
        delta = relay_delta(win)
        span = max(1e-9, (fnum(win[-1]["t_unix"]) - fnum(win[0]["t_unix"]))) if len(win) > 1 else 0.0
        sub_vals = {p.stem: sub_stats(p) for p in subs}
        per_sub = [v["mbps"] for v in sub_vals.values() if v["mbps"] is not None]
        bursts = [v["burst_mbps"] for v in sub_vals.values() if v["burst_mbps"] is not None]
        if not per_sub and bursts:
            per_sub = bursts
        rec = {
            "point": doc.get("point_id"),
            "rep": doc.get("rep") or (doc.get("meta") or {}).get("rep"),
            "status": (doc.get("meta") or {}).get("status"),
            "n_sub_logs": len(subs),
            "fanout_declared": None,
            "pace_mbps": summary.get("pub_mbps"),
            "obj_s": summary.get("obj_s"),
            "sub_mbps": {k: v["mbps"] for k, v in sub_vals.items()},
            "objects_seen": {k: v["objects"] for k, v in sub_vals.items()},
            "short_run": bool(el is not None and el < 0.8 * float(summary.get("duration_s") or 60)),
            "elapsed_s": el,
            "sub_mbps_mean": round(statistics.fmean(per_sub), 2) if per_sub else None,
            "sub_mbps_total": round(sum(per_sub), 2) if per_sub else None,
            "sub_mbps_min": min(per_sub) if per_sub else None,
            "sub_mbps_max": max(per_sub) if per_sub else None,
            "loss_pct_max": max([v["loss_pct"] for v in sub_vals.values()
                                 if v["loss_pct"] is not None], default=None),
            "ooo_total": sum(v["ooo"] or 0 for v in sub_vals.values()),
            "relay_cpu_cores": round(delta["relay_cpu_s"] / span, 3) if span else None,
            "cpu_pub": summary.get("cpu_pub"),
            "cpu_sub_mean": summary.get("cpu_sub"),
            "ingress_mbps": round(delta["q_bytes_read"] * 8 / span / 1e6, 1) if span else None,
            "egress_mbps": round(delta["q_bytes_written"] * 8 / span / 1e6, 1) if span else None,
            "nic_tx_mbps": round(delta["nic_tx"] * 8 / span / 1e6, 1) if span else None,
            "nic_rx_mbps": round(delta["nic_rx"] * 8 / span / 1e6, 1) if span else None,
            "sampler_rows": len(win),
            "sampler_span_s": round(span, 1),
        }
        if rec["ingress_mbps"] and rec["egress_mbps"]:
            rec["fanout_ratio"] = round(rec["egress_mbps"] / rec["ingress_mbps"], 2)
        if fanout_path.exists():
            f = json.loads(fanout_path.read_text())
            rec["fanout_declared"] = f.get("count")
            rec["joins_s"] = [f.get("first_join_s"), f.get("last_join_s")]
            rec["active_sessions_peak"] = gauge(win, "moq_sessions")
            rec["active_conns_peak"] = gauge(win, "q_conns")
            rec["pub_subs_peak"] = gauge(win, "pub_subs")
            rec["sub_streams_peak"] = gauge(win, "sub_streams")
        reports.append(rec)

    doc = {"block": str(block), "sampler": args.sampler, "reps": reports}
    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2))
    json.dump(doc, sys.stdout, indent=2)
    print()
    if args.markdown:
        lines = ["| point | rep | subs | pace Mb/s | sub mean | sub total | egress Mb/s "
                 "| ingress Mb/s | fan-out | relay cores | status |",
                 "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in reports:
            lines.append(
                f"| {r['point']} | {r['rep']} | {r['n_sub_logs']} | {r['pace_mbps']} "
                f"| {r['sub_mbps_mean']} | {r['sub_mbps_total']} | {r['egress_mbps']} "
                f"| {r['ingress_mbps']} | {r.get('fanout_ratio')} | {r['relay_cpu_cores']} "
                f"| {r['status']} |")
        Path(args.markdown).write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
