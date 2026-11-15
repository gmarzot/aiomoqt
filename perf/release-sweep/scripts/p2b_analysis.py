#!/usr/bin/env python3
"""Phase 2b analysis — 0.6.1 (cmp-061) vs 0.7.0 (base-070) on M1 and C2.

Method is Phase 2a's, unchanged: arm identity comes from each rep's own record
(meta.arm for sweep.py points, arm.txt for C2 reps), never from the block name;
delta is B - A as a percentage of the arm-A median; CI is a percentile bootstrap
over reps (10 000 resamples, 5 000 for interval quantiles); the significance test
is a two-sided Mann-Whitney U (exact for 5v5).

Points:
  M1  media shape, 1080p, 60 s      -> mbps, obj/s, grp/s, lost %, interval tail
  C2  late join at +30 s, 60 s run  -> TTFF ms, delivered mbps, catch-up burst,
                                       missing groups
  D3  direct ceiling, control       -> drift test (early vs late group medians)

Writes regressions/phase2b-data.json and prints the markdown tables.
"""
import glob
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p2a_analysis import boot_ci, boot_ciq, mwu_p, pct, icv  # noqa: E402

RS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(RS, "runs")
C2ROOT = os.path.join(RUNS, "p2b-c2", "C2")
OUT = os.path.join(RS, "regressions", "phase2b-data.json")
ARMS = ("cmp-061", "base-070")
LBL = {"cmp-061": "A(0.6.1)", "base-070": "B(0.7.0)"}


def ttff_json(path):
    if not os.path.exists(path):
        return None
    for line in open(path, errors="replace"):
        if "TTFF-JSON:" in line:
            try:
                return json.loads(line.split("TTFF-JSON:", 1)[1].strip())
            except Exception:
                return None
    return None


def missing_groups(ids):
    """Count group ids absent from the received run (gaps in a 1 group/s track)."""
    if not ids:
        return None
    s = sorted(ids)
    return (s[-1] - s[0] + 1) - len(s)


def load_sw(point_prefix, point_name):
    reps = []
    for blk in sorted(glob.glob(os.path.join(RUNS, "*p2b-%s*" % point_prefix))):
        slot = os.path.basename(blk).split("p2b-", 1)[-1]
        if slot.startswith("smoke"):
            continue
        for rj in glob.glob(os.path.join(blk, "*", "rep-*", "result.json")):
            d = json.load(open(rj))
            m = d.get("meta") or {}
            s = d.get("summary") or {}
            iv = [x.get("mbps") for x in (d.get("samples") or []) if x.get("mbps") is not None]
            reps.append({
                "block": os.path.basename(blk), "slot": slot, "point": point_name,
                "arm": m.get("arm"), "status": m.get("status"),
                "waived": m.get("waived_violations"),
                "mbps": s.get("mbps"), "obj_s": s.get("obj_s"), "grp_s": s.get("grp_s"),
                "lost_pct": s.get("lost_pct"), "ooo": s.get("ooo"),
                "active_s": s.get("active_s"), "jitter_ms": s.get("jitter_ms"),
                "mbps_min_interval": s.get("mbps_min_interval"),
                "mbps_max_interval": s.get("mbps_max_interval"),
                "interval_cv": s.get("interval_cv"),
                "p50_ms": s.get("p50"), "p99_ms": s.get("p99"),
                "samples": iv, "n_interval": len(iv),
            })
    return reps


def load_c2():
    reps = []
    for d in sorted(glob.glob(os.path.join(C2ROOT, "c2-*"))):
        slot = os.path.basename(d)[3:]
        arm = open(os.path.join(d, "arm.txt")).read().strip() if os.path.exists(os.path.join(d, "arm.txt")) else None
        late = ttff_json(os.path.join(d, "sub1.log"))
        warm = ttff_json(os.path.join(d, "sub-warm.log"))
        if not late:
            reps.append({"block": os.path.basename(d), "slot": slot, "point": "C2",
                         "arm": arm, "status": "failed", "error": "no TTFF-JSON"})
            continue
        ss = late.get("stream_seconds") or 0
        reps.append({
            "block": os.path.basename(d), "slot": slot, "point": "C2", "arm": arm,
            "status": "ok" if not late.get("error") else "dirty",
            "error": late.get("error"),
            "ttff_ms": late.get("ttff_ms"),
            "mbps": (late["bytes"] * 8.0 / ss / 1e6) if ss else None,
            "bytes": late.get("bytes"), "objects": late.get("objects"),
            "stream_s": ss,
            "obj_s": (late["objects"] / ss) if ss and late.get("objects") else None,
            "first_group": late.get("first_group"),
            "first_object_bytes": late.get("first_object_bytes"),
            "burst_bytes_1s": late.get("burst_bytes_1s"),
            "burst_groups_1s": late.get("burst_groups_1s"),
            "n_groups": len(late.get("groups") or []),
            "missing_groups": missing_groups(late.get("groups")),
            "ttff_warm_ms": (warm or {}).get("ttff_ms"),
            "mbps_warm": ((warm["bytes"] * 8.0 / warm["stream_seconds"] / 1e6)
                          if warm and warm.get("stream_seconds") else None),
            "samples": [],
        })
    return reps


