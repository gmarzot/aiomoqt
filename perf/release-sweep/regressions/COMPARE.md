# COMPARE.md — aiopquic 0.6.1 → 0.7.0, consolidated regression verdict

**Program:** `perf/release-sweep` · **Phases consolidated:** 2a (transport) + 2b (media/churn)
**Arms:** A = aiopquic **v0.6.1** (`cmp-061`) · B = aiopquic **v0.7.0** (`base-070`)
**Verdict in one line: no regression was found at any point on either release.** The only
non-null effect in the whole comparison is a ~1.3 % throughput *gain* at the D3 direct
ceiling on 0.7.0, and §6.4 of PROGRAM.md forbids attributing it to aiopquic.

---

## 0. Scope and supersession

This file replaces `regressions/compare-transport.md` as the program's regression record.
The Phase 2a report remains the detailed write-up for D3/R2/R3 and is still the place to
read for the relay-eviction story; its conclusions are carried forward verbatim below and
nothing in it is re-litigated here.

It also **retracts** the Phase 1e claim of a "relay tax −7.8 %". That number compared a
1.1 s relay burst against a 60 s direct ceiling. It is not a regression and must not be
quoted as one (see §4.4).

Scope discipline per Giovanni's 2026-11-02 comment: the comparison is on the **released
tags** (`aiopquic v0.6.1` vs `v0.7.0`), not `0.7.0rc2`; the `base-070rc2` arm still exists
on disk but was not measured. No scope was added toward G5 or ClickHouse.

---

## 1. Provenance

| item | arm A | arm B |
|---|---|---|
| aiopquic tag | `v0.6.1` | `v0.7.0` |
| arm directory | `arms/cmp-061` | `arms/base-070` |
| wheel sha256 | `3220fb9f…46f05` | `e7d28cbd…1b9ab` |
| aiomoqt | same tree both arms (`0.14.0`) | same tree both arms |
| picoquic | 1.1.53.0 | 1.1.54.0 |
| picotls | `f06553b8` | `e446233b` |

Arm identity is taken from each rep's **own record** — `meta.arm` inside every `sweep.py`
`result.json`, and `arm.txt` / `versions.txt` / `wheel.sha256` written inside every C2 rep
directory by `scripts/c2_latejoin.sh` — never from a block name or a driver label. This is
the same discipline Phase 2a used.

The **picoquic and picotls rows are a disclosed PROGRAM.md §6.4 violation**: the transport
dependency moves with the release, so the A/B measures the whole released stack, not
aiopquic's Python layer alone. Every attribution statement in this file is conditioned on
that, and §7 makes de-confounding it the first item of work.

### 1.1 Run blocks

| block | phase | points | reps | window (UTC) | status |
|---|---|---|---|---|---|
| `2026-11-15T18-04-01Z…18-51-15Z` (30 blocks) | 2a | D3, R2, R3 | 5+5+5 per point | 18:04:01 → 18:51:15 | 30/30 `rc=0` |
| `2026-11-15T18-59-51Z-p2b-d3e-*` … `p2b-d3l-*` | 2b | D3 ×10, M1 ×10 | 5 per arm per point | 19:01:25 → 19:45:06 | 30/30 `rc=0` |
| `runs/p2b-c2/C2/c2-*` | 2b | C2 ×10 | 5 per arm | 19:23:27 → ~19:36 | 10/10 `rc=0` |

2b total wall clock: 18:59:51 (block dir) → 19:45:06, i.e. 45 min 15 s including
pre-flight; index `runs/p2b-runs.txt`, log `runs/p2b-driver.log`,
hostcheck `ok:true` before and after on shenzhen, `ok:true` on seawolf.

### 1.2 Data quality waivers

All 20 M1/D3 reps are `status=dirty` for exactly one rule, `seawolf/clock_offset_jump`
(offset moved 527.0 → 533.3 ms inside the block). This is the **same waived clock jump as
Phase 2a** (272 → 277 ms) and it is why latency p50/p99 remain unquotable program-wide.
No rep is dirty for any host, load, or qdisc rule. C2 reps are `status=ok` because the C2
path does not run `hostcheck` — that is a gap in the harness, not evidence of cleanliness
(see §7 item 4).

