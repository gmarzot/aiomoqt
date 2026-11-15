#!/usr/bin/env python3
"""Emit result.json for a load_sim point rep (C0/C1) from its load.log.

The sweep's collector crashed after the run (NameError, fixed 2026-11-15), so
these reps have a complete load.log but no summary. This rebuilds the summary
from the same parse the collector uses, plus interval-series stats.
"""
import json, os, re, sys, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect

BLOCK = "runs/2026-11-11T02-33-51Z"
RE_IV = re.compile(r"T\+\s*(\d+)s\s+subs=(\d+)\s+\(\+(\d+)/-(\d+)/x(\d+)\)\s+rx=(\d+)Mbps\s+p90=(\d+)ms")

def iv_rows(text):
    rows = []
    for line in text.splitlines():
        m = RE_IV.search(line)
        if m:
            t, subs, j, c, x, rx, p90 = (int(g) for g in m.groups())
            rows.append(dict(t=t, subs=subs, joins=j, churn=c, exodus=x, rx=rx, p90=p90))
    return rows

def main(point, rep):
    d = f"{BLOCK}/{point}/{rep}"
    text = open(f"{d}/load.log", errors="replace").read()
    summary, rows = collect.parse_load_sim(text, f"{d}/churn.csv")
    iv = iv_rows(text)
    live = [r for r in iv if r["subs"] > 0]
    steady = [r for r in live if r["t"] >= 15]
    if steady:
        rx = [r["rx"] for r in steady]
        p90 = [r["p90"] for r in steady]
        summary.update(
            mbps=round(st.mean(rx), 2),
            mbps_mean_intervals=round(st.mean(rx), 2),
            interval_cv=round(st.pstdev(rx) / st.mean(rx), 4) if st.mean(rx) else None,
            p90_mean=round(st.mean(p90), 1),
            p90_max=max(p90),
            intervals_used=len(steady),
        )
    summary["intervals_total"] = len(iv)
    summary["subs_series_tail"] = [r["subs"] for r in iv][-6:]
    out = dict(point_id=point, arm="base-070rc2", topology="C", transport="raw",
               draft=16, duration_s=300,
               config=dict(slots=50, rate_mbps=4, offered_mbps=200, interval_s=5,
                           seed=1),
               summary=summary, status="ok",
               provenance="rebuilt from load.log 2026-11-15 (collector NameError; "
                          "run itself completed)")
    json.dump(out, open(f"{d}/result.built.json", "w"), indent=1)
    print(f"{point}/{rep} mbps={summary.get('mbps')} cv={summary.get('interval_cv')} "
          f"peak={summary.get('peak_subs')} joins={summary.get('joins')} "
          f"churns={summary.get('churns')} exodus={summary.get('exodus')} "
          f"delivery_frac={summary.get('delivery_frac')} result={summary.get('result')}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