def delta_block(a_vals, b_vals, label, kind="median", q=None):
    """Percent delta of B vs A with bootstrap CI, normalised by the A central value."""
    a_vals = [x for x in a_vals if x is not None]
    b_vals = [x for x in b_vals if x is not None]
    if len(a_vals) < 2 or len(b_vals) < 2:
        return None
    if q is None:
        ca = st.median(a_vals) if kind == "median" else st.mean(a_vals)
        cb = st.median(b_vals) if kind == "median" else st.mean(b_vals)
        lo, hi = boot_ci(a_vals, b_vals, stat=kind)
    else:
        ca, cb = pct(a_vals, q), pct(b_vals, q)
        lo, hi = boot_ciq(a_vals, b_vals, q)
    if not ca:
        return None
    return {"label": label, "a": ca, "b": cb, "delta_pct": 100.0 * (cb - ca) / ca,
            "ci_pct": [100.0 * lo / ca, 100.0 * hi / ca],
            "mwu_p": mwu_p(a_vals, b_vals)}


def summarize(reps, point):
    out = {}
    for arm in ARMS:
        rs = [r for r in reps if r["point"] == point and r["arm"] == arm and r.get("status") != "failed"]
        if not rs:
            continue
        mb = [r["mbps"] for r in rs if r.get("mbps") is not None]
        iv = [x for r in rs for x in (r.get("samples") or [])]
        ttff = [r["ttff_ms"] for r in rs if r.get("ttff_ms") is not None]
        out[arm] = {
            "n": len(rs),
            "slots": [r["slot"] for r in rs],
            "status": [r.get("status") for r in rs],
            "mbps": mb,
            "mbps_median": st.median(mb) if mb else None,
            "mbps_min": min(mb) if mb else None,
            "mbps_max": max(mb) if mb else None,
            "ttff": ttff,
            "ttff_median": st.median(ttff) if ttff else None,
            "ttff_min": min(ttff) if ttff else None,
            "ttff_max": max(ttff) if ttff else None,
            "bytes": [r.get("bytes") for r in rs if r.get("bytes")],
            "bytes_median": st.median([r["bytes"] for r in rs if r.get("bytes")]) if any(r.get("bytes") for r in rs) else None,
            "objects_median": st.median([r["objects"] for r in rs if r.get("objects")]) if any(r.get("objects") for r in rs) else None,
            "obj_s_median": st.median([r["obj_s"] for r in rs if r.get("obj_s")]) if any(r.get("obj_s") for r in rs) else None,
            "grp_s_median": st.median([r["grp_s"] for r in rs if r.get("grp_s") is not None]) if any(r.get("grp_s") is not None for r in rs) else None,
            "lost_pct": [r.get("lost_pct") for r in rs],
            "ooo": [r.get("ooo") for r in rs],
            "stream_s_median": st.median([r["stream_s"] for r in rs if r.get("stream_s")]) if any(r.get("stream_s") for r in rs) else None,
            "first_group": [r.get("first_group") for r in rs],
            "missing_groups": [r.get("missing_groups") for r in rs],
            "burst_bytes_1s": [r.get("burst_bytes_1s") for r in rs],
            "burst_groups_1s": [r.get("burst_groups_1s") for r in rs],
            "ttff_warm": [r.get("ttff_warm_ms") for r in rs],
            "mbps_warm": [r.get("mbps_warm") for r in rs],
            "interval_n": len(iv),
            "interval_cv_pct": icv(iv),
            "interval_p50": pct(iv, 0.50),
            "interval_p99": pct(iv, 0.99),
            "interval_p10": pct(iv, 0.10),
            "interval_min": min(iv) if iv else None,
            "slow_frac_pct": (100.0 * sum(1 for x in iv if x < 0.5 * st.median(iv)) / len(iv)) if iv else None,
            "jitter_ms_median": st.median([r["jitter_ms"] for r in rs if r.get("jitter_ms") is not None]) if any(r.get("jitter_ms") is not None for r in rs) else None,
            "mbps_min_interval": [r.get("mbps_min_interval") for r in rs],
            "_samples": iv,
        }
    a, b = out.get("cmp-061"), out.get("base-070")
    if a and b:
        if a["mbps"] and b["mbps"]:
            out["d_mbps"] = delta_block(a["mbps"], b["mbps"], "mbps")
            out["d_obj_s"] = delta_block([r["obj_s"] for r in reps if r["point"] == point and r["arm"] == "cmp-061"],
                                         [r["obj_s"] for r in reps if r["point"] == point and r["arm"] == "base-070"], "obj_s")
            if a["interval_p50"] and b["interval_p50"]:
                out["d_iv_p50"] = delta_block(a["_samples"], b["_samples"], "iv_p50", q=0.50)
                out["d_iv_p99"] = delta_block(a["_samples"], b["_samples"], "iv_p99", q=0.99)
                out["d_iv_p10"] = delta_block(a["_samples"], b["_samples"], "iv_p10", q=0.10)
                out["mwu_p_intervals"] = mwu_p(a["_samples"], b["_samples"])
        if a["ttff"] and b["ttff"]:
            out["d_ttff"] = delta_block(a["ttff"], b["ttff"], "ttff_ms")
            out["d_ttff_abs_ms"] = st.median(b["ttff"]) - st.median(a["ttff"])
        if a["bytes_median"] and b["bytes_median"]:
            out["d_bytes"] = delta_block(a["bytes"], b["bytes"], "bytes")
    return out