---

## 2. Method — unchanged from Phase 2a

Identical to Phase 2a by construction, so the two phases' verdicts are directly comparable:

- **Alternating A/B, never blocked.** Order `A,B,B,A,A,B,B,A,A,B` per point. Arms are
  interleaved at rep granularity because per-arm blocks make slow drift indistinguishable
  from a release delta.
- **N=5 reps per arm per point**, 60 s measurement window, 10 s inter-rep gap.
- **D3 control at both ends** of the block (3 early + 2 late for A, 2 early + 3 late for B),
  so each arm gets N=5 D3 and the early/late group means are arm-balanced. Early and late
  groups are ~45 min apart.
- **Delta** = B − A as a percentage of the arm-A median. **CI** = percentile bootstrap over
  reps (10 000 resamples; 5 000 for interval quantiles). **p** = two-sided Mann-Whitney U,
  exact for 5v5.
- **Verdict rule (PROGRAM §8):** a delta whose CI spans zero and whose CI lies inside the
  MDE is written as **"no significant difference"**. It is never softened into "improved"
  or "slightly worse".

Two 2b-specific deviations, both recorded in `manifests/phase2b.yaml`:

1. **M1 uses a 60 s window** (Phase 1e used 120 s). Both arms see the same window, so the
   A/B is internally consistent; no Phase 1e absolute is compared against a 2b absolute.
2. **C2 is not run through `sweep.py`.** The topology-C2 path cannot hold publishing state
   (`pub_bench` returns as soon as PUBLISH is sent with no subscriber attached, so the
   measured joiner gets *"no such namespace or track"*). C2 therefore runs through
   `scripts/c2_latejoin.sh` with `ARM=<arm>` selecting the **publisher** arm while the
   subscriber stays on seawolf's pinned venv — the same publisher-side property as every
   other program point.

---

## 3. Consolidated table

| point | phase | arm A (0.6.1) | arm B (0.7.0) | Δ mbps (CI) | Δ p99 (CI) | verdict |
|---|---|---|---|---|---|---|
| D3 | 2b | 808.08 | 818.87 | +1.34 % [-0.45, +2.32] | +0.00 % [-14.73, +10.06] | **no significant difference** |
| D3 | 2a | 802.83 | 817.55 | +1.83 % [-1.13, +2.72] | -13.94 % [-32.15, +9.73] | **no significant difference** |
| D3 | 2a+2b | 807.13 | 817.57 | +1.29 % [+0.81, +2.09] | n/a | **difference** (CI excludes 0) *(pooled, not pre-registered)* |
| M1 | 2b | 5.02 | 5.02 | +0.00 % [-0.20, +0.20] | +0.00 % [-2.04, +3.06] | **no significant difference** |
| C2 | 2b | 4.98 | 4.98 | +0.00 % [-0.05, +0.05] | n/a — no interval samples | **no significant difference**; TTFF +39.27 % [-56.04, +108.43] |
| R2 | 2a | 740.50 | 745.75 | +0.71 % [-6.11, +5.24] | n/a — no per-interval samples | **point invalid** (stream reset, see §4) |
| R3 | 2a | 601.43 | 601.01 | -0.07 % [-64.98, +35.03] | n/a — no per-interval samples | **point invalid** (stream reset, see §4) |

### Observed run-to-run spread and minimum detectable effect (N=5 per arm)

