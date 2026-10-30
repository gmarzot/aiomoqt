#!/usr/bin/env python3
"""Validate result.json files against PROGRAM.md Appendix A (plus the honesty rules).

    validate_result.py [--allow-incomplete] PATH [PATH...]     exit 0 only if every file is valid

Beyond key presence and types:

* Hard rule 2 -- a numeric field may be ``null`` only when ``summary.not_measured``
  names it with a reason. A made-up zero is never acceptable, and neither is a
  silent null.
* Hard rule 1 -- ``meta.run_id``, ``env.fingerprint`` and a git SHA
  (``shas.aiomoqt``) must be present; the aiopquic / picoquic SHAs must be real.
* Unless ``--allow-incomplete``, ``meta.status`` must be ``ok`` or ``dirty`` and the
  summary/samples populated (an aborted point is a valid *record*, not a measurement).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

NUM = (int, float)
TOP = {"point_id": str, "arm": str, "topology": str, "transport": str, "draft": int,
       "config": dict, "hosts": dict, "shas": dict, "env": dict, "started_utc": str,
       "duration_s": NUM, "samples": list, "summary": dict, "artifacts": list, "meta": dict}
CONFIG = ("P", "s", "g", "r", "t")
HOSTS = ("relay", "load", "subs")
SHAS = ("aiomoqt", "aiopquic", "picoquic")
ENV = ("cc", "jemalloc", "gso", "qdisc", "pinning", "clock_offset_us")
SAMPLE = ("t_s", "mbps", "p50", "p99")
SUMMARY = ("mbps", "obj_s", "grp_s", "p50", "p95", "p99", "p99_9", "jitter_ms", "lost", "ooo",
           "interval_cv", "cpu_pub", "cpu_sub", "rss_max_mb")
META = ("status", "run_id", "rep", "arm", "point_argv", "hostcheck_pre_local",
        "hostcheck_post_local", "config_fingerprint")
COMPLETE = ("ok", "dirty")


def _isnum(v) -> bool:
    return isinstance(v, NUM) and not isinstance(v, bool)


def validate(doc: dict, require_complete: bool = True) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["top level is not an object"]

    for key, typ in TOP.items():
        if key not in doc:
            errs.append(f"{key}: missing")
        elif typ is NUM:
            if not _isnum(doc[key]):
                errs.append(f"{key}: expected number")
        elif not isinstance(doc[key], typ) or (typ is int and isinstance(doc[key], bool)):
            errs.append(f"{key}: expected {typ.__name__}")
    if errs:
        return errs

    meta, env, summary, shas = doc["meta"], doc["env"], doc["summary"], doc["shas"]
    nm = summary.get("not_measured") or {}

    for k in CONFIG:
        if k not in doc["config"]:
            errs.append(f"config.{k}: missing")
    for k in HOSTS:
        if k not in doc["hosts"]:
            errs.append(f"hosts.{k}: missing")
    for k in META:
        if k not in meta:
            errs.append(f"meta.{k}: missing")

    # hard rule 1: run id + env fingerprint + SHAs
    if not meta.get("run_id"):
        errs.append("meta.run_id: empty (a number without a run id does not exist)")
    fp = env.get("fingerprint")
    if not isinstance(fp, dict) or not fp:
        errs.append("env.fingerprint: missing/empty (a number without an env fingerprint does not exist)")
    for k in SHAS:
        v = shas.get(k)
        if not v or v == "unknown":
            errs.append(f"shas.{k}: missing/unknown (a number without a git SHA does not exist)")

    for k in ENV:
        if k not in env:
            errs.append(f"env.{k}: missing")
    for k in ("jemalloc", "gso"):
        if k in env and not isinstance(env[k], bool):
            errs.append(f"env.{k}: expected bool")
    if "clock_offset_us" in env and env["clock_offset_us"] is None:
        why = ((meta.get("clock") or {}).get("latency_why"))
        if not why:
            errs.append("env.clock_offset_us: null without meta.clock.latency_why")
    elif "clock_offset_us" in env and not _isnum(env["clock_offset_us"]):
        errs.append("env.clock_offset_us: expected number or null")

    status = meta.get("status")
    if require_complete:
        if status not in COMPLETE:
            errs.append(f"meta.status={status!r}: not a completed measurement "
                        f"(reason: {meta.get('reason')})")
        if not doc["samples"]:
            errs.append("samples: empty")

    complete = status in COMPLETE
    for k in SUMMARY:
        if k not in summary:
            if complete or require_complete:
                errs.append(f"summary.{k}: missing")
        elif summary[k] is None:
            if k not in nm:
                errs.append(f"summary.{k}: null without a not_measured reason")
        elif not _isnum(summary[k]):
            errs.append(f"summary.{k}: expected number or null")

    for i, row in enumerate(doc["samples"]):
        for k in SAMPLE:
            if k not in row:
                errs.append(f"samples[{i}].{k}: missing")
            elif row[k] is None:
                gate = "samples.p50" if k == "p50" else "p99"
                if k in ("p50", "p99") and (gate in nm or "p99" in nm and k == "p99"):
                    continue
                errs.append(f"samples[{i}].{k}: null without a not_measured reason")
            elif not _isnum(row[k]):
                errs.append(f"samples[{i}].{k}: expected number or null")
    return errs


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    allow = "--allow-incomplete" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    bad = 0
    for p in paths:
        try:
            doc = json.loads(Path(p).read_text())
        except (OSError, ValueError) as exc:
            print(f"FAIL {p}: {exc}")
            bad += 1
            continue
        errs = validate(doc, require_complete=not allow)
        if errs:
            bad += 1
            print(f"FAIL {p}")
            for e in errs:
                print(f"     - {e}")
        else:
            st = (doc.get("meta") or {}).get("status")
            print(f"OK   {p}  status={st}  point={doc.get('point_id')}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
