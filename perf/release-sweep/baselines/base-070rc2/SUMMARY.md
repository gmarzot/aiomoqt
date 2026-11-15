# base-070rc2 — consolidated baseline (Phases 1a–1e)

Consolidates the screening matrix of `PROGRAM.md` §7 into one table and one set of
answers. Every number below is a median over the point's reps in the canonical run
block for its phase; every claim cites a point id. Raw per-rep rows are in
`rollup.csv` next to this file.

- **Arm:** `base-070rc2` — aiopquic **v0.7.0rc2 @51a80c9**, aiomoqt `perf/release-sweep`, CPython **3.14.6**.
- **Hosts:** shenzhen (WSL2, 6-core load host, publisher + relay) ↔ seawolf (Mac mini M4, subscribers) over **1 GbE**.
- **Relay:** `moqx` on shenzhen, `:9768`, `bbr1`, GSO on.
- **Methodology:** `PROGRAM.md` §3 (metrics), §7 (matrix); noise floors and MDE from `noise-floor.md`.
- **Blocks:** N/L/P `2026-10-30T21-40-50Z-partA` + `…22-04-13Z-partB`; D/Ri `…22-56-13Z-p1c2`; R `…23-42-43Z-p1d`, `…23-57-24Z-p1d-b2`, `…16-47-31Z-p1d-b3`; M/C **`2026-11-11T02-33-51Z`**.

## 0. Read this before the table

