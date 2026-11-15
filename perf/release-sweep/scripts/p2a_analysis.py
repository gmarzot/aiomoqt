#!/usr/bin/env python3
"""Phase 2a analysis — 0.6.1 (cmp-061) vs 0.7.0 (base-070) on D3/R2/R3.

Reads every run block named *p2a-<slot>*, takes the arm identity from
result.json meta.arm (never from the block name), and reports per point/arm:

  median mbps, obj_s, active_s, interval CV, interval-mbps p50/p99,
  delta % (B - A) with a percentile bootstrap 95 % CI,
  Mann-Whitney U p-value (exact for 5v5 reps, normal-approx w/ ties for
  interval samples).

Latency p50/p99 are reported only if the summary carries them; every run in
this program is clock-waived, so they are expected to be null and the tail is
described with interval-throughput statistics instead.

Writes regressions/phase2a-data.json and prints a markdown table.
"""
import json
import glob
import os
import random
import statistics as st
from itertools import combinations

RS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(RS, "runs")
OUT = os.path.join(RS, "regressions", "phase2a-data.json")

ARMS = {"cmp-061": "A(0.6.1)", "base-070": "B(0.7.0)"}
random.seed(20261115)


def load():
    reps = []
    for blk in sorted(glob.glob(os.path.join(RUNS, "*p2a-*"))):
        for pt in ("D3", "R2", "R3"):
            for rj in glob.glob(os.path.join(blk, pt, "rep-*", "result.json")):
                d = json.load(open(rj))
                m = d.get("meta") or {}
                s = d.get("summary") or {}
                slot = os.path.basename(blk).split("p2a-")[-1]
                samples = [x.get("mbps") for x in (d.get("samples") or []) if x.get("mbps") is not None]
                reps.append({
                    "block": os.path.basename(blk), "slot": slot, "point": pt,
                    "arm": m.get("arm"), "status": m.get("status"),
                    "mbps": s.get("mbps"), "obj_s": s.get("obj_s"),
                    "active_s": s.get("active_s"), "lost_pct": s.get("lost_pct"),
                    "p50_ms": s.get("p50"), "p99_ms": s.get("p99"),
                    "samples": samples,
                })
    return reps


def boot_ciq(a, b, q, n=5000):
    diffs = []
    for _ in range(n):
        ra = [random.choice(a) for _ in a]
        rb = [random.choice(b) for _ in b]
        diffs.append(pct(rb, q) - pct(ra, q))
    diffs.sort()
    return diffs[int(0.025 * n)], diffs[int(0.975 * n) - 1]


def boot_ci(a, b, stat="median", n=10000):
    diffs = []
    for _ in range(n):
        ra = [random.choice(a) for _ in a]
        rb = [random.choice(b) for _ in b]
        sa = st.median(ra) if stat == "median" else st.mean(ra)
        sb = st.median(rb) if stat == "median" else st.mean(rb)
        diffs.append(sb - sa)
    diffs.sort()
    return diffs[int(0.025 * n)], diffs[int(0.975 * n) - 1]