| point | arm | metric | median | sd | CV | min–max | MDE @ n=5 |
|---|---|---|---|---|---|---|---|
| M1 | cmp-061 | mbps | 5.020 | 0.000 | 0.00 % | 5.020–5.020 | ±0.00 % |
| M1 | base-070 | mbps | 5.020 | 0.008 | 0.17 % | 5.010–5.030 | ±0.21 % |
| M1 | **pooled** | mbps | 5.020 | 0.006 | 0.11 % | 5.010–5.030 | ±0.14 % |
| C2 | cmp-061 | ttff | 23.020 | 11.841 | 51.44 % | 7.170–36.020 | ±64.41 % |
| C2 | base-070 | ttff | 32.060 | 12.285 | 38.32 % | 10.120–43.030 | ±47.98 % |
| C2 | **pooled** | ttff | 28.510 | 11.700 | 41.04 % | 7.170–43.030 | ±51.39 % |
| D3 | cmp-061 | mbps | 808.080 | 6.717 | 0.83 % | 805.640–822.540 | ±1.04 % |
| D3 | base-070 | mbps | 818.870 | 5.569 | 0.68 % | 813.440–826.800 | ±0.85 % |
| D3 | **pooled** | mbps | 813.915 | 7.240 | 0.89 % | 805.640–826.800 | ±1.11 % |

### Per-rep detail — M1 (media, 1080p, 60 s)

| slot | arm | mbps | obj/s | grp/s | lost % | ooo | active s | iv n | iv p50 | iv p99 | iv min–max |
|---|---|---|---|---|---|---|---|---|---|---|---|
| m1-a1 | cmp-061 | 5.020 | 30.00 | 1.000 | 0.000 | 0 | 55.5 | 11 | 5.00 | 5.27 | 5.00–5.30 |
| m1-a4 | cmp-061 | 5.020 | 30.00 | 1.000 | 0.000 | 0 | 55.6 | 11 | 5.00 | 5.27 | 5.00–5.30 |
| m1-a5 | cmp-061 | 5.020 | 30.00 | 1.000 | 0.000 | 0 | 55.6 | 11 | 5.00 | 5.27 | 5.00–5.30 |
| m1-a8 | cmp-061 | 5.020 | 30.00 | 1.000 | 0.000 | 0 | 55.6 | 11 | 5.00 | 5.27 | 5.00–5.30 |
| m1-a9 | cmp-061 | 5.020 | 30.00 | 1.000 | 0.000 | 0 | 55.6 | 11 | 5.00 | 5.27 | 5.00–5.30 |
| m1-b10 | base-070 | 5.030 | 30.10 | 1.000 | 0.000 | 0 | 55.4 | 11 | 5.00 | 5.28 | 5.00–5.30 |
| m1-b2 | base-070 | 5.010 | 30.00 | 1.000 | 0.000 | 0 | 55.7 | 11 | 5.00 | 5.27 | 5.00–5.30 |
| m1-b3 | base-070 | 5.020 | 30.00 | 1.000 | 0.000 | 0 | 55.5 | 11 | 5.00 | 5.27 | 5.00–5.30 |
| m1-b6 | base-070 | 5.020 | 30.00 | 1.000 | 0.000 | 0 | 55.6 | 11 | 5.00 | 5.27 | 5.00–5.30 |
| m1-b7 | base-070 | 5.030 | 30.10 | 1.000 | 0.000 | 0 | 55.5 | 11 | 5.00 | 5.28 | 5.00–5.30 |

### Per-rep detail — C2 (late join at +30 s, 60 s run)

| slot | arm | TTFF ms | bytes | objects | groups | missing | first obj B | burst 1 s | stream s | warm TTFF ms |
|---|---|---|---|---|---|---|---|---|---|---|
| c2-a1 | cmp-061 | 18.07 | 12643403 | 629 | 28..48 | 0 | 10013 | 2 grp / 615401 B | 20.32 | 22.93 |
| c2-a4 | cmp-061 | 23.02 | 12643403 | 629 | 28..48 | 0 | 10013 | 2 grp / 615401 B | 20.31 | 23.80 |
| c2-a5 | cmp-061 | 33.88 | 12643403 | 629 | 28..48 | 0 | 25015 | 2 grp / 625414 B | 20.32 | 21.29 |
| c2-a8 | cmp-061 | 36.02 | 12643403 | 629 | 28..48 | 0 | 25015 | 2 grp / 625414 B | 20.32 | 21.92 |
| c2-a9 | cmp-061 | 7.17 | 12643403 | 629 | 28..48 | 0 | 10013 | 2 grp / 615401 B | 20.31 | 22.68 |
| c2-b10 | base-070 | 24.96 | 12643403 | 629 | 28..48 | 0 | 25015 | 2 grp / 625414 B | 20.32 | 102.05 |
| c2-b2 | base-070 | 43.03 | 12643403 | 629 | 28..48 | 0 | 25015 | 2 grp / 625414 B | 20.31 | 36.04 |
| c2-b3 | base-070 | 32.06 | 12643403 | 629 | 28..48 | 0 | 10013 | 2 grp / 625414 B | 20.32 | 69.90 |
| c2-b6 | base-070 | 33.98 | 12643403 | 629 | 28..48 | 0 | 10013 | 2 grp / 615401 B | 20.31 | 22.66 |
| c2-b7 | base-070 | 10.12 | 12643403 | 629 | 28..48 | 0 | 10013 | 2 grp / 615401 B | 20.32 | 142.38 |

