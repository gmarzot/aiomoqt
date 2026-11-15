#!/usr/bin/env python3
"""Emit the markdown tables for regressions/COMPARE.md from the two data files.

Every number in COMPARE.md's tables comes from here, so the report cannot drift
from regressions/phase2a-data.json / phase2b-data.json. Run from release-sweep/:

    python3 scripts/p2b_tables.py > /tmp/p2b-tables.md
"""
import json
import os
import statistics as st

RS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = json.load(open(os.path.join(RS, "regressions", "phase2a-data.json")))
B = json.load(open(os.path.join(RS, "regressions", "phase2b-data.json")))


def g(d, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def pct_s(v):
    return "—" if v is None else f"{v:+.2f} %"


def ci(c):
    return "—" if not c else f"[{c[0]:+.2f}, {c[1]:+.2f}]"


def q(xs, f):
    """Linear-interpolated quantile, same convention as p2a_analysis.boot_ciq."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * f
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def num(v, d=2):
    return "—" if v is None else f"{v:.{d}f}"


def verdict(delta, lo, hi, mde, point):
    """PROGRAM §8: a delta whose CI spans zero is written "no significant difference".

    The MDE qualifier is deliberately NOT printed per row: the MDE depends on which
    sigma you choose (per-arm vs pooled), and printing one of them in the verdict
    column invites a dispute the data does not settle. The MDE table is §6's job.
    """
    if delta is None:
        return "—"
    if lo is not None and hi is not None and lo <= 0 <= hi:
        return "**no significant difference**"
    return "**difference** (CI excludes 0)"


out = []
w = out.append

# ---------------------------------------------------------------- table 1
w("| point | phase | arm A (0.6.1) | arm B (0.7.0) | Δ mbps (CI) | Δ p99 (CI) | verdict |")
w("|---|---|---|---|---|---|---|")

d3b = B["d3"]
d3a = A["points"]["D3"]
pd3 = B["pooled_d3"]

mde_d3b = max(g(B, "floors", "D3_cmp-061_mbps", "mde_pct_n", default=0),
              g(B, "floors", "D3_base-070_mbps", "mde_pct_n", default=0))
mde_m1 = max(g(B, "floors", "M1_cmp-061_mbps", "mde_pct_n", default=0),
             g(B, "floors", "M1_base-070_mbps", "mde_pct_n", default=0))


def row(pt, phase, a_med, b_med, d, p99, mde, extra=""):
    lo, hi = (d["ci_pct"] if d else (None, None))
    w(f"| {pt} | {phase} | {num(a_med)} | {num(b_med)} | {pct_s(d['delta_pct']) if d else '—'} "
      f"{ci(d['ci_pct']) if d else ''} | {p99} | {verdict(d['delta_pct'] if d else None, lo, hi, mde, pt)}{extra} |")


p99 = lambda x: (f"{pct_s(g(x,'delta_pct'))} {ci(g(x,'ci_pct'))}") if x else "—"

row("D3", "2b", g(d3b, "cmp-061", "mbps_median"), g(d3b, "base-070", "mbps_median"),
    d3b.get("d_mbps"), p99(d3b.get("d_iv_p99")), mde_d3b)
row("D3", "2a", g(d3a, "cmp-061", "mbps_median"), g(d3a, "base-070", "mbps_median"),
    {"delta_pct": g(d3a, "delta_pct"), "ci_pct": g(d3a, "delta_ci_pct")},
    f"{pct_s(g(d3a,'iv_p99_delta_pct'))} {ci(g(d3a,'iv_p99_ci_pct'))}", 2.95)
row("D3", "2a+2b", pd3["a"], pd3["b"], pd3, "n/a",
    mde_d3b, " *(pooled, not pre-registered)*")

m1 = B["m1"]
row("M1", "2b", g(m1, "cmp-061", "mbps_median"), g(m1, "base-070", "mbps_median"),
    m1.get("d_mbps"), p99(m1.get("d_iv_p99")), mde_m1)

c2 = B["c2"]
row("C2", "2b", g(c2, "cmp-061", "mbps_median"), g(c2, "base-070", "mbps_median"),
    c2.get("d_mbps"), "n/a — no interval samples", None,
    f"; TTFF {pct_s(g(c2,'d_ttff','delta_pct'))} {ci(g(c2,'d_ttff','ci_pct'))}")

for pt in ("R2", "R3"):
    s = A["points"][pt]
    w(f"| {pt} | 2a | {num(g(s,'cmp-061','mbps_median'))} | {num(g(s,'base-070','mbps_median'))} | "
      f"{pct_s(g(s,'delta_pct'))} {ci(g(s,'delta_ci_pct'))} | n/a — no per-interval samples | "
      "**point invalid** (stream reset, see §4) |")

# ---------------------------------------------------------------- table 2
w("")
w("### Observed run-to-run spread and minimum detectable effect (N=5 per arm)")
w("")
w("| point | arm | metric | median | sd | CV | min–max | MDE @ n=5 |")
w("|---|---|---|---|---|---|---|---|")
for k, v in B["floors"].items():
    pt, arm, metric = k.split("_", 2)
    arm = "**pooled**" if arm == "pooled" else arm
    w(f"| {pt} | {arm} | {metric} | {num(v['median'],3)} | {num(v['sd'],3)} | {v['cv_pct']:.2f} % | "
      f"{num(v['min'],3)}–{num(v['max'],3)} | ±{v['mde_pct_n']:.2f} % |")

# ---------------------------------------------------------------- table 3
w("")
w("### Per-rep detail — M1 (media, 1080p, 60 s)")
w("")
w("| slot | arm | mbps | obj/s | grp/s | lost % | ooo | active s | iv n | iv p50 | iv p99 | iv min–max |")
w("|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in sorted(B["reps"], key=lambda r: r["slot"]):
    if r["point"] != "M1":
        continue
    sm = r.get("samples") or []
    w(f"| {r['slot']} | {r['arm']} | {num(r.get('mbps'),3)} | {num(r.get('obj_s'),2)} | "
      f"{num(r.get('grp_s'),3)} | {num(r.get('lost_pct'),3)} | {r.get('ooo')} | {num(r.get('active_s'),1)} | "
      f"{len(sm)} | {num(q(sm,0.5))} | {num(q(sm,0.99))} | {num(min(sm) if sm else None)}–{num(max(sm) if sm else None)} |")

w("")
w("### Per-rep detail — C2 (late join at +30 s, 60 s run)")
w("")
w("| slot | arm | TTFF ms | bytes | objects | groups | missing | first obj B | burst 1 s | stream s | warm TTFF ms |")
w("|---|---|---|---|---|---|---|---|---|---|---|")
for r in sorted(B["reps"], key=lambda r: r["slot"]):
    if r["point"] != "C2":
        continue
    gr = f"{r.get('first_group')}..{(r.get('first_group') or 0) + (r.get('n_groups') or 1) - 1}"
    w(f"| {r['slot']} | {r['arm']} | {num(r.get('ttff_ms'))} | {r.get('bytes')} | {r.get('objects')} | "
      f"{gr} | {r.get('missing_groups')} | {r.get('first_object_bytes')} | "
      f"{r.get('burst_groups_1s')} grp / {r.get('burst_bytes_1s')} B | {num(r.get('stream_s'))} | "
      f"{num(r.get('ttff_warm_ms'))} |")

w("")
w("### Per-rep detail — D3 (direct ceiling, control)")
w("")
w("| slot | arm | mbps | iv n | iv p50 | iv p99 | iv CV |")
w("|---|---|---|---|---|---|---|")
for r in sorted(B["reps"], key=lambda r: r["slot"]):
    if r["point"] != "D3":
        continue
    sm = r.get("samples") or []
    w(f"| {r['slot']} | {r['arm']} | {num(r.get('mbps'),2)} | {len(sm)} | {num(q(sm,0.5))} | "
      f"{num(q(sm,0.99))} | {num(100*st.stdev(sm)/st.median(sm) if len(sm)>1 else None)} % |")

print("\n".join(out))
