#!/usr/bin/env python3
"""Emit C0/C1 churn statistics from load.log interval series as a markdown table."""
import glob, json, os, re, statistics as st

BLOCK = "runs/2026-11-11T02-33-51Z"
RE_IV = re.compile(r"T\+\s*(\d+)s\s+subs=(\d+)\s+\(\+(\d+)/-(\d+)/x(\d+)\)\s+rx=(\d+)Mbps\s+p90=(\d+)ms")

def parse(path):
    rows = []
    for line in open(path, errors="replace"):
        m = RE_IV.search(line)
        if m:
            t, subs, j, c, x, rx, p90 = (int(g) for g in m.groups())
            rows.append(dict(t=t, subs=subs, joins=j, churn=c, exodus=x, rx=rx, p90=p90))
    return rows

def stats(rows):
    live = [r for r in rows if r["subs"] > 0]
    steady = [r for r in live if r["t"] >= 15]
    rx = [r["rx"] for r in steady]
    p90 = [r["p90"] for r in steady]
    return dict(
        n_iv=len(rows), n_steady=len(steady),
        mbps=round(st.mean(rx), 1),
        cv=round(100 * st.pstdev(rx) / st.mean(rx), 2),
        rx_min=min(rx), rx_max=max(rx),
        p90_mean=round(st.mean(p90), 1), p90_max=max(p90),
        peak=max(r["subs"] for r in rows),
        joins=sum(r["joins"] for r in rows), churns=sum(r["churn"] for r in rows),
        exodus=sum(r["exodus"] for r in rows),
        churn_per_iv=round(st.mean([r["churn"] for r in steady]), 2),
    )

print("| point | rep | mean rx Mb/s | CV | p90 mean/max ms | peak subs | joins | churns | churn/5s |")
print("|---|---|---|---|---|---|---|---|---|")
out = {}
for point in ("C0", "C1"):
    for d in sorted(glob.glob(f"{BLOCK}/{point}/rep-*")):
        rows = parse(f"{d}/load.log")
        if not rows:
            print(f"| {point} | {os.path.basename(d)} | NO INTERVAL ROWS | | | | | | |")
            continue
        s = stats(rows)
        out[f"{point}/{os.path.basename(d)}"] = s
        print(f"| {point} | {os.path.basename(d).replace('rep-','')} | {s['mbps']} | "
              f"{s['cv']}% | {s['p90_mean']}/{s['p90_max']} | {s['peak']} | {s['joins']} | "
              f"{s['churns']} | {s['churn_per_iv']} |")

# derived: churn cost at fixed offered rate
for p in ("C0", "C1"):
    vals = [v["mbps"] for k, v in out.items() if k.startswith(p + "/")]
    if vals:
        print(f"\n{p}: mean of rep means = {round(st.mean(vals),1)} Mb/s (n={len(vals)})")
c0 = [v["mbps"] for k, v in out.items() if k.startswith("C0/")]
c1 = [v["mbps"] for k, v in out.items() if k.startswith("C1/")]
if c0 and c1:
    a, b = st.mean(c0), st.mean(c1)
    print(f"churn cost C1 vs C0 = {round(100*(b-a)/a,1)}% ({round(b,1)} vs {round(a,1)}, "
          f"offered 200 Mb/s = {round(100*a/200,1)}% and {round(100*b/200,1)}% of offered)")
json.dump(out, open("/home/gmarzot/.hermes/cache/scratch/c1c0_stats.json", "w"), indent=1)