def drift(reps):
    e = [r["mbps"] for r in reps if r["slot"].startswith("d3e") and r.get("mbps")]
    l = [r["mbps"] for r in reps if r["slot"].startswith("d3l") and r.get("mbps")]
    if not e or not l:
        return None
    me, ml = st.median(e), st.median(l)
    lo, hi = boot_ci(e, l)
    return {"early_n": len(e), "late_n": len(l),
            "early_median": me, "late_median": ml,
            "drift_pct": 100.0 * (ml - me) / me,
            "drift_ci_pct": [100.0 * lo / me, 100.0 * hi / me],
            "mwu_p": mwu_p(e, l),
            "early_slots": sorted(r["slot"] for r in reps if r["slot"].startswith("d3e")),
            "late_slots": sorted(r["slot"] for r in reps if r["slot"].startswith("d3l"))}


def floors(data):
    """Observed run-to-run spread per arm, and the N=5 effect size it implies.

    Only M1 has a pre-published floor (noise-floor.md N2: mbps CV 0.27 %, MDE
    n=5 0.33 %). M1 here is a 60 s window, so the published 120 s floor is a
    lower bound; the observed per-arm CV is reported next to it. C2 has no
    pre-published floor at all, so its floor is derived post hoc from this block
    and must be labelled as such wherever it is quoted.
    """
    out = {}
    for pt, key in (("M1", "mbps"), ("C2", "ttff"), ("D3", "mbps")):
        s = data.get(pt.lower()) or {}
        for arm in ARMS:
            x = s.get(arm)
            vals = [v for v in (x.get(key) or []) if v is not None] if x else []
            if len(vals) < 2:
                continue
            med = st.median(vals)
            sd = st.stdev(vals)
            out["%s_%s_%s" % (pt, arm, key)] = {
                "n": len(vals), "median": med, "sd": sd,
                "cv_pct": 100.0 * sd / med if med else None,
                "min": min(vals), "max": max(vals),
                # 80 % power, alpha=0.05 two-sided, n reps per arm:
                #   MDE = (z_{a/2} + z_b) * sqrt(2) * sigma / sqrt(n) = 2.8 * sd / sqrt(n)
                "mde_pct_n": (100.0 * 2.8 * sd / med / (len(vals) ** 0.5)) if med else None,
            }
        # pooled across arms: the between-arm sd is the honest sigma for an A/B,
        # because a per-arm sd of zero (metric quantisation) would otherwise
        # advertise an infinitely sharp test.
        pooled = [v for arm in ARMS for v in ((s.get(arm) or {}).get(key) or []) if v is not None]
        if len(pooled) >= 4:
            med = st.median(pooled)
            sd = st.stdev(pooled)
            out["%s_pooled_%s" % (pt, key)] = {
                "n": len(pooled), "median": med, "sd": sd,
                "cv_pct": 100.0 * sd / med if med else None,
                "min": min(pooled), "max": max(pooled),
                "mde_pct_n": (100.0 * 2.8 * sd / med / ((len(pooled) / 2.0) ** 0.5)) if med else None,
            }
    return out