def mwu_p(a, b):
    """Two-sided Mann-Whitney U. Exact for n1+n2 <= 12, else normal approx."""
    a, b = list(a), list(b)
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return None
    comb = a + b
    order = sorted(range(len(comb)), key=lambda i: comb[i])
    ranks = [0.0] * len(comb)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and comb[order[j + 1]] == comb[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    if n1 + n2 <= 12:
        obs = u1
        allr = list(range(n1 + n2))
        cnt = 0
        tot = 0
        for idx in combinations(allr, n1):
            u = sum(ranks[i] for i in idx) - n1 * (n1 + 1) / 2.0
            tot += 1
            if abs(u - n1 * n2 / 2.0) >= abs(obs - n1 * n2 / 2.0) - 1e-9:
                cnt += 1
        return cnt / tot
    mu = n1 * n2 / 2.0
    # tie correction
    tie = 0.0
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and comb[order[j + 1]] == comb[order[i]]:
            j += 1
        t = j - i + 1
        tie += t ** 3 - t
        i = j + 1
    N = n1 + n2
    sd = (n1 * n2 / 12.0 * ((N + 1) - tie / (N * (N - 1)))) ** 0.5
    if sd == 0:
        return 1.0
    z = (abs(u1 - mu) - 0.5) / sd
    # two-sided normal tail
    p = 2 * 0.5 * (1 - _erf(abs(z) / 2 ** 0.5))
    return min(1.0, max(0.0, p))


def _erf(x):
    # Abramowitz & Stegun 7.1.26
    t = 1 / (1 + 0.3275911 * abs(x))
    y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * 2.718281828459045 ** (-x * x)
    return y if x >= 0 else -y


def icv(xs):
    return (st.pstdev(xs) / st.mean(xs) * 100.0) if len(xs) > 1 and st.mean(xs) else None


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summarize(reps, point):
    out = {}
    for arm in ("cmp-061", "base-070"):
        rs = [r for r in reps if r["point"] == point and r["arm"] == arm]
        if not rs:
            continue
        mb = [r["mbps"] for r in rs if r["mbps"] is not None]
        iv = [x for r in rs for x in r["samples"]]
        out[arm] = {
            "n": len(rs),
            "slots": [r["slot"] for r in rs],
            "mbps": mb,
            "mbps_median": st.median(mb) if mb else None,
            "mbps_min": min(mb) if mb else None,
            "mbps_max": max(mb) if mb else None,
            "obj_s_median": st.median([r["obj_s"] for r in rs if r["obj_s"] is not None]) if any(r["obj_s"] for r in rs) else None,
            "active_s_median": st.median([r["active_s"] for r in rs if r["active_s"] is not None]) if any(r["active_s"] for r in rs) else None,
            "active_s": [r["active_s"] for r in rs],
            "p50_ms": [r["p50_ms"] for r in rs],
            "p99_ms": [r["p99_ms"] for r in rs],
            "interval_n": len(iv),
            "interval_cv_pct": icv(iv),
            "interval_p50": pct(iv, 0.50),
            "interval_p99": pct(iv, 0.99),
            "interval_min": min(iv) if iv else None,
            "interval_p10": pct(iv, 0.10),
            "slow_frac_pct": (100.0 * sum(1 for x in iv if x < 0.5 * st.median(iv)) / len(iv)) if iv else None,
            "rep_min_interval": min((min(r["samples"]) for r in rs if r["samples"]), default=None),
            "bytes_mb": [(r["mbps"] * r["active_s"] / 8.0) for r in rs if r["mbps"] and r["active_s"]],
            "bytes_mb_median": st.median([r["mbps"] * r["active_s"] / 8.0 for r in rs if r["mbps"] and r["active_s"]]) if any(r["mbps"] and r["active_s"] for r in rs) else None,
            "status": [r["status"] for r in rs],
        }
    a = out.get("cmp-061")
    b = out.get("base-070")
    if a and b and a["mbps"] and b["mbps"]:
        d = st.median(b["mbps"]) - st.median(a["mbps"])
        lo, hi = boot_ci(a["mbps"], b["mbps"])
        iv_a = [x for r in reps if r["point"] == point and r["arm"] == "cmp-061" for x in r["samples"]]
        iv_b = [x for r in reps if r["point"] == point and r["arm"] == "base-070" for x in r["samples"]]
        out["delta_pct"] = 100.0 * d / st.median(a["mbps"])
        out["delta_ci_pct"] = [100.0 * lo / st.median(a["mbps"]), 100.0 * hi / st.median(a["mbps"])]
        out["mwu_p_reps"] = mwu_p(a["mbps"], b["mbps"])
        if a["active_s"] and b["active_s"]:
            out["active_s_delta_pct"] = 100.0 * (b["active_s_median"] - a["active_s_median"]) / a["active_s_median"] if a["active_s_median"] else None
            out["mwu_p_active_s"] = mwu_p([x for x in a["active_s"] if x is not None], [x for x in b["active_s"] if x is not None])
        if a["bytes_mb_median"] and b["bytes_mb_median"]:
            bl, bh = boot_ci(a["bytes_mb"], b["bytes_mb"])
            out["bytes_delta_pct"] = 100.0 * (b["bytes_mb_median"] - a["bytes_mb_median"]) / a["bytes_mb_median"]
            out["bytes_delta_ci_pct"] = [100.0 * bl / a["bytes_mb_median"], 100.0 * bh / a["bytes_mb_median"]]
        if a["interval_p50"] and b["interval_p50"]:
            out["iv_p50_delta_pct"] = 100.0 * (b["interval_p50"] - a["interval_p50"]) / a["interval_p50"]
            out["iv_p99_delta_pct"] = 100.0 * (b["interval_p99"] - a["interval_p99"]) / a["interval_p99"] if a["interval_p99"] else None
            paid_a = [x for r in reps if r["point"] == point and r["arm"] == "cmp-061" for x in r["samples"]]
            paid_b = [x for r in reps if r["point"] == point and r["arm"] == "base-070" for x in r["samples"]]
            if paid_a and paid_b:
                lo50, hi50 = boot_ci(paid_a, paid_b)
                out["iv_p50_ci_pct"] = [100.0 * lo50 / a["interval_p50"], 100.0 * hi50 / a["interval_p50"]]
                lo99, hi99 = boot_ciq(paid_a, paid_b, 0.99)
                out["iv_p99_ci_pct"] = [100.0 * lo99 / a["interval_p99"], 100.0 * hi99 / a["interval_p99"]]
                lo10, hi10 = boot_ciq(paid_a, paid_b, 0.10)
                out["iv_p10_ci_pct"] = [100.0 * lo10 / a["interval_p10"], 100.0 * hi10 / a["interval_p10"]]
        out["mwu_p_intervals"] = mwu_p(iv_a, iv_b) if (iv_a and iv_b) else None
    return out


def drift(reps):
    """D3 early (block start) vs late (block end) -> control drift."""
    e = [r["mbps"] for r in reps if r["slot"].startswith("d3e") and r["mbps"]]
    l = [r["mbps"] for r in reps if r["slot"].startswith("d3l") and r["mbps"]]
    if not e or not l:
        return None
    me, ml = st.median(e), st.median(l)
    lo, hi = boot_ci(e, l)
    return {"early_n": len(e), "late_n": len(l), "early_median": me, "late_median": ml,
            "drift_pct": 100.0 * (ml - me) / me,
            "drift_ci_pct": [100.0 * lo / me, 100.0 * hi / me],
            "mwu_p": mwu_p(e, l),
            "early_slots": sorted(r["slot"] for r in reps if r["slot"].startswith("d3e")),
            "late_slots": sorted(r["slot"] for r in reps if r["slot"].startswith("d3l"))}


def main():
    reps = load()
    data = {"reps": reps, "points": {}, "control_drift": drift(reps)}
    for pt in ("D3", "R2", "R3"):
        data["points"][pt] = summarize(reps, pt)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w"), indent=1, default=str)

    print(f"reps loaded: {len(reps)}  (expect 30)")
    print(f"{'point':5s} {'arm':9s} {'n':>2s} {'mbps_med':>9s} {'spread':>17s} {'obj_s':>9s} {'active_s':>8s} {'iv_cv%':>6s} {'iv_p99':>8s}")
    for pt in ("D3", "R2", "R3"):
        for arm in ("cmp-061", "base-070"):
            s = data["points"][pt].get(arm)
            if not s:
                continue
            sp = f"{s['mbps_min']:.1f}-{s['mbps_max']:.1f}" if s["mbps"] else "-"
            print(f"{pt:5s} {arm:9s} {s['n']:2d} {s['mbps_median']:9.2f} {sp:>17s} "
                  f"{(s['obj_s_median'] or 0):9.1f} {(s['active_s_median'] or 0):8.2f} "
                  f"{(s['interval_cv_pct'] or 0):6.2f} {(s['interval_p99'] or 0):8.1f}")
        d = data["points"][pt]
        if "delta_pct" in d:
            lo, hi = d["delta_ci_pct"]
            print(f"      delta B-A {d['delta_pct']:+.2f}%  CI [{lo:+.2f},{hi:+.2f}]  "
                  f"MWU_reps p={d['mwu_p_reps']:.4f}  MWU_iv p={d['mwu_p_intervals'] if d['mwu_p_intervals'] is None else round(d['mwu_p_intervals'],4)}")
    cd = data["control_drift"]
    if cd:
        print(f"\ncontrol D3 early {cd['early_median']:.2f} (n={cd['early_n']}) vs late {cd['late_median']:.2f} "
              f"(n={cd['late_n']}) = {cd['drift_pct']:+.2f}% CI [{cd['drift_ci_pct'][0]:+.2f},{cd['drift_ci_pct'][1]:+.2f}] p={cd['mwu_p']:.4f}")
    print(f"\nwrote {OUT}")

    # markdown block for the report
    print("\n<!-- md -->")
    print("| point | arm | n | median Mb/s | range | obj/s | active_s | MB to reset | iv CV % | iv p50 | iv p99 | slow 5s % |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for pt in ("D3", "R2", "R3"):
        for arm, lbl in (("cmp-061", "0.6.1"), ("base-070", "0.7.0")):
            s2 = data["points"][pt].get(arm)
            if not s2:
                continue
            f = lambda v, d=1: ("—" if v is None else f"{v:.{d}f}")
            print(f"| {pt} | {lbl} | {s2['n']} | {f(s2['mbps_median'],2)} | {f(s2['mbps_min'])}–{f(s2['mbps_max'])} | "
                  f"{f(s2['obj_s_median'],0)} | {f(s2['active_s_median'],2)} | {f(s2['bytes_mb_median'],1)} | {f(s2['interval_cv_pct'],2)} | "
                  f"{f(s2['interval_p50'],1)} | {f(s2['interval_p99'],1)} | {f(s2['slow_frac_pct'],1)} |")
        d2 = data["points"][pt]
        if "delta_pct" in d2:
            lo, hi = d2["delta_ci_pct"]
            print(f"| {pt} Δ | B−A | | **{d2['delta_pct']:+.2f}%** | CI {lo:+.2f}…{hi:+.2f}% | | | | | | | |")


if __name__ == "__main__":
    main()