### Per-rep detail — D3 (direct ceiling, control)

| slot | arm | mbps | iv n | iv p50 | iv p99 | iv CV |
|---|---|---|---|---|---|---|
| d3e-a1 | cmp-061 | 805.64 | 12 | 780.00 | 1076.90 | 15.37 % |
| d3e-a4 | cmp-061 | 808.08 | 12 | 801.00 | 922.49 | 10.90 % |
| d3e-a5 | cmp-061 | 807.83 | 12 | 801.50 | 1088.78 | 20.59 % |
| d3e-b2 | base-070 | 822.34 | 12 | 834.00 | 905.15 | 5.81 % |
| d3e-b3 | base-070 | 818.87 | 12 | 811.00 | 1100.00 | 20.39 % |
| d3l-a3 | cmp-061 | 822.54 | 12 | 814.00 | 985.59 | 8.75 % |
| d3l-a4 | cmp-061 | 810.85 | 12 | 770.00 | 980.35 | 12.68 % |
| d3l-b1 | base-070 | 814.39 | 12 | 806.00 | 856.89 | 5.12 % |
| d3l-b2 | base-070 | 826.80 | 12 | 799.50 | 993.18 | 13.30 % |
| d3l-b5 | base-070 | 813.44 | 12 | 818.50 | 866.35 | 4.11 % |

### 3.1 How to read the p99 column

"p99" here is the **p99 of the per-interval throughput distribution** (12 × 5 s intervals
at D3, 11 × 5 s at M1), not a latency percentile. Latency p50/p99 are unquotable across
this program because of the waived clock jump (§1.2) and are not reported. C2 has no
per-interval samples at all — the late joiner is a single catch-up burst — so its tail
metric is the joiner's TTFF, given in the verdict column.

---

## 4. Point by point

### 4.1 D3 — direct ceiling (transport, steady state) — **no significant difference**

| phase | A 0.6.1 | B 0.7.0 | Δ mbps | 95 % CI | MWU | MDE @ n=5 |
|---|---|---|---|---|---|---|
| 2a | 802.83 | 817.55 | +1.83 % | −1.13…+2.72 % | iv p=0.216 (60v60), reps p=0.151 | ±2.95 % |
| 2b | 808.08 | 818.87 | +1.34 % | −0.45…+2.32 % | reps p=0.095 | ±1.11 % (pooled sd) |
| 2a+2b (secondary) | 807.13 | 817.57 | +1.29 % | +0.81…+2.09 % | p=0.014 (10v10) | — |

**Per-block verdict: no significant difference, both phases, in the same direction.**
Tail ranks the same way and does not separate: interval p99 1182.0 → 1017.2 in 2a
(−13.94 %, CI −32.15…+9.73 %), 1100.0 → 1100.0 in 2b (+0.00 %, CI −14.73…+10.06 %).

The 2b CI is narrower than the 2a CI because the 2b D3 block was unusually quiet
(pooled sd 7.24 Mb/s, CV 0.89 %). Its point estimate (+1.34 %) is above its own MDE
(±1.11 %) yet its CI still spans zero — that is the signature of an underpowered test
relative to a real small effect, not of a null. **Do not read 2b's D3 row as evidence of
no effect.** Read it as: an effect of ~1.3 % is present and this block cannot resolve it.

