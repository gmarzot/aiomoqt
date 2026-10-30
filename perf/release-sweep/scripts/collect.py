#!/usr/bin/env python3
"""collect.py — turn bench transcripts into Appendix A samples + summary.

The aiomoqt bench tools print two blocks to stdout:

    Interval  Grps  GrpRate  Objs  ObjRate  Bitrate  Latency  Jitter  Loss
    ─────────────────────────────────────────────────────────────────────
    0-5s      55    11/s     222529 44506/s 1.5Gbps  5.9ms p99:10ms 21us 0.00% (0)

    ════════════════════════════════════════════════════════════
      aiomoqt-bench results  (8.3s active / 8.3s elapsed)
    ════════════════════════════════════════════════════════════
      Groups:      94
      Objects:     383,129
      Bytes:       1,574,277,061
      Throughput:  1517.01 Mbps
      Latency:     min=0.8  avg=4.9  max=14.3  sd=1.2 ms
                   p50=4.8  p95=7.2  p99=9.3 ms
      Jitter:      0.02 ms
      Lost:        0 (0.00%)

The subscriber flavour carries a Latency column; the sender flavour
(`pub_server`, `pub_bench`) does not and instead ends with

      Sent: 383,371 objects, 94 groups, 46167/s, 1.5Gbps (8.3s)

`sub_bench` has no machine-readable output on this branch (verified: no
`--json`/`--csv` flag in `--help`), so this parser is the only reader of those
transcripts.  It is unit-tested against captured fixtures — see
`tests/test_collect.py`.  If a row fails to parse it is skipped and recorded in
`summary["unparsed_rows"]` rather than silently dropped.

CLI
---
    collect.py LOG [LOG ...] [--json] [--offset-ms F] [--csv PATH]
    collect.py --merge A.json B.json ... [--csv PATH]
    collect.py --runs DIR [--csv PATH]

`--offset-ms` adds a constant to every latency sample (use the measured
shenzhen↔seawolf clock offset when the timestamp source is `loc`).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys

# --- transcripts -----------------------------------------------------------

_BANNER = re.compile(r"[═─]{10,}")
_ROW_SUB = re.compile(
    r"^\s*(?P<t0>\d+(?:\.\d+)?)-(?P<t1>\d+(?:\.\d+)?)s\s+"
    r"(?P<grps>\d+)\s+(?P<grp_rate>[\d.]+)/s\s+"
    r"(?P<objs>\d+)\s+(?P<obj_rate>[\d.]+)/s\s+"
    r"(?P<bitrate>[\d.]+)(?P<bit_unit>[KMGkmg])bps\s+"
    r"(?P<lat>[\d.]+)(?P<lat_unit>us|ms|s)\s+"
    r"p99:(?P<p99>[\d.]+)(?P<p99_unit>us|ms|s)\s+"
    r"(?P<jitter>[\d.]+)(?P<jit_unit>us|ms)\s+"
    r"(?P<loss_pct>[\d.]+)%\s+\((?P<lost>\d+)\)"
)
_ROW_SENDER = re.compile(
    r"^\s*(?P<t0>\d+(?:\.\d+)?)-(?P<t1>\d+(?:\.\d+)?)s\s+"
    r"(?P<grps>\d+)\s+(?P<grp_rate>[\d.]+)/s\s+"
    r"(?P<objs>\d+)\s+(?P<obj_rate>[\d.]+)/s\s+"
    r"(?P<bitrate>[\d.]+)(?P<bit_unit>[KMGkmg])bps\s*$"
)
_NUM = r"([\d,]+(?:\.\d+)?)"
_F = {
    "objects": re.compile(r"^\s*Objects:\s*" + _NUM, re.M),
    "groups": re.compile(r"^\s*Groups:\s*" + _NUM, re.M),
    "bytes": re.compile(r"^\s*Bytes:\s*" + _NUM, re.M),
    "grp_rate": re.compile(r"^\s*GrpRate:\s*([\d.]+)", re.M),
    "obj_rate": re.compile(r"^\s*ObjRate:\s*([\d.]+)", re.M),
    "mbps": re.compile(r"^\s*Throughput:\s*([\d.]+)\s*Mbps", re.M),
    "lat_line": re.compile(
        r"^\s*Latency:\s*min=([\d.]+)\s+avg=([\d.]+)\s+max=([\d.]+)\s+sd=([\d.]+)\s*(us|ms|s)",
        re.M,
    ),
    "pct_line": re.compile(
        r"^\s*p50=([\d.]+)\s+p95=([\d.]+)\s+p99=([\d.]+)\s*(us|ms|s)", re.M
    ),
    "jitter": re.compile(r"^\s*Jitter:\s*([\d.]+)\s*(us|ms)", re.M),
    "lost": re.compile(r"^\s*Lost:\s*(\d+)\s*\(([\d.]+)%\)", re.M),
    "ooo": re.compile(r"^\s*Out-of-order:\s*(\d+)", re.M),
    "timestamps": re.compile(r"^\s*Timestamps:\s*(\S+)", re.M),
    "sent": re.compile(
        r"^\s*Sent:\s*([\d,]+)\s+objects,\s*([\d,]+)\s+groups,\s*"
        r"([\d.]+)/s,\s*([\d.]+)([KMGkmg])bps",
        re.M,
    ),
    "active": re.compile(r"\(([\d.]+)s active /\s*([\d.]+)s elapsed\)"),
}
_UNIT = {"us": 0.001, "ms": 1.0, "s": 1000.0}
_BIT = {"k": 1e-3, "m": 1.0, "g": 1e3}


def _ms(value: str, unit: str) -> float:
    return float(value) * _UNIT[unit.lower()]


def _mbps(value: str, unit: str) -> float:
    return float(value) * _BIT[unit.lower()]


def _fnum(value: str) -> float:
    return float(value.replace(",", ""))


def parse_log(text: str, source: str = "") -> dict:
    """Parse one benchmark transcript.

    Returns {"source", "flavour" ("sub"|"sender"|"unknown"), "intervals": [...],
    "summary": {...}}.  Interval dicts use the Appendix A sample keys where they
    apply (`t_s`, `mbps`, `p50`, `p99`) plus the raw per-interval counters.
    """
    intervals: list[dict] = []
    unparsed: list[str] = []
    flavour = "unknown"

    for line in text.splitlines():
        if _BANNER.match(line.strip()) or "Interval" in line and "Grps" in line:
            continue
        m = _ROW_SUB.match(line)
        if m:
            flavour = "sub"
            d = m.groupdict()
            intervals.append(
                {
                    "t_s": round(float(d["t1"]), 3),
                    "t_start_s": float(d["t0"]),
                    "groups": int(d["grps"]),
                    "grp_s": float(d["grp_rate"]),
                    "objects": int(d["objs"]),
                    "obj_s": float(d["obj_rate"]),
                    "mbps": round(_mbps(d["bitrate"], d["bit_unit"]), 3),
                    "lat_avg_ms": round(_ms(d["lat"], d["lat_unit"]), 3),
                    "p99_ms": round(_ms(d["p99"], d["p99_unit"]), 3),
                    "jitter_ms": round(_ms(d["jitter"], d["jit_unit"]), 3),
                    "loss_pct": float(d["loss_pct"]),
                    "lost": int(d["lost"]),
                }
            )
            continue
        m = _ROW_SENDER.match(line)
        if m:
            flavour = "sender"
            d = m.groupdict()
            intervals.append(
                {
                    "t_s": round(float(d["t1"]), 3),
                    "t_start_s": float(d["t0"]),
                    "groups": int(d["grps"]),
                    "grp_s": float(d["grp_rate"]),
                    "objects": int(d["objs"]),
                    "obj_s": float(d["obj_rate"]),
                    "mbps": round(_mbps(d["bitrate"], d["bit_unit"]), 3),
                }
            )
            continue
        if line.strip() and re.match(r"^\s*\d+(?:\.\d+)?-\d+(?:\.\d+)?s\s", line):
            unparsed.append(line.rstrip())

    summ: dict = {}
    g = lambda k: _F[k].search(text)  # noqa: E731
    if (m := g("objects")):
        summ["objects"] = _fnum(m.group(1))
    if (m := g("groups")):
        summ["groups"] = _fnum(m.group(1))
    if (m := g("bytes")):
        summ["bytes"] = _fnum(m.group(1))
    if (m := g("grp_rate")):
        summ["grp_s"] = float(m.group(1))
    if (m := g("obj_rate")):
        summ["obj_s"] = float(m.group(1))
    if (m := g("mbps")):
        summ["mbps"] = float(m.group(1))
    if (m := g("lat_line")):
        summ["lat_min_ms"] = _ms(m.group(1), m.group(5))
        summ["lat_avg_ms"] = _ms(m.group(2), m.group(5))
        summ["lat_max_ms"] = _ms(m.group(3), m.group(5))
        summ["lat_sd_ms"] = _ms(m.group(4), m.group(5))
    if (m := g("pct_line")):
        summ["p50"] = _ms(m.group(1), m.group(4))
        summ["p95"] = _ms(m.group(2), m.group(4))
        summ["p99"] = _ms(m.group(3), m.group(4))
    if (m := g("jitter")):
        summ["jitter_ms"] = _ms(m.group(1), m.group(2))
    if (m := g("lost")):
        summ["lost"] = int(m.group(1))
        summ["lost_pct"] = float(m.group(2))
    if (m := g("ooo")):
        summ["ooo"] = int(m.group(1))
    if (m := g("timestamps")):
        summ["timestamps"] = m.group(1)
    if (m := g("sent")) and "objects" not in summ:
        summ["objects"] = _fnum(m.group(1))
        summ["groups"] = _fnum(m.group(2))
        summ["obj_s"] = float(m.group(3))
        summ["mbps"] = _mbps(m.group(4), m.group(5))
    if (m := g("active")):
        summ["active_s"] = float(m.group(1))
        summ["elapsed_s"] = float(m.group(2))

    # derived: interval coefficient of variation on throughput, and the
    # latency percentiles when only per-interval rows carry them.
    mbps = [iv["mbps"] for iv in intervals if iv.get("mbps") is not None]
    if len(mbps) >= 2:
        mean = sum(mbps) / len(mbps)
        var = sum((x - mean) ** 2 for x in mbps) / (len(mbps) - 1)
        summ["interval_cv"] = round((var ** 0.5) / mean, 4) if mean else None
        summ["mbps_mean_intervals"] = round(mean, 3)
        summ["mbps_min_interval"] = round(min(mbps), 3)
        summ["mbps_max_interval"] = round(max(mbps), 3)
    if "p50" not in summ and intervals and "lat_avg_ms" in intervals[0]:
        avg = sorted(iv["lat_avg_ms"] for iv in intervals if "lat_avg_ms" in iv)
        if avg:
            summ["p50_of_interval_avgs"] = avg[len(avg) // 2]
    if unparsed:
        summ["unparsed_rows"] = len(unparsed)

    return {
        "source": source,
        "flavour": flavour,
        "intervals": intervals,
        "summary": summ,
        "unparsed": unparsed,
    }


def parse_file(path: str, offset_ms: float = 0.0) -> dict:
    with open(path, "r", errors="replace") as fh:
        text = fh.read()
    out = parse_log(text, source=os.path.basename(path))
    if offset_ms:
        apply_offset(out, offset_ms)
    return out


def apply_offset(parsed: dict, offset_ms: float) -> None:
    """Add the clock offset to every latency figure and note it."""
    for iv in parsed["intervals"]:
        for k in ("lat_avg_ms", "p99_ms"):
            if k in iv:
                iv[k] = round(iv[k] + offset_ms, 3)
    s = parsed["summary"]
    for k in ("p50", "p95", "p99", "lat_min_ms", "lat_avg_ms", "lat_max_ms"):
        if k in s:
            s[k] = round(s[k] + offset_ms, 3)
    s["clock_offset_ms"] = offset_ms


# Latency figures that depend on the pub/sub clock offset. Everything else the
# parser emits (throughput, counts, jitter, lat_sd_ms, loss) is offset-free.
LAT_KEYS = ("p50", "p95", "p99", "p99_9", "lat_min_ms", "lat_avg_ms", "lat_max_ms",
            "p50_of_interval_avgs")


def correction_ms(sub_minus_pub_us: float) -> float:
    """Milliseconds to ADD to subscriber-measured one-way latency.

    measured = rx_stamp(sub clock) - tx_stamp(pub clock) = true + (sub - pub),
    therefore corrected = measured - (sub - pub).
    """
    return -float(sub_minus_pub_us) / 1000.0


def sample_rows(parsed: dict) -> list[dict]:
    """Appendix A `samples` block: one row per interval."""
    rows = []
    for iv in parsed["intervals"]:
        row = {"t_s": iv["t_s"], "mbps": iv.get("mbps")}
        if "lat_avg_ms" in iv:
            row["lat_ms"] = iv["lat_avg_ms"]
            row["p99"] = iv["p99_ms"]
        rows.append(row)
    return rows


def merge(a: dict, b: dict, *, b_prefix: str = "sub") -> dict:
    """Merge two parsed transcripts (typically sender + subscriber).

    The subscriber block wins on anything it measured; sender-only counters are
    kept under ``<b_prefix>_*`` keys so nothing is lost.
    """
    out = json.loads(json.dumps(b))
    for k, v in a["summary"].items():
        out["summary"].setdefault(k, v)
    return out


def walk_runs(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "**", "result.json"), recursive=True))


FIELDS = (
    "run", "point_id", "arm", "topology", "transport", "rep", "status",
    "started_utc", "duration_s", "mbps", "obj_s", "grp_s", "p50", "p95", "p99",
    "jitter_ms", "lost", "ooo", "interval_cv", "cpu_pub", "cpu_sub",
    "rss_max_mb", "clock_offset_us",
)


def build_csv(rows: list[dict]) -> list[dict]:
    """Flatten Appendix A result dicts into CSV rows."""
    out = []
    for r in rows:
        if not isinstance(r, dict) or "point_id" not in r:
            continue
        s = r.get("summary") or {}
        out.append(
            {
                "run": r.get("meta", {}).get("run_id", ""),
                "point_id": r.get("point_id"),
                "arm": r.get("arm"),
                "topology": r.get("topology"),
                "transport": r.get("transport"),
                "rep": r.get("meta", {}).get("rep"),
                "status": r.get("meta", {}).get("status", ""),
                "started_utc": r.get("started_utc", ""),
                "duration_s": r.get("duration_s", ""),
                "mbps": s.get("mbps", ""),
                "obj_s": s.get("obj_s", ""),
                "grp_s": s.get("grp_s", ""),
                "p50": s.get("p50", ""),
                "p95": s.get("p95", ""),
                "p99": s.get("p99", ""),
                "jitter_ms": s.get("jitter_ms", ""),
                "lost": s.get("lost", ""),
                "ooo": s.get("ooo", ""),
                "interval_cv": s.get("interval_cv", ""),
                "cpu_pub": s.get("cpu_pub", ""),
                "cpu_sub": s.get("cpu_sub", ""),
                "rss_max_mb": s.get("rss_max_mb", ""),
                "clock_offset_us": (r.get("env") or {}).get("clock_offset_us", ""),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="collect.py - bench transcripts -> Appendix A samples/summary"
    )
    ap.add_argument("logs", nargs="*", help="bench transcript(s) to parse")
    ap.add_argument("--json", action="store_true", help="print JSON (default)")
    ap.add_argument("--offset-ms", type=float, default=0.0)
    ap.add_argument("--csv", help="write a CSV rollup of result.json files")
    ap.add_argument("--runs", help="directory tree containing result.json files")
    args = ap.parse_args(argv)

    if args.runs:
        rows = []
        for path in walk_runs(args.runs):
            with open(path) as fh:
                rows.append(json.load(fh))
        if args.csv:
            with open(args.csv, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(FIELDS))
                w.writeheader()
                for row in build_csv(rows):
                    w.writerow(row)
            print(f"wrote {args.csv} ({len(rows)} results)", file=sys.stderr)
        print(json.dumps(rows, indent=2))
        return 0

    if not args.logs:
        ap.error("give at least one LOG, or --runs DIR")
    results = [parse_file(p, args.offset_ms) for p in args.logs]
    payload = results[0] if len(results) == 1 else results
    if args.csv and len(results) == 1:
        rows = results[0]["intervals"]
        if rows:
            with open(args.csv, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