**Status vocabulary.** `ok` = clean. `dirty` = ran to completion with a waived
hostcheck violation (recorded in the rep's `result.json`); throughput and TTFF are
unaffected, cross-host one-way latency is not measured. `failed`/`aborted_*` =
excluded from medians, kept in `rollup.csv`.

**Why so many reps are `dirty`.** Every cross-host rep in this program carries a
waived `clock_offset_jump`: seawolf's clock estimate moves 220–310 ms during a 60 s
point with a 4 ms RTT (`noise-floor.md` §Caveat), which breaks wall-clock one-way
latency but nothing else. N1/N2 (14 reps), D1–D5, R1–R6 and all of Phase 1e run
under that waiver. **Consequence: cross-host p50/p95/p99 exist only where the
offset happened to be stable** — in practice R4 (pre/post drift 4 ms) and the
same-host L/P points. Every other cross-host latency cell is `null`, not zero.

**Release-basis delta, stated not papered over.** This table was measured on
**v0.7.0rc2**, while the current program basis is the final releases (aiomoqt
0.14.0, aiopquic v0.7.0). The gap is **1 commit**: `51a80c9..v0.7.0` = `7baed8b`
("0.7.0: bbr v3 default, RX/TX wake fixes, picoquic 3012b39c, RESET_STREAM_AT"),
27 files, mostly tests plus **picoquic/picotls submodule bumps** and a default-CC
change. The picoquic bump means the transport under test is *not* bit-identical to
final; the CC default change matters because every point here pins `cc: bbr1`
explicitly, so the default flip does not reach these runs. Re-anchoring the
headline points on the final tags is Phase 2 step 0, not a silent rename here.

**n.** Screening points are N=3. Media (M1, M2) N=3. Churn storm (C1) and late
join (C2) are **N=2**, and the steady-state control **C0 is N=1** — **no CI is
quoted below n=3**: at n=2 the two raw values are shown side by side, and at n=1
there is no dispersion statement at all. C0's single rep is why §2.5 and §4 quote
the churn cost against it rather than as an absolute.

## 1. Headline table

Medians. CI = bootstrap 95% over reps, shown only where n≥3 and n_ok≥2.

| point | what it is | offered | n_ok/n | median Mb/s | 95% CI | CV % |
|---|---|---|---|---|---|---|
| **L1** | loopback, 1 sub, unpaced | line | 3/3 | **1714.8** | 1424.4–1767.5 | 11.33 |
| **L2** | loopback, P=8, unpaced | line | 3/3 | **1731.8** | 1715.5–1731.8 | 0.40 |
| **L3** | loopback, WT, P=8 | line | 3/3 | **1715.8** | 1691.6–1744.9 | 1.55 |
| **L4** | loopback ceiling + churn g=40 | line | 3/3 | **1458.3** | 1393.1–1503.6 | 3.83 |
| **L5** | loopback ceiling, s=1200 (pps) | line | 3/3 | **943.9** | 925.3–971.2 | 2.44 |
| **P1** | two-process, no network, P=1 | line | 3/3 | **1728.2** | 1671.7–1736.0 | 2.26 |
| **P2** | two-process, "paced 20 Mb/s" (mis-scaled, see §5) | 20 | 3/3 | **657.5** | 657.44–657.45 | 0.00 |
| **P3** | two-process, WT, P=8 | line | 3/3 | **1744.4** | 1728.5–1768.6 | 1.73 |
| **D1** | direct, 1 sub @300 Mb/s | 300 | 3/3 | **286.4** | 285.1–287.5 | 0.35 |
| **D2** | direct, 1 sub @600 Mb/s | 600 | 3/3 | **577.4** | 574.9–579.7 | 0.41 |
| **D3** | direct, unpaced (ceiling) | line | 3/3 | **829.4** | 794.1–864.4 | 3.72 |
| **D4** | direct ceiling + churn g=40 | line | 3/3 | **785.9** | 784.0–791.6 | 0.50 |
| **D5** | direct, WT, 1 sub @600 | 600 | 3/3 | **569.8** | 569.27–572.30 | 0.44 |
| **Ri1** | ring, 1 hop, 1 sub @300 | 300 | 3/3 | **318.0** | 317.5–318.4 | 0.13 |
| **R1** | relay, 1 sub, controlled | 200 | 3/3 | **197.5** | 193.9–201.0 | 1.01 |
| **R2** | relay, 1 sub, unpaced (ceiling) | line | 3/3 | **764.5** | 746.8–782.2 | 2.15 |
| **R3** | relay ceiling + churn g=40 | line | 3/3 | **606.4** | 553.9–628.7 | 3.47 |
| **R4** | relay, 5 subs fan-out | line | 3/3 | **200.8** | 112.4–200.9 | 29.80 |
| **R5** | relay, 20 subs ×50 Mb/s (over-driven) | 1000 | 3/3 | **154.1** | 138.5–154.2 | 6.05 |
| **R6** | relay, 5 subs fan-out, WT | line | 3/3 | **119.8** | 102.0–140.5 | 33.94 |
| **R5b** | relay, 20 subs ×50 Mb/s (suppl.) | 1000 | 2/3 | **47.5** /sub | 47.44–47.57 | 0.19 |
| **N1** | D-raw reference, P=4 unpaced (P1b) | line | 7/7 | **811.7** | 802.1–826.2 | 2.38 |
| **N2** | M1 media shape, 1 sub (P1b) | ~4.8 | 7/7 | **4.83** | 4.82–4.84 | 0.27 |
| **M1** | 1080p, 1 sub, 120 s | ~4.8 | 3/3 | **4.82** (4.82, 4.81, 4.83) | 4.81–4.83 | 0.21 |
| **M2** | 720p, 5 subs, 120 s | 12 | 3/3 | **2.37** per sub (2.38, 2.37, 2.37), **11.9** aggregate | 2.37–2.38 | 0.24 |
| **C0** | 50 slots, steady, 0 churn (control) | 200 | 1/1 | **200.7** | n=1 | — |
| **C1** | churn storm, 50 slots, 300 s | 200 | 2/2 | **176.2** (176.31, 176.07) | n=2 | 0.10 |
| **C2** | late join t=30 s, 90 s | 200 | 2/2 | **TTFF 37.7 ms** (38.12, 37.37); no Mb/s median — one 1080p track | 37.4–38.1 ms | 1.41 |

Per-point config (P, s, g, pacing, subscribers, duration) is the manifest:
`manifests/baseline-v0.yaml` for N…R, `manifests/phase1e.yaml` for M/C.
Per-rep rows, including the excluded ones: `rollup.csv`.

## 2. What the baseline says

### 2.1 Where the loopback ceiling is, and what binds it

**1.73 Gb/s, and it is one core of user-space QUIC, not the loopback device.**

- L2 (P=8, unpaced) = **1731.8 Mb/s**, CV 0.40% over 3 reps — the tightest
  throughput point in the whole matrix.
- Going from P=1 to P=8 buys **+1.0%** (L1 1714.8 → L2 1731.8): adding publisher
  concurrency does not lift the ceiling, so the limit is per-connection and
  serial, not aggregate scheduling.
- WebTransport is free at the ceiling: L3 = 1715.8, **−0.9% vs L2** (inside the
  3.8% link-limited floor, i.e. no measurable difference).
- The subscriber side is the busy side: L2 rep CPU sub 106.5% of a core, relay
  2.1%. At 1.73 Gb/s the receive path is the constraint — measured on one host,
  so no clock correction and no `null` latency.
- It is NOT the loopback device and NOT the wire: the *same* stack, moved onto
  the 1 GbE pair, delivers 829.4 (D3), i.e. **−52.1%**.

**Small objects hit a different wall.** L5 (s=1200 B) = 943.9 Mb/s,
**−45.5% vs L2**: at 1.2 KB the point becomes packet/object-rate bound
(~98 k obj/s) long before the byte ceiling.

### 2.2 How far two-node direct falls from loopback, and whether that is the NIC

**Direct reaches 829.4 Mb/s = 47.9% of loopback (−52.1%), and yes, that is the
link.** D3 unpaced = 829.4 Mb/s = **82.9% of a 1000 Mb/s line rate**, which is
where a 1 GbE path saturates once framing, ACK traffic and pacing are counted.
The stack is not the limit: the same code did 1731.8 on loopback (L2).

The two-node numbers reproduce across blocks and days, which is why the NIC
reading is safe:

- N1 (P1b, D-raw, P=4, unpaced, n=7) = **811.7** vs D3 (P1c, n=3) = **829.4** →
  **−2.1%**, inside N1's own 3.8% MDE. Two independent blocks, same ceiling.
- Paced points land where you would expect: D1 **286.4** at 300 offered (−4.5%),
  D2 **577.4** at 600 (−3.8%). The ~4% pacing shortfall is the pacer's own
  granularity plus per-object headers, and it is stable to CV 0.35–0.41%.
- WT costs nothing on the wire either: D5 **569.8** vs D2 577.4 = **−1.3%**.

### 2.3 What the relay costs on top of direct, at equal offered rate

**At a controlled rate: 1.3%, i.e. nothing measurable. At the ceiling: 7.8%,
which is real but small — and it is not the relay's CPU.**

- **At equal offered rate (200 Mb/s, 1 subscriber):** R1 delivers **197.5 Mb/s**
  = **−1.3% vs offered** (CI 193.9–201.0, CV 1.01%), indistinguishable from the
  direct paced points' −3.8% to −4.5% (D1, D2). Add a relay hop at a controlled
  rate and the subscriber cannot tell.
- **At the ceiling:** R2 **764.5** vs D3 **829.4** = **−7.8%**, i.e. 76.5% vs
  82.9% of line rate. A 7.8% tax is outside the 3.8% link-limited floor at N=3,
  so it is a claim — but a narrow one (D3 CV 3.72%).
- **It is not relay CPU:** R2 mean **0.98 core**, R3 **0.18**, R4 **0.32** — the
  relay is idle at every rate this rig can drive. That confirms Phase 1d's finding
  (P1d: relay CPU 0.11–0.25 core at fan-out) and rules out CPU as the mechanism.
- The relay's real cost is **fan-out**, which is a correctness/eviction problem,
  not a throughput tax — see 2.4.

### 2.4 How fan-out changes tail latency

**Fan-out does not degrade the median much; it destroys the tail, and it does so
bimodally because subscribers get evicted.**

- R4 (5 subscribers, relay) median **200.8 Mb/s** per subscriber, but CI
  **112.4–200.9** and **CV 29.8%**: the point is not noisy, it is *bimodal*.
  Phase 1d showed why: the relay evicts 25–40% of subscribers in every fan-out
  configuration (R4 3/5, R6 3/5, R5 1/20, R5b 14/15/14 of 20) because a
  per-connection QUIC uni-stream budget of 1024 is never replenished
  (`MaxStreamsUni "Unhandled"`), escalated to `removeSubscriberOnError`, clustered
  at ~35 s. A rep where 2 of 5 are evicted reports ~1/5 less delivered bytes.
- **Tails, where latency is measurable at all** (same-clock / stable-offset points
  only): R4 p50 **32.1 ms**, p95 **126.9 ms**, p99 **268.1 ms**, against L2's
  **15.3 / 21.8 / 23.9 ms**. p95 ×4.8, p99 ×10.2. Median roughly doubles; the tail
  is an order of magnitude worse. Judged against the same-host floor this is a
  large effect; it is *not* a clean fan-out-only number, because 3 of 5 R4 reps had
  evictions and eviction itself perturbs the remaining subscribers.
- R6 (same shape, WT) is no better: **119.8** median, CV 33.9% — WT does not
  mitigate eviction.
- Data rate does not fix it, it postpones it: R5 (20 × 50 Mb/s, 3/3 reps usable)
  delivers **154.1** median per subscriber, CV 6.05%; the supplementary R5b holds **47.5 Mb/s per
  subscriber × 20 = 950 Mb/s aggregate = 95% of the 1 GbE line** — the aggregate is
  at line rate while 14–15 of 20 subscribers are evicted. **Fan-out on this build is
  bounded by the stream-credit leak, not by bandwidth.**

### 2.5 What churn costs at fixed rate

**5% on the wire, 16% on loopback, 21% at the relay ceiling — churn is cheap until
the transport is already saturated, then it is the most expensive thing measured.**

Same shape, only `g` (group/churn) changed within each pair:

| pair | without churn | with churn | delta |
|---|---|---|---|
| loopback, P=8 | L2 **1731.8** | L4 **1458.3** | **−15.8%** |
| direct, at ceiling | D3 **829.4** | D4 **785.9** | **−5.2%** (CI 784.0–791.6) |
| relay, at ceiling | R2 **764.5** | R3 **606.4** | **−20.7%** |

Reading: on the wire the link still has headroom at 785.9 Mb/s, so churn costs
only **5.2%** — and unlike D3 this is a tight point (D4 CV 0.50%, CI 784.0–791.6),
so the direct-churn cost is solid even though the direct *ceiling* it sits under is
not. On loopback, where there is no idle link to absorb the churn-induced gaps,
the same logical load costs **15.8%** (L4 vs L2, CV 3.83% vs 0.40% — solid).
Through the relay at the ceiling it costs **20.7%** (R3 vs R2), the largest
churn penalty in the matrix, consistent with 2.4: relay churn interacts with the
stream-credit path, not with bandwidth.

## 3. Media (1080p / 720p)

The media points measure the **media profile**, not the transport: M1 and M2 are
publisher-paced at the 1080p/720p profile rate, far below every transport ceiling
in §2, so a transport regression cannot show up here as throughput. They exist to
catch CPU/scheduling effects at sub-percent scale — N2's CV is 0.27%, MDE N=3
**0.43%**, the tightest floor the program has.

| point | shape | per-sub rate | n | reps | spread | aggregate egress |
|---|---|---|---|---|---|---|
| M1 | 1080p, 1 sub, 120 s | **4.82** Mb/s | 3 | 4.82 / 4.81 / 4.83 | 0.21% | 4.82 Mb/s |
| M2 | 720p, 5 subs, 120 s | **2.37** Mb/s | 3 | 2.38 / 2.37 / 2.37 | 0.24% | **11.9** Mb/s (5 × 2.37) |

M2's `mbps` is **per subscriber**, not aggregate: each rep's byte count is
35.6 MB over 120 s = 2.37 Mb/s for one stream, and every one of the five
subscribers receives the full track. Objects per rep are 3,457–3,468 (M2) and
3,463–3,466 (M1) — the same object cadence, different object size
(10.3 KB at 720p vs 20.9 KB at 1080p), which is what a video-profile change
should look like and nothing else.

- M1 sits at **4.82** vs N2's **4.83** (7 reps, CV 0.27%, Phase 1b) = **−0.2%**,
  inside N2's own 0.43% MDE. Five subscribers cost nothing measurable:
  M2 −50.7% vs M1 is the 720p profile, not fan-out.
- **Latency was not measured for M1 or M2, and no latency number is claimed.**
  The cross-host clock offset moved mid-point in every rep (M1: 377 / 520 / 725 ms;
  M2: 67 / 114 / 120 ms) against a 3.8–4.2 ms tolerance, so `p50/p95/p99` are
  recorded as `not_measured` with that reason in each `result.json`. The
  rig cannot do cross-host one-way latency; see §5.
- Consequence for Phase 2a: media points are a **throughput-only** regression
  detector at 0.43% resolution. Any 0.6.1 media regression that manifests only as
  added latency is invisible to M1/M2 as currently instrumented.

## 4. Churn storm (C1) and late join (C2)

C1 is 50 subscriber slots at 4 Mb/s = **200 Mb/s offered egress**, `churn_rate
0.05`, `leave_early 20`, `leave_rate 10`, publisher 300 s. C0 is the control:
identical publisher and slots, **all churn rates zero**, so C1 − C0 isolates churn
at a fixed offered rate. Both ran at the full 50-slot scale — no reduction was
needed, and none was made.

| point | n | delivered Mb/s | % of 200 offered | interval CV | p90 mean/max ms | joins | churns | delivery_frac |
|---|---|---|---|---|---|---|---|---|
| **C0** | 1 | **200.7** | 100.3% | 1.0% | 4.9 / 6 | 50 | 0 | 1.0026 † |
| **C1** | 2 | **176.2** (176.31 / 176.07) | 88.1% | 4.2% / 3.5% | 8.0 / 41 · 6.4 / 29 | 967 | 933 ‡ | 0.9997 / 0.9995 |

† C0's `delivery_frac` **exceeds 1** (1.0026), which is physically impossible and
is an accounting artifact of summing per-interval subscriber byte deltas against a
nominal published rate; read it as "no loss", not as 0.26% gain. C1's 0.9997 sits
on the other side of the same rounding, so the honest statement is that the two
are indistinguishable from 1.0, not that churn saves or costs bytes.

‡ 933 is the generator's own churn counter over the whole run. The interval series
in `load.log` sums to **926** because its steady-state window starts at t=15 s and
drops the teardown row; the 7-event difference is warm-up churn, not a disagreement.

**Churn costs 12.2% of delivered rate at a fixed 200 Mb/s offered**
(C0 200.7 → C1 176.2, n=1 control vs n=2). The two C1 reps agree to **0.14%**
(176.31 vs 176.07) with **identical 967 joins / 933 churns** — the churn schedule
is seeded, so this is a repeat of one schedule, not two independent draws; the
12.2% is a property of *that* schedule, and the n=2 agreement says the rig is
reproducible, not that the number generalises to other churn rates.
`delivery_frac` stays at **0.9997 / 0.9995** across 933 churns: almost nothing is lost —
the cost is paid in **rate and tail**, not in dropped objects. p90 mean rises
4.9 → 8.0 / 6.4 ms with 41 / 29 ms excursions, i.e. the churn cost shows up as
per-interval dips that the 300 s mean averages down to 12%.

Two caveats, stated because they bound the claim:

- **The load host is CPU-constrained at 50 subscribers.** It is a 6-core box
  running 50 subscriber processes plus the churn generator. C0 runs the same 50
  processes, so the *delta* is churn work, not subscriber count — but the
  absolute 176.2 is a load-host-limited number, not a relay limit. The relay's
  own CPU stayed far below one core throughout (relay CPU <1.0 core in every §2
  point), so nothing here is relay-bound.
- The churn interval is 5 s with ~17 churn events per interval (967 joins over
  300 s); this does **not** probe sub-second churn.

### Late join (C2): TTFF and catch-up

Late joiner subscribes at **t=+30 s** into a 1080p publisher already running;
a warm subscriber joins at **t=+2 s** in the same rep as a same-rep control.
TTFF = SUBSCRIBE sent → first object arrival, both stamps taken on the
subscriber host (`time.time()` in `ttff_probe.py`), so the number does not
depend on the cross-host clock offset that voids M1/M2 latency.

| rep | late TTFF ms | warm TTFF ms | late-warm | bytes | objects | first_group | first_object_bytes | first second |
|---|---|---|---|---|---|---|---|---|
| 01 | **38.12** | 36.05 | +2.07 | 50,143,228 | 2,488 | 28 | 10,013 | 415 KB |
| 02 | **37.37** | 35.94 | +1.43 | 50,158,230 | 2,488 | 27 | 10,013 | 615 KB |

**Late-join TTFF = 37.7 ms median (38.12 / 37.37, n=2, no CI)**, against a warm
join in the same reps of 36.0 ms (36.05 / 35.94) — **+1.75 ms** for joining 28 s
late. The late joiner then receives **50.15 MB in 80.3 s** (2,488 objects, both
reps byte-identical to 0.03%) at the live 4.8 Mb/s profile rate: **catch-up
bytes = 0**. There is no catch-up burst — the first second carries 415 / 615 KB
against a 600 KB/s steady state, and the first object is a normal 10 KB media
object, not a burst — so 0.7.0 serves a late joiner from the live edge and
retains no history to replay. That is the property Phase 2a must preserve: a
0.6.1 comparison that starts replaying groups on join will show up here as
first-second bytes ≫ 600 KB.

Provenance: the two C2 reps on disk from the 2026-11-11 sweep are **invalid** —
`pub_bench` in 0.7.0 announces the namespace only under `--pub-ns` **or
`--pub-both`** (`track.publish(announce_namespace=(args.pub_ns or args.pub_both))`),
and `scripts/sweep.py` passed neither, so every subscriber got
`code=4 reason=no such namespace or track`. The C2 numbers above were taken
2026-11-15 by `scripts/c2_latejoin.sh` with `--pub-both` and a warm subscriber
(publisher exits rc=0 if no subscriber is present before it starts), and are
recorded as `result.built.json` beside the stale sweep `result.json`.

Provenance, C0/C1: the 2026-11-11 sweep's C0/C1 summaries were voided by a `Path`
`NameError` in `collect.py` (fixed in `d7552cb`), which wrote no summary at all
rather than a wrong one. From the `t0_reference` stamps: C1 rep-01's raw `load.log` is from
**2026-11-11T02:56–03:01Z** and its summary was rebuilt from those logs on
2026-11-15; C1 rep-02's logs are a **full re-run at 2026-11-15T17:31Z** because the
original had no usable logs; C0's single rep is a **fresh run at
2026-11-15T17:36Z**, which is why C0 is n=1 and not the n=2 the sweep intended.
C1's n=2 is therefore **one rep per session, four days apart** — the 0.14%
agreement (176.31 vs 176.07) is a session-to-session reproducibility result, which
is the stronger form of the claim, but it is also only two sessions and shares the
seeded churn schedule. C0 has no such corroboration.

## 5. Confidence — what is solid, what is at the noise floor

Floors from `noise-floor.md`: link-limited throughput at N=3 must clear **3.8%**
(N1 CV 2.38%); media-paced at N=3 must clear **0.43%** (N2 CV 0.27%);
cross-host one-way latency is **not measurable** without a stable offset;
`interval_cv` must never be used at N<10 (its own CV is 36–72%).

**Solid — effect ≫ floor, CV < 4%, mechanism identified:**

| claim | numbers | why it holds |
|---|---|---|
| loopback ceiling ~1.73 Gb/s, single-connection | L2 1731.8 (CV 0.40), L1/L3 within 1% | CV 10× below floor |
| two-node direct ceiling = line rate, not stack | D3 829.4 = 82.9% of 1 GbE; N1 811.7 (−2.1%) | two blocks agree |
| paced points deliver ~96–99% of offered | D1 −4.5%, D2 −3.8%, R1 −1.3% (CV ≤1.01) | CV ≤1% |
| WT is free in every same-rate pair | L2→L3 −0.9%, D2→D5 −1.3% | inside floor |
| small objects are pps-bound | L5 943.9 = −45.5% vs L2 | huge effect |
| churn: loopback / direct / relay ceiling | L4 −15.8%, D4 −5.2%, R3 −20.7% | ≫ 3.8% floor, CV 0.50–3.83 |
| relay is not CPU-bound | R2 0.98, R3 0.18, R4 0.32 core | 6-core host, <1 core |
| media is transport-insensitive | M1 4.82 (n=3), N2 4.83 (−0.2%) | inside 0.43% floor |

**At or near the noise floor — quote as "small", never as an exact percentage:**

- **Relay ceiling tax** R2 vs D3 = −7.8%: outside the 3.8% floor but only 2× it,
  with D3's own CV at 3.72%. Directionally reliable, magnitude ±4%.
- **Ring, one hop** Ri1 = 318.0 (CV 0.13%, CI 317.5–318.4) — tight in itself, but
  there is no 2-hop point and no matching direct point, so it is reported as a
  single datum, not as "the cost of a hop".
- **L1 cold start**: CV 11.29% (first rep 1424.4, then 1715–1767). L2 is the
  trustworthy loopback ceiling; L1 is quoted only for the P=1→P=8 delta, which is
  +1.0% despite that variance.
- **P2 is mis-scaled**: labelled "paced 20 Mb/s" but reports **657.5 Mb/s**
  (CV 0.00%, CI 657.44–657.45), so its pacer never engaged — the same kbit/s vs
  objects/s unit error the manifest header warns about. It is excluded from every
  claim above and needs re-authoring before it means anything.

**Not usable as a central tendency — report as a distribution:**

- **R4** (CV 29.80%) and **R6** (CV 33.94%) medians are meaningless on their own:
  they average over reps with different eviction counts. Their value is the
  eviction rate and the tail, not the median. Likewise R5/R5b (1 of 3 reps
  usable) — directional only.
- **Cross-host latency** for D\*, R1, R2, R3, R5, R5b, N1, N2, M1, M2 is `null`
  by design (clock drift 220–310 ms). The only valid cross-host tails in this
  document are R4's. L/P tails are valid (same clock domain).

**n=2, no CI: C1, C2.** Two values are reported side by side and the spread
between them is the only uncertainty statement available. **C0 is n=1** — a single
control rep, no dispersion statement possible — which is why C1's cost is quoted
against C0's control rather than as an absolute, and why the 12.2% inherits
whatever C0's single run happened to be, including its 1.0026 `delivery_frac`
artifact. Treat ±1% as the plausible error on the churn cost, not the 0.14% that
C1's own two reps agree to.

**Not claimed at all:** any benefit of WebTransport (only its cost is measured),
relay behaviour for >20 subscribers, anything about the observability stack
(ClickHouse/G5 are out of scope per the 2026-11-02 note), and bit-identical
equivalence to the final v0.7.0 tag (§0).

## 6. What would change these conclusions

1. A stable cross-host clock (or a QUIC-RTT-based latency definition) — would turn
   every `null` tail cell into data and is the single largest information gain.
2. The stream-credit fix (`MaxStreamsUni` replenishment) — R4/R6/R5b are all
   measuring that bug, not fan-out capacity. Re-run R4/R6 at the same load after the
   fix and the fan-out section becomes a capacity statement.
3. Re-anchoring on the final v0.7.0 / 0.14.0 tags (§0).
