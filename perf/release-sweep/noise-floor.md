# Noise floor — base-070rc2 (Phase 1b, Part A)

Run block: `perf/release-sweep/runs/2026-10-30T21-40-50Z-partA`
Branch: `perf/release-sweep`. Arm: `base-070rc2` (aiopquic v0.7.0rc2 @51a80c9, CPython 3.14.6).
Topology: shenzhen publisher → seawolf subscriber through `moqx` relay at `10.14.25.173:9768`.
Protocol: 60 s measurement, 5 s warmup, 10 s idle between runs, same arm throughout, 7 repetitions per point, points run back to back (N1 × 7, then N2 × 7).

## What was measured

| point | shape | P | s | g | pacing |
|---|---|---|---|---|---|
| **N1** | D-raw, synthetic objects | 4 | 4096 | 200 | unpaced (publisher-limited only by the link) |
| **N2** | M1 media, 1080p video profile, 1 subscriber, raw transport through the relay | — | — | — | media encoder pacing (~4.8 Mbps) |

Both points are **screening grade, status `dirty`**: every rep carries a waived
`clock_offset_jump` violation (see *Caveat* below). Throughput, object rate and
interval CV are unaffected by the clock; one-way latency is not measurable and is
`null` for all 14 reps.

## Formula

For a two-sample comparison of means, the minimum detectable effect at n reps per
arm is

```
MDE(n) = (t_{0.975,df=n-1} + t_{0.80,df=n-1}) · sqrt(2/n) · σ
       ≈ 2.77 · CV / sqrt(n)          [reported as % of the mean]
```

`CV = σ/μ` with `σ` the sample standard deviation over the 7 reps. 2.77 is the
task's rounding of `(t_0.975 + t_0.80)`; at df = 4–6 those quantiles sum to
2.78–2.84, so the quoted MDE is, if anything, 1–2 % optimistic. Values are
relative: the smallest true difference that a two-arm comparison at that n would
detect 80 % of the time at α = 0.05.

## N1 — D-raw, P=4, s=4096, g=200, unpaced (n = 7)

| metric | mean | stdev | CV % | MDE n=3 % | MDE n=5 % |
|---|---|---|---|---|---|
| mbps | 812.1 | 19.34 | 2.38 | 3.81 | 2.95 |
| obj_s | 24,700 | 588 | 2.38 | 3.81 | 2.95 |
| p50 | — | — | — | — | — |
| p99 | — | — | — | — | — |
| interval_cv | 0.0805 | 0.0577 | 71.75 | 114.74 | 88.88 |

Per rep: 776.6, 832.7, 826.0, 826.7, 811.1, 799.7, 811.4 Mbps (all reps `lost = 0`).

## N2 — M1 media 1080p through the relay (n = 7)

| metric | mean | stdev | CV % | MDE n=3 % | MDE n=5 % |
|---|---|---|---|---|---|
| mbps | 4.83 | 0.0129 | 0.27 | 0.43 | 0.33 |
| obj_s | 28.9 | 0.0817 | 0.28 | 0.45 | 0.35 |
| p50 | — | — | — | — | — |
| p99 | — | — | — | — | — |
| interval_cv | 0.0461 | 0.0168 | 36.35 | 58.13 | 45.03 |

Per rep: 4.85, 4.83, 4.84, 4.81, 4.83, 4.84, 4.85 Mbps (all reps `lost = 0`).

## Reading

**N1 is a link-limited benchmark, not a stack-limited one.** At 812 Mbps the
Gigabit path between shenzhen and seawolf is the bottleneck; the 2.4 % CV is
mostly the link/relay's own jitter, and a change that only moves the *stack*
will be invisible here until the path stops being the ceiling. Every later claim
on a D-raw-class point at N=3 must therefore clear **±3.8 %** to be a claim at
all, and ±3.0 % at N=5. Anything smaller is noise on this rig.

**N2 is the tight point.** Publisher-paced at 4.8 Mbps, it never touches the
link ceiling, so run-to-run variation is 0.27 % and the N=3 floor is **0.43 %**.
Media-shape regressions are detectable at sub-percent scale, which is why the
program keeps an M point in the matrix: it is the only point where small CPU or
scheduling effects are visible above noise.

**`interval_cv` is not a usable decision metric at screening N.** Its own CV is
72 % (N1) and 36 % (N2) because it is a dispersion statistic estimated from a
handful of 5 s intervals; the MDE at N=3 is 115 % / 58 %. It is reported for
completeness, and a difference in `interval_cv` must never be the basis of a
verdict. Use `mbps`, `obj_s` and (same-host) `p50/p99` for decisions.

**The MDE floor every later claim must clear (N=3, screening):**

| metric class | floor | basis |
|---|---|---|
| throughput / obj rate, link-limited (D-raw, R-raw at ~800 Mbps) | **3.8 %** | N1 CV 2.38 % |
| throughput / obj rate, publisher-paced media (M1) | **0.45 %** | N2 CV 0.27 % |
| one-way latency, cross-host | **not measurable today** | clock probe unstable, see caveat |
| one-way latency, same host (L/P points) | see `SUMMARY-part1.md` (Part B, N=3) | same clock domain, no skew |
| interval_cv | do not use at N<10 | 72 % / 36 % CV |

## Caveat — cross-host clock, and why N1/N2 latency is null

Part A attempts 1 (`runs/2026-10-30T21-28-36Z`) and 2 (`runs/2026-10-30T21-33-59Z-partA`)
each aborted after rep 1 on `seawolf:clock_offset_jump`. A direct probe sequence
(20 samples per probe, 2 s apart) returned offsets of −912 ms, −990 ms, −1058 ms,
−27 ms, −96 ms with a steady 4 ms RTT. With a 4 ms round trip, a correct
two-sample estimate cannot exceed ±2 ms, so the estimate — not the link — is at
fault. During a 60 s point the pre/post offset moved between 39 ms and 1.32 s.
`timedatectl` on shenzhen reports `System clock synchronized: yes`, NTP active;
the suspect is WSL2's `time.time()` behaviour under this host's load pattern.

Attempt 3 ran with `--waive clock_offset_jump`, which downgrades the violation to
`dirty` and records `latency not measured: clock offset moved …` on each rep.
That is the entire reason these 14 reps are not baseline grade. The same-host
L/P points in Part B share one clock domain and are not affected.

Re-baselining N1/N2 latency in Phase 1c needs either a verified-stable clock on
both ends or a latency definition that does not depend on cross-host wall-clock
comparison (e.g. QUIC-stack RTT plus a documented one-way correction).

## Provenance

- `runs/2026-10-30T21-40-50Z-partA/N1/rep-0{1..7}/result.json`
- `runs/2026-10-30T21-40-50Z-partA/N2/rep-0{1..7}/result.json`
- `runs/noise-floor.json`, `runs/noise-floor-tables.md` (generated by `scripts/noise_floor.py`)
- Hostcheck before/after every rep: `hostcheck-pre*.json`, `hostcheck-post*.json` in each rep dir.