def pooled_d3():
    """Secondary, NOT pre-registered: D3 reps from Phase 2a and 2b pooled.

    Both blocks ran the same arms, hosts, config and 60 s duration within 100 min
    of each other, so pooling raises the D3 power from n=5 to n=10 per arm. It is
    reported separately and never replaces the per-block verdict, because a pooled
    estimate can hide a between-block component that neither block controlled.
    """
    try:
        a = json.load(open(os.path.join(RS, "regressions", "phase2a-data.json")))
    except Exception:
        return None
    p2a = a["points"]["D3"]
    cur = [r for r in load_sw("d3e", "D3") + load_sw("d3l", "D3") if r.get("mbps")]
    va = list(p2a["cmp-061"]["mbps"]) + [r["mbps"] for r in cur if r["arm"] == "cmp-061"]
    vb = list(p2a["base-070"]["mbps"]) + [r["mbps"] for r in cur if r["arm"] == "base-070"]
    if len(va) < 3 or len(vb) < 3:
        return None
    d = delta_block(va, vb, "mbps")
    d["n_a"], d["n_b"] = len(va), len(vb)
    d["a_2a"] = list(p2a["cmp-061"]["mbps"])
    d["b_2a"] = list(p2a["base-070"]["mbps"])
    d["a_2b"] = [r["mbps"] for r in cur if r["arm"] == "cmp-061"]
    d["b_2b"] = [r["mbps"] for r in cur if r["arm"] == "base-070"]
    return d


def f(v, d=2):
    return "—" if v is None else f"{v:.{d}f}"


def main():
    m1 = load_sw("m1", "M1")
    d3 = load_sw("d3e", "D3") + load_sw("d3l", "D3")
    c2 = load_c2()
    reps = m1 + c2 + d3
    data = {"m1": summarize(m1, "M1"), "c2": summarize(c2, "C2"),
            "d3": summarize(d3, "D3"), "control_drift": drift(d3),
            "reps": reps}
    data["floors"] = floors(data)
    data["pooled_d3"] = pooled_d3()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w"), indent=1, default=str)

    print(f"reps: M1={len(m1)} C2={len(c2)} D3={len(d3)}  (expect 10/10/10)")
    for name in ("d3", "m1", "c2"):
        s = data[name]
        print(f"\n== {name.upper()} ==")
        for arm in ARMS:
            x = s.get(arm)
            if not x:
                continue
            print(f"  {LBL[arm]:10s} n={x['n']} mbps={f(x['mbps_median'])} [{f(x['mbps_min'])}-{f(x['mbps_max'])}] "
                  f"ttff={f(x['ttff_median'])} [{f(x['ttff_min'])}-{f(x['ttff_max'])}] "
                  f"iv_cv={f(x['interval_cv_pct'])} iv_p99={f(x['interval_p99'])} "
                  f"missing={x['missing_groups']} lost={x['lost_pct']}")
        for k in ("d_mbps", "d_ttff", "d_iv_p50", "d_iv_p99", "d_bytes", "d_obj_s"):
            d = s.get(k)
            if not d:
                continue
            lo, hi = d["ci_pct"]
            p = d["mwu_p"]
            print(f"  {d['label']:10s} A={f(d['a'],3)} B={f(d['b'],3)} Δ={d['delta_pct']:+.2f}% "
                  f"CI [{lo:+.2f},{hi:+.2f}] MWU p={('—' if p is None else round(p,4))}")
        if s.get("mwu_p_intervals") is not None:
            print(f"  MWU intervals p={round(s['mwu_p_intervals'],4)} (n={s['cmp-061']['interval_n']}v{s['base-070']['interval_n']})")
    cd = data["control_drift"]
    if cd:
        print(f"\ncontrol D3 early {cd['early_median']:.2f} (n={cd['early_n']}) vs late {cd['late_median']:.2f} "
              f"(n={cd['late_n']}) drift {cd['drift_pct']:+.2f}% CI [{cd['drift_ci_pct'][0]:+.2f},"
              f"{cd['drift_ci_pct'][1]:+.2f}] p={cd['mwu_p']:.4f}")
    pd = data.get("pooled_d3")
    if pd:
        lo, hi = pd["ci_pct"]
        print(f"\n== pooled D3 (2a+2b, NOT pre-registered) n={pd['n_a']}v{pd['n_b']} ==")
        print(f"  A={pd['a']:.2f} B={pd['b']:.2f} Δ={pd['delta_pct']:+.2f}% CI [{lo:+.2f},{hi:+.2f}] "
              f"MWU p={('—' if pd['mwu_p'] is None else round(pd['mwu_p'],4))}")
    print("\n== observed floors (per-arm run-to-run spread) ==")
    for k, v in data["floors"].items():
        print(f"  {k:22s} n={v['n']} med={f(v['median'],3)} sd={f(v['sd'],3)} "
              f"CV={f(v['cv_pct'])}% range=[{f(v['min'],3)},{f(v['max'],3)}] "
              f"MDE@n={f(v['mde_pct_n'],1)}%")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
