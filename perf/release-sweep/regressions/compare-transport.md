# Phase 2a — transport comparison, aiopquic 0.6.1 vs 0.7.0

Run date 2026-11-15. One block, one day: 30 single-rep runs, `18:04:01Z`–`18:51:15Z`,
all tagged `p2a-<slot>`. No cross-day comparison is made anywhere in this report;
the D3 control was measured inside the same block (see §3).

## 1. Basis and arm identity

The task card says "0.7.0rc2". The operator's 2026-11-02 comment overrides it:
*"Use the final released tags, not rc2."* This report therefore compares **releases**,
aiopquic **v0.6.1** vs **v0.7.0**, and does not use the 0.7.0rc2 numbers from Phases 1a–1e
as a comparison basis.

Identity is asserted from the built arms, not from a label:

| | arm A | arm B |
|---|---|---|
| arm dir | `arms/cmp-061` | `arms/base-070` |
| ref | `v0.6.1` | `v0.7.0` |
| aiopquic SHA | `ccf37d8ab0b9fa2919298665779daa3b681642e2` | `7baed8b8a7ef1f19e603e19e0679bf5c3acc8151` |
| version print (`python -P -m aiomoqt.versions`) | `aiopquic: 0.6.1` | `aiopquic: 0.7.0` |
| wheel | `aiopquic-0.6.1-cp314-cp314-linux_x86_64.whl` | `aiopquic-0.7.0-cp314-cp314-linux_x86_64.whl` |
| wheel sha256 | `3220fb9f2dfdebadd6ee810ab20a5b2a56336718b16fc5eb70bcfd4898246f05` | `e7d28cbd59bd565a98ad6c4cc7fdcc8325b24393d7fe58932218d0e26af1b9ab` |
| picoquic | 1.1.53.0 `c9088724` | 1.1.54.0 `3012b39c` |
| picotls | `f06553b8` | `e446233b` |
| built | 2026-10-28T12:50:51-04:00 | 2026-11-15T13:03:21-05:00 |
| aiomoqt | same editable tree, `0.13.3-20-g6cbe7af` (`perf/release-sweep`) | identical |

**Confound, stated up front.** aiopquic 0.6.1 and 0.7.0 pin different picoquic
(1.1.53.0 → 1.1.54.0) and picotls (`f06553b8` → `e446233b`). The A/B therefore compares
the *whole released stack*, not the Python layer alone, and violates PROGRAM §6.4
("one variable at a time"). Every delta below is a release-to-release delta; none of it
may be attributed to aiopquic's Python code without the 1.1.54 rebuild in §7.

## 2. What was run

- Points: **D3** (direct ceiling), **R2** (relay, unpaced), **R3** (relay + churn),
  copied verbatim from `manifests/baseline-v0.yaml` into `manifests/phase2a.yaml`
  (only `reps` changed, to 1, so arms can interleave).
- N=5 per arm per point, 60 s measurement window each, order `A,B,B,A,A,B,B,A,A,B`
  per point (PROGRAM §6.2 alternating A/B), driven by `scripts/p2a_driver.sh` —
  one `sweep.py` invocation per rep, so `meta.arm` is per-rep truth and no rep can
  be relabelled by block position.
- Control: D3 at the start (`A,B,B,A,A`) and end (`B,B,A,A,B`) of the block.
- Both arms drawn from the same aiomoqt tree; only the aiopquic venv differs.

## 3. Block validity — control point

| group | reps | median Mb/s |
|---|---|---|
| D3 early (block start) | `d3e-a1,a4,a5` + `d3e-b2,b3` | 808.35 |
| D3 late (block end) | `d3l-a3,a4` + `d3l-b1,b2,b5` | 806.44 |

Drift **−0.24 %**, bootstrap 95 % CI **[−1.82, +2.66] %**, Mann-Whitney p=1.00.
The P1b noise floor for this class of point is CV 2.38 %, MDE(N=5) **2.95 %**. Drift is
inside the MDE and the CI excludes the MDE in both directions → **block valid**, no
detectable drift, no day-splitting caveat applies.

Hosts: no strays, no orphans, no foreign benchmark processes, qdisc unchanged pre/post
on both hosts. All 30 reps carry `status=dirty` for exactly one reason — the waived
cross-host clock-offset jump (`clock_offset_jump`, 272→277 ms); nothing else was waived
that touches throughput. That same clock condition is why **latency p50/p99 are not
reported** (see §5).

