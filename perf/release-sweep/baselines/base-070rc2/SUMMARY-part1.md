# Baseline matrix v0, part 1 — loopback (L1–L5) and two-process (P1–P3)

Arm `base-070rc2` (aiopquic v0.7.0rc2 @51a80c9, aiomoqt 0.13.3 @1a444c0, CPython 3.14.6).
Branch `perf/release-sweep`. Run block `runs/2026-10-30T22-04-13Z-partB`.
Protocol (PROGRAM §6): N=3 screening reps, 60 s measured after 5 s warmup, 5 s interval,
10 s idle between runs, hostcheck pre/post every rep, one variable per point.
All 24 reps returned `status: ok`, `lost = 0` except where noted, no abort, no waiver.

**All eight points in this file run on shenzhen alone.** L is one process playing both
ends; P is two processes (`pub_server` → `sub_bench`) over 127.0.0.1:4434. Neither crosses
the LAN, so neither is a deployment number, and both share one CPU pool with the harness
itself — the L/P figures are an *upper bound on what this stack can do when the network
and the second host are removed*, not a prediction of the D/R rows. They are not
comparable to D or R rows as if topology were the only difference.

## Table (median of 3 reps per point, PROGRAM §6.9)

| point | config | mbps | obj_s | grp_s | p50 ms | p99 ms | lost | interval_cv | CPU pub/sub % | note |
|---|---|---|---|---|---|---|---|---|---|---|
| L1 | raw, P=1, s=4096, g=200, unpaced | 1714.8 | 52,164 | 260.8 | 15.1 | 25.3 | 0 | 0.05 | n/a | single-stream ceiling; rep-01 is a cold outlier (1424 Mbps) |
| L2 | raw, P=8, s=4096, g=200, unpaced | 1731.8 | 52,683 | 263.4 | 15.1 | 24.0 | 0 | 0.04 | n/a | multi-subgroup ceiling; tightest point in the matrix (CV 0.4 %) |
| L3 | WT, P=8, s=4096, g=200, unpaced | 1715.8 | 52,197 | 261.0 | 15.7 | 24.0 | 24 | 0.03 | n/a | WT vs raw: −0.9 % throughput, within noise; 24 lost objects in one rep |
| L4 | raw, P=8, s=4096, g=40, unpaced | 1458.3 | 44,363 | 1109.1 | 1.0 | 25.5 | 0 | 0.06 | n/a | churn: −15.8 % vs L2, groups/s ×4.2, p50 collapses to 1 ms |
| L5 | raw, P=8, s=1200, g=200, unpaced | 943.9 | 97,271 | 486.4 | 1.1 | 7.3 | 0 | 0.02 | n/a | small objects: pps-bound at 97 k obj/s, best tail of the set |
| P1 | raw, P=4, s=4096, g=200, unpaced | 1728.2 | 52,574 | 262.9 | 15.3 | 23.4 | 0 | 0.03 | 83.6 / 106.5 | two-process, no network |
| P2 | raw, P=4, s=4096, g=200, r=20000 obj/s | 657.5 | 20,000 | 100.0 | 0.3 | 0.7 | 0 | 0.00 | 31.6 / 42.5 | paced: rate control holds to 0.0 % across reps; p99 0.7 ms |
| P3 | WT, P=4, s=4096, g=200, unpaced | 1744.4 | 53,065 | 265.3 | 14.7 | 22.6 | 0 | 0.03 | 88.3 / 107.3 | WT two-process; +0.9 % vs P1, within noise |

`n/a` = the point runs as a single process, so per-process CPU is not attributable; the
combined figure lives in each rep's `result.json` (`cpu_bench`). CPU is mean % over the
process lifetime including connect, so it understates steady-state load.

Per-rep `mbps` (3 reps, in run order):

```
L1 1424.4 1714.8 1767.0    L2 1723.4 1731.8 1737.3    L3 1691.6 1715.8 1744.8
L4 1393.1 1458.3 1503.6    L5  943.9  971.2  925.3    P1 1773.2 1697.5 1728.2
P2  657.5  657.5  657.4    P3 1744.4 1778.0 1738.1
```

## What the numbers say

**1. The process boundary is free.** L2 (one process, both ends) and P1 (two processes)
are the same traffic shape and land 1731.8 vs 1728.2 Mbps — a 0.2 % difference, far
inside the N=3 floor. P3 vs L3 (WT) is +1.7 %, also inside noise. So the harness's
two-process path adds no measurable cost at the ceiling, and L/P rows can be pooled when
the shape matches. This was the main structural question for Part B and it is answered
negatively: there is no isolation penalty to correct for.