The secondary pooled estimate (n=10 per arm, Phase 2a + 2b, same arms/hosts/config/60 s
within 100 min of each other) resolves it: **+1.29 %, CI +0.81…+2.09 %, p=0.014**. That CI
excludes zero, so a difference of ~1.3 % is real. It is a **gain**, not a regression, and
it is **not attributable to aiopquic** (§6.4 — picoquic and picotls move with the release).

The pooled row is **not pre-registered and does not replace the per-block verdicts.** It is
reported separately because pooling raises D3 power from n=5 to n=10 per arm but can also
hide a between-block component that neither block controlled — here the two blocks do
disagree slightly in point estimate (+1.83 % vs +1.34 %), which is exactly that component.

**Control validity.** D3 early vs late: +0.78 %, CI −0.99…+2.32 %, p=0.22, inside the
D3 MDE (±1.11 %) → **the block did not drift measurably and both per-block verdicts stand.**
Because the arms are interleaved, any residual drift is arm-balanced and cannot bias the
A/B delta even if it exists.

### 4.2 M1 — media, 1080p, 60 s (MoQT, steady state) — **no significant difference**

| metric | A 0.6.1 | B 0.7.0 | Δ | 95 % CI | MWU |
|---|---|---|---|---|---|
| throughput | 5.020 Mb/s | 5.020 Mb/s | +0.00 % | −0.20…+0.20 % | reps p=0.444 |
| interval p99 | 5.30 Mb/s | 5.30 Mb/s | +0.00 % | −2.04…+3.06 % | iv p=0.584 (55v55) |
| interval p50 | 5.00 Mb/s | 5.00 Mb/s | +0.00 % | — | — |
| objects/s | 30.00 | 30.00–30.10 | +0.00 % | — | — |
| groups/s | 1.000 | 1.000 | +0.00 % | — | — |
| loss / out-of-order | 0.000 % / 0 | 0.000 % / 0 | — | — | — |

**Verdict: no significant difference.** This is the cleanest null in the program: every one
of the 10 reps sits inside a single 0.01 Mb/s quantisation step (A: 5.02 ×5; B: 5.01, 5.02,
5.02, 5.03, 5.03), and the per-interval distributions are indistinguishable (all 10 reps
have interval p50 = 5.00 and p99 = 5.27–5.28 Mb/s, 11 intervals each, zero loss, zero OOO).

**Caveat on the MDE.** The per-rep M1 metric is quantised to 0.01 Mb/s = 0.2 % of its own
value, and arm A's per-rep sd is literally 0.000. A per-arm sd of zero would advertise an
infinitely sharp test, so the MDE is computed from the **pooled** sd (±0.14 %). Even so,
**treat ±0.2 % as M1's resolution floor, not as evidence of a 0.2 %-sharp null.** The
statistically meaningful M1 test is the 110-interval comparison (p=0.584), which is what
the verdict rests on.

M1 cannot be compared against Phase 1e's 120 s M1 absolute (4.81 Mb/s) — different window,
different block. No cross-phase M1 comparison is made anywhere in this file.

### 4.3 C2 — late join at +30 s, 60 s run (MoQT, churn/late-join) — **no significant difference**

Timeline per rep: relay at t=0, publisher at t=2 s, warm subscriber at t=4 s, late joiner at
t=30 s, publisher exits at t=82 s.

| metric | A 0.6.1 | B 0.7.0 | Δ | 95 % CI | MWU |
|---|---|---|---|---|---|
| late-joiner TTFF | 23.02 ms | 32.06 ms | +39.27 % | −56.04…+108.43 % | p=0.548 |
| delivered throughput | 4.978 Mb/s | 4.978 Mb/s | +0.00 % | −0.05…+0.05 % | p=1.000 |
| bytes delivered | 12 643 403 | 12 643 403 | 0.00 % | — | identical in 10/10 |
| objects delivered | 629 | 629 | 0.00 % | — | identical in 10/10 |
| groups received | 28..48 (21) | 28..48 (21) | 0 | — | identical in 10/10 |
| missing groups | 0 | 0 | 0 | — | 0 in 10/10 |