## 4. Results

| point | arm | n | median Mb/s | range | obj/s | active_s | MB to reset | iv CV % | iv p50 | iv p99 | slow 5s % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| D3 | 0.6.1 | 5 | 802.83 | 795.8–808.4 | 24423 | 61.60 | 6191.8 | 15.47 | 800.5 | 1182.0 | 0.0 |
| D3 | 0.7.0 | 5 | 817.55 | 793.8–823.1 | 24871 | 61.10 | 6241.6 | 10.76 | 817.0 | 1017.2 | 0.0 |
| D3 Δ | B−A | | **+1.83 %** | CI −1.13…+2.72 % | +1.83 % | −0.81 % | +0.80 % | | +2.06 % | −13.94 % | |
| R2 | 0.6.1 | 5 | 740.50 | 707.0–761.4 | 22527 | 1.10 | 104.7 | — | — | — | — |
| R2 | 0.7.0 | 5 | 745.75 | 712.1–759.8 | 22686 | 1.10 | 104.5 | — | — | — | — |
| R2 Δ | B−A | | **+0.71 %** | CI −6.11…+5.24 % | | 0.00 % | −0.21 % | | | | |
| R3 | 0.6.1 | 5 | 601.43 | 390.3–625.2 | 18296 | 0.10 | 7.5 | — | — | — | — |
| R3 | 0.7.0 | 5 | 601.01 | 210.6–613.5 | 18283 | 0.10 | 7.7 | — | — | — | — |
| R3 Δ | B−A | | **−0.07 %** | CI −64.98…+35.03 % | | 0.00 % | +1.67 % | | | | |

Raw per-rep values and every CI: `regressions/phase2a-data.json`. Δ is the difference of
medians (B − A); CI is a percentile bootstrap (10 000 resamples of reps; 5 000 for
interval quantiles), `scripts/p2a_analysis.py`.

### D3 — direct ceiling (the only valid ceiling point in this block)

- Throughput: **+1.83 %, CI [−1.13, +2.72]**, Mann-Whitney on per-interval samples
  (60 vs 60) p=0.216, on per-rep medians p=0.151. Not a regression. The CI's upper bound
  is inside the 2.95 % noise MDE, so a throughput regression larger than ~2.7 % is
  excluded; the point estimate is *positive*.
- Tail: worst 5 s interval **−13.94 %, CI [−32.15, +9.73]**; interval CV 15.47 % → 10.76 %;
  no 5 s interval below half the arm median in either arm (`slow 5s` 0.0 % both).
  Direction favours 0.7.0, CI includes zero → not significant. Throughput and tail rank
  the **same** way here (0.7.0 no worse), so there is no "throughput fine, tail worse"
  split to report.
- Per-rep spread shows why n=5 is the limit of resolution: 0.7.0 holds 823.07 and 793.77
  in the same arm, 0.6.1 holds 795.76–808.35. Medians separate by 15 Mb/s against ~30 Mb/s
  within-arm range.

### R2 — relay, unpaced: not a ceiling point

Both releases hit the same wall and it is not bandwidth: the subscriber stream is reset
after **~1.1 s** (`active_s` 1.1 in 9/10 reps, one 2.4 s), after ~**104.6 MB**. Burst rate
740.50 vs 745.75 Mb/s, Δ +0.71 % CI [−6.11, +5.24]; bytes-to-reset Δ −0.21 % CI
[−4.25, +104.06] (CI inflated by one 213.6 MB outlier). `active_s` is identical
(Δ 0.00 %, MWU p=1.00).

Consequence: R2 measures **burst-to-eviction**, not relay ceiling, and the eviction
threshold is release-invariant. There are **no per-interval samples** for R2 in either
arm (`samples: []`), so PROGRAM §6.9's per-interval test cannot be run — the per-rep
Mann-Whitney (n=5, p=1.00) is reported but is below protocol grade.

### R3 — relay + churn: same wall, noisier