**2. Latency is queue budget, confirming the Phase 0 finding.** At ~1.73 Gbps
(≈216 MB/s) with the default 4 MiB `--max-queued-bytes`, the standing latency is
`4 MiB / 216 MB/s ≈ 19 ms`; measured p50 is 15.1–15.7 ms. L4 (g=40) and L5 (1200 B)
drop p50 to ~1 ms because they never fill the queue. The lever identified in Phase 0
(`--max-queued-bytes`) is therefore still the first-order latency control at the
ceiling, and any future latency claim at 1.7 Gbps must state the queue budget with it.

**3. Churn costs throughput, not tail.** L4 vs L2: −15.8 % throughput while p99 stays
at 25 ms and groups/s quadruples. Churn is a rate-of-metadata problem here, not a
latency problem.

**4. Small objects are pps-bound but healthy.** L5 moves 97 k obj/s at 944 Mbps with
the best tail in the matrix (p99 7.3 ms). 1200-byte objects do not hit a cliff on this
host, so the scaling sweep should vary `s` at fixed offered load rather than assume one.

**5. Rate control is exact.** P2, paced at 20,000 obj/s, returns 20,000.2 obj/s with
0.0 % CV across reps and p99 0.7 ms. The pacer, not the stack, is the limit there —
P2 is a control for the pacer, not a throughput point.

**6. The first rep of a loopback block is cold.** L1 rep-01 is 1424 Mbps (−18 % vs its
own later reps) with a doubled p50. This is the only point whose CV is inflated by a
single rep (mbps CV 11.3 % vs 0.4–3.8 % elsewhere). Screening N=3 therefore must discard
or explicitly flag rep-01 on any point that starts a block; better, each block should
run one throwaway warmup rep. Recorded here rather than silently dropped.

## Noise floor for same-host latency (N=3, indicative only)

Part A could not produce a cross-host latency floor (clock probe unstable — see
`noise-floor.md`). The same-host points give a first estimate of run-to-run CV at n=3:

| point | mbps CV | p50 CV | p99 CV |
|---|---|---|---|
| L2 | 0.4 % | 0.8 % | 1.8 % |
| L3 | 1.6 % | 3.3 % | 3.2 % |
| L4 | 3.8 % | 6.0 % | 33.6 % |
| L5 | 2.4 % | 5.4 % | 46.2 % |
| P1 | 2.2 % | 4.3 % | 1.6 % |
| P2 | 0.0 % | 0.0 % | 8.7 % |
| P3 | 1.2 % | 0.7 % | 8.6 % |

Throughput at the ceiling reproduces to ≤2.2 %; p50 to ≤6 %; **p99 is the noisy one**
(1.6–46 %). With n=3 the CV estimate itself is ±40 %, so these bound the *shape* of the
noise, not a publishable MDE: same-host p99 claims need N≥5, and churn/small-object p99
claims (CV 34–46 %) need N≥10 or a percentile that pools interval samples.

## Provenance and deviations

- Reps: `runs/2026-10-30T22-04-13Z-partB/<point>/rep-0{1,2,3}/{result.json,pub.log,sub.log|bench.log,hostcheck-pre*.json,hostcheck-post*.json}`
- Generated tables: `runs/partB-summary.md`, `runs/partB-noise.md`, `runs/partB-noise.json`
- Manifest: `manifests/phase1b-partb.yaml` (point configs copied verbatim from PROGRAM §7).
- Deviation 1: P-point subscriber pin is `6-9`, not the PROGRAM pin map's `0-3` — `0-3` is
  the seawolf-side subscriber pin and on shenzhen it overlaps the publisher's `2-5`.
  L points get the union `0-5`. Pinning is otherwise identical across points.
- Deviation 2: hostcheck ran with `--hostcheck-samples 3` (not 20) for screening reps to
  keep the 24-rep block inside the task's runtime budget; the flag is recorded in each
  rep's `meta`. It does not affect the L/P clock (single clock domain), only the
  cross-host offset numbers, which are unused here.
- Deviation 3: M1 in PROGRAM §7 is 120 s; the task card's N2 specified 60 s, which is
  what Part A ran (`noise-floor.md`).
- No tuning was applied: qdisc, sysctls, `--max-queued-bytes` and GSO are at defaults
  (Q4/O4 in STATE.md remain open). No relay config change.