**Verdict: no significant difference** on every pre-registered C2 metric. Delivery is
**bit-identical** across arms: all ten reps deliver exactly 12 643 403 bytes / 629 objects /
groups 28–48 / zero missing, in a 20.31–20.32 s window, with a 2-group, ~615–625 kB
catch-up burst in the first second. Whatever else 0.7.0 changed, it did not change what a
late joiner receives.

**TTFF is unresolvable at N=5 and must not be read as a slowdown.** The observed spread is
7.17–36.02 ms on A and 10.12–43.03 ms on B — CV 51 % and 38 % respectively. The pooled CV
of 41 % implies an MDE of ±51 % at n=5, and the measured delta (+39.27 %) sits *inside*
that. There is **no pre-published noise floor for C2**: unlike M1 (which has
`noise-floor.md` N2), this floor is **derived post hoc from this very block** and is quoted
as such wherever it appears.

**One distributional flag, explicitly not a verdict.** The *warm* subscriber (joins at
+4 s, i.e. while the publisher is still in its startup phase) has a TTFF that is tight on
A and heavy-tailed on B:

| arm | warm-sub TTFF per rep (ms) | median | sd |
|---|---|---|---|
| A `cmp-061` | 22.93, 23.80, 21.29, 21.92, 22.68 | 22.68 | 0.96 |
| B `base-070` | 36.04, 69.90, 22.66, 102.05, 142.38 | 69.90 | 48.87 |

Post-hoc permutation tests (statistic chosen *after* seeing the spread, so these p-values
carry selection bias and are **not** the verdict): median p=0.119, mean p=0.032, max
p=0.048; 0/5 reps above 50 ms on A versus 3/5 on B. The pre-registered late-joiner test
shows nothing (median p=0.524, mean p=0.492, max p=1.000; 0/5 above 50 ms on both arms).

Reading: if 0.7.0 has a problem, it is a **startup-phase first-object latency with a heavy
tail**, not steady-state late-join behaviour. N=5 cannot establish it and this file does not
claim it. §7 item 1 is the measurement that would settle it.

### 4.4 R2 / R3 — **point invalid on both releases**, not a regression

Carried forward from Phase 2a unchanged: the subscriber stream is reset after ~1.1 s /
~104.6 MB at R2 and ~0.1 s / ~7.6 MB at R3, **identically on both arms**. Both points carry
zero per-interval samples, so PROGRAM §6.9's interval test cannot run on them and their
deltas (+0.71 %, CI −6.11…+5.24 %; −0.07 %, CI −64.98…+35.03 %) measure *reset timing*,
not throughput. They are reported for completeness and excluded from the verdict.

**Retraction (final):** the Phase 1e "relay tax −7.8 %" is dead. It compared a 1.1 s burst
against a 60 s direct ceiling. No sustained relay ceiling has been measured in this program.

---

## 5. Where the regression lives

The card asks the question directly; the answer is that it does not live anywhere this
program can measure. Each axis, with the evidence that closes it:

### 5.1 Transport vs MoQT

| layer | point | result |
|---|---|---|
| transport only (no MoQT catalog/track semantics) | **D3** | +1.34 % (2b) / +1.83 % (2a), both **no significant difference**; pooled +1.29 % is a **gain** |
| MoQT steady state | **M1** | **no significant difference**, +0.00 % CI ±0.20 % |
| MoQT late join | **C2** | **no significant difference**; delivery bit-identical |

Neither layer regresses. The only effect anywhere is in the transport layer and it is
positive.

### 5.2 Throughput vs tail

- **Throughput: clean everywhere.** M1 +0.00 % (CI ±0.20 %), C2 +0.00 % (bit-identical
  bytes), D3 positive.
- **Tail: no established regression.** D3 interval p99 +0.00 % (CI −14.73…+10.06 %), M1
  interval p99 +0.00 % (CI −2.04…+3.06 %), C2 TTFF +39 % but CI −56…+108 % at CV 41 %.
- **One tail-shaped flag survives:** warm-sub TTFF dispersion at C2 (§4.3). It is a
  *distributional* difference that the median test cannot see and N=5 cannot confirm. It is
  the single item in this program that has the shape of a regression rather than a null.