`active_s` 0.1 s (0.2 s in 2/10), ~7.6 MB to reset, burst 601.43 vs 601.01 Mb/s,
Δ −0.07 % CI [−64.98, +35.03]. Within-arm spread is enormous (0.6.1 390.3–625.2,
0.7.0 210.6–613.5) because reset timing dominates a 0.1 s window. No significance claim
is possible; no per-interval samples exist.

## 5. Latency p50/p99 — not reported

The card asks for p50/p99 per arm. They are **not available** in this program: clock
calibration is waived for every run (cross-host offset moves > the 4.4 ms guard, 272→277 ms
this block), and `sweep.py` correctly suppresses corrected latency (`summary.p50/p99` null,
`diagnostics.raw_latency_uncorrected_do_not_quote` present). Per the program's own rule
those raw numbers must not be quoted. The tail question is instead answered with
interval-throughput statistics (iv p50/p99, CV, slow-5s fraction), which are
clock-independent. Fixing latency is a harness change (PTP or same-host clock), not a
release property.

## 6. Ranked findings

1. **No transport regression from aiopquic 0.6.1 to 0.7.0 at the direct ceiling.**
   D3 +1.83 % (CI −1.13…+2.72, interval MWU p=0.216); the CI excludes any regression
   larger than ~2.7 %. Block valid (control drift −0.24 %, inside the 2.95 % MDE).
2. **D3's tail does not diverge from its throughput.** iv p99 −13.9 % (CI −32.1…+9.7),
   interval CV 15.5 %→10.8 %: same direction as throughput, not significant, no rep
   with a catastrophic slow interval in either arm.
3. **R2 and R3 are structurally invalid as ceiling/churn points on both releases.**
   The subscriber stream is reset after ~1.1 s (R2, ~104.6 MB) and ~0.1 s (R3, ~7.6 MB).
   Deltas +0.71 % [−6.11, +5.24] and −0.07 % [−64.98, +35.03] are measuring eviction
   timing, not delivered throughput. They must not be cited as relay ceilings.
4. **The historical "relay tax −7.8 %" is retracted.** P1e's R2 764.5 Mb/s vs D3 829.4 Mb/s
   compared a 1.1 s burst against a 60 s ceiling. The correct statement is that no
   sustained relay ceiling has ever been measured in this program.
5. **The +1.8 % D3 delta is unattributable to aiopquic** because picoquic 1.1.53.0→1.1.54.0
   and picotls `f06553b8`→`e446233b` move with the release (§1). Release-to-release it is
   the answer the card asked for; component-level it is still open.

## 7. Bisect shortlist (highest value first)

1. **Arm rebuild, not a commit bisect: cmp-061 on picoquic 1.1.54.0 / picotls `e446233b`.**
   One knob, one arm, one D3 block. This is a prerequisite for *any* claim about the
   aiopquic Python layer — without it the +1.8 % stays confounded.
2. **moqx relay: the stream reset under unpaced load.** ~104.6 MB at R2 and ~7.6 MB at R3,
   identical on 0.6.1 and 0.7.0 → relay-side, release-independent. Bisect moqx between the
   P1d relay build and HEAD on stream-limit/`MaxStreamsUni` eviction. Highest program value:
   it restores two of the three transport points.
3. **Only if (2) implicates the client: aiopquic `v0.6.1..v0.7.0`**, filtered to credit /
   `MaxStreamsUni` replenishment. Predicted low yield, since the eviction threshold is
   arm-invariant to within 0.2 %.

Nothing has been bisected. Per the card, this phase only ranks the candidates.

## 8. Provenance

- Branch `perf/release-sweep`; harness `scripts/sweep.py`, `scripts/p2a_driver.sh`,
  `scripts/p2a_analysis.py`; manifest `manifests/phase2a.yaml`.
- Run blocks: 30 dirs `runs/2026-11-15T18-0[4-9]*Z-p2a-*`, `runs/2026-11-15T18-[1-5]*Z-p2a-*`
  enumerated in `runs/p2a-runs.txt` (30/30 `rc=0`), driver log `runs/p2a-driver.log`
  (`DRIVER COMPLETE 2026-11-15T13:51:15-05:00`).
- Gap between reps: 0 s at block level (each rep is its own `sweep.py` invocation with its
  own 10 s warm-up + `--gap 0`); 94 s per rep end-to-end.
- Analysis is reproducible from raw `result.json` only:
  `python3 scripts/p2a_analysis.py`.