### 5.3 Steady state vs churn / late join

- **Steady state: clean** — M1 and D3, both arms, all reps.
- **Late join: clean** — 10/10 C2 reps deliver identical bytes/objects/groups with zero
  missing. A late joiner is not disadvantaged by 0.7.0 in any way this point can detect.
- **Churn: not exercised.** R2/R3 are invalid points (§4.4). There is **no churn
  measurement** in this comparison — the C2 point tests *late join*, not *churn*. Any claim
  about churn on 0.7.0 would be unsupported by this data.

---

## 6. Noise floors and MDE, stated honestly

| point | pre-published floor? | source | value used |
|---|---|---|---|
| D3 | no (program-derived) | per-block pooled sd | ±1.11 % @ n=5 (2b), ±2.95 % (2a) |
| M1 | **yes** | `noise-floor.md` N2 | CV 0.27 %, MDE n=5 0.33 % — measured over a **120 s** window |
| C2 | **no** | derived post hoc from this block | CV 41 %, MDE ±51 % @ n=5 |

Two caveats that matter for anyone reusing these numbers:

1. **The published M1 floor is a 120 s figure applied to a 60 s window**, so it is a *lower
   bound* on the 60 s floor. The observed per-arm M1 CV (0.00 % / 0.17 %) is reported next
   to it rather than replaced by it, and the quantisation floor (±0.2 %) dominates both.
2. **C2 has no pre-registered floor.** Its floor is derived from the same 10 reps that
   produced the estimate, which makes the C2 MDE optimistic. Any future C2 claim should use
   a floor from a *separate* block.

---

## 7. Ranked plan — what a bisect must detect, and what must not be bisected

**No bisect is justified today.** Every point is either a null, invalid, or a gain that
§6.4 forbids attributing to aiopquic. What follows is the ranked work that would either
create a bisect target or close the question, written so Phase 2c needs no re-analysis.

### Rank 1 — de-confound the transport dependency (one experiment, answers two questions)

The whole A/B confounds aiopquic with picoquic 1.1.53.0→1.1.54.0 and picotls
`f06553b8`→`e446233b`. One control resolves both the D3 +1.3 % gain and the C2 warm-TTFF
tail:

- **Build:** aiopquic `v0.7.0` code on the **0.6.1 dependency pins** (picoquic 1.1.53.0,
  picotls `f06553b8`). One knob, nothing else changed.
- **Points:** `D3` (60 s, N=5/arm) and `C2` (60 s, late=30 s, N=5/arm), alternating A/B
  against `cmp-061` exactly as in 2b, D3 control at both ends.
- **Decision rule for D3:** if the new arm's D3 delta vs `cmp-061` is inside ±1.11 %
  (2b D3 MDE), the +1.3 % lives in the transport dependency, not aiopquic → close the gain
  question, no bisect. If it stays ≥ +1.0 % with CI excluding zero, it is an aiopquic-layer
  gain → Rank 2a.
- **Decision rule for C2:** the metric is `fraction(warm_ttff > 50 ms)` (A: 0/5, B: 3/5
  observed). If the re-pinned arm returns 0–1/5, the tail is the transport dependency → close
  it. If it returns ≥3/5, it is an aiopquic-layer regression → Rank 2b.

### Rank 2a — aiopquic gain (only if Rank 1 says the gain is in aiopquic)

A gain is not a regression bisect, but if it is wanted: `git bisect` over
`v0.6.1..v0.7.0` in aiopquic, point **D3**, 60 s, **N=5 per arm per bisect step**, decision
threshold **≥ +1.0 % with CI excluding zero** (2b D3 MDE ±1.11 %, point estimate +1.34 %).
Expected yield: low — the effect is 1.3 % against a 1.1 % MDE, so each bisect step is
borderline and will need the pooled n=10 protocol to confirm. Budget accordingly.

### Rank 2b — aiopquic startup-latency tail (only if Rank 1 implicates aiopquic)

This is the only candidate with the *shape* of a regression, so it gets a real spec:

- **Point:** `C2` exactly as implemented (`scripts/c2_latejoin.sh`), 60 s run, late delay
  30 s, warm subscriber at +4 s.
- **Metric:** `fraction(warm_ttff > 50 ms)` **and** median warm TTFF. Both must be
  reported; the fraction is the one with the power.
- **N:** **15 reps per arm.** Justification: distinguishing 0 % from 60 % at α=0.05
  two-sided and 80 % power needs n≈14 per arm (Fisher exact); at n=15 the median MDE is
  ±2.8·41 %/√15 ≈ ±30 %, against an observed median difference of +208 %. Either statistic
  detects the observed effect comfortably at n=15.
- **Decision rule:** arm is "bad" if fraction > 40 % and median > 45 ms; "good" if fraction
  ≤ 20 % and median ≤ 30 ms; anything else → re-run that step once before deciding.
- **Then** `git bisect` over `v0.6.1..v0.7.0`, same point, N=15 per arm per step, same
  decision rule.
- **Expected delta a bisect must detect:** median warm TTFF 22.7 ms → 69.9 ms (+205 %),
  or equivalently 0/5 → 3/5 reps above 50 ms. If a bisect step cannot show at least
  +50 % on the median at N=15, the step is not resolvable and the bisect must stop rather
  than guess.

### Rank 3 — restore the invalid points (not a bisect; a harness fix)

R2/R3 are invalid because the moqx relay evicts the subscriber stream after ~104.6 MB /
~1.1 s (R2) and ~7.6 MB / ~0.1 s (R3), identically on both releases. Until that eviction is
fixed or replaced by a paced relay point (offered ≈ 800 Mb/s), no relay comparison can
produce a verdict. This is a separate card; it is **not** a regression bisect and must not
be scheduled as one.

### Explicitly do NOT bisect

| point | reason |
|---|---|
| **M1** | +0.00 %, CI −0.20…+0.20 % — at the metric's quantisation floor. Nothing to find. |
| **C2 delivery** | bit-identical bytes/objects/groups, 0 missing in 10/10. Nothing to find. |
| **D3 (as a regression)** | the effect is a gain, and §6.4 blocks attribution until Rank 1 runs. |
| **R2/R3** | invalid points. A bisect would be measuring eviction timing. |

---

## 8. What this file does not claim

- It does **not** claim 0.7.0 is faster. The +1.3 % is a gain in a confounded A/B and is
  not attributable to aiopquic.
- It does **not** claim 0.7.0 is slower at late join. TTFF is inside a ±51 % MDE.
- It does **not** claim churn was tested. R2/R3 are invalid; C2 is late join, not churn.
- It does **not** quote latency p50/p99. The clock waiver makes them unquotable.
- It does **not** resurrect the "relay tax −7.8 %". Retracted in §4.4.
- It does **not** compare 2b M1 absolutes against Phase 1e's 120 s M1. Different windows.

---

## 9. Reproduction

```bash
cd /home/gmarzot/Projects/marzresearch/perf-sweep/aiomoqt/perf/release-sweep

# 2b measurement (45 min; arms interleaved A,B,B,A,A,B,B,A,A,B per point)
bash scripts/p2b_driver.sh                 # → runs/p2b-runs.txt, runs/p2b-driver.log

# 2b statistics (writes regressions/phase2b-data.json, prints the tables)
python3 scripts/p2b_analysis.py

# the tables in this file, regenerated from the JSON so they cannot drift
python3 scripts/p2b_tables.py > /tmp/p2b-tables.md

# 2a statistics, for the carried-forward rows
python3 scripts/p2a_analysis.py
```

Single C2 rep, arm-explicit:

```bash
ARM=cmp-061 scripts/c2_latejoin.sh runs/p2b-c2 "c2-a1" 30 60
```

Each C2 rep writes `arm.txt`, `versions.txt`, `wheel.sha256` and `params.txt` beside its
logs, so arm identity is auditable without trusting the driver.

---

*Generated from `regressions/phase2a-data.json` and `regressions/phase2b-data.json` by
`scripts/p2b_tables.py`. Every number in the tables is emitted by that script; no figure in
this file was transcribed by hand.*
