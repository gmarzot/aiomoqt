# Phase 1d — relay egress baseline (R1..R6)

Status: **partial** — R1..R4 measured, R5/R6 still running when this file was
written. Blocks: `runs/2026-10-30T23-42-43Z-p1d` (R1, 3 reps) and
`runs/2026-10-30T23-57-24Z-p1d-b2` (R2..R6, 3 reps each).

## Setup

- Relay: program's own moqx on **port 9768**, config
  `perf/release-sweep/moqx-perf.yaml`, CPU pinned 0-1. The shared instance on
  **:9668 was never touched** (verified with `relay_probe` before each rep).
- Publisher co-located with the relay on shenzhen; subscribers on seawolf
  (`load` host, 6 cores).
- Fan-out stagger: the repo carried no documented stagger value, so the harness
  grew an explicit knob — `sub_stagger_s: 0.5` in the relay block of
  `manifests/baseline-v0.yaml`, honoured per point, recorded per joiner in
  `fanout.json` (`join_index`, `t_join_s`). R4 (5 subs) joined at +2.3 s .. +4.4 s,
  i.e. joiners 0..4 at 0.5 s spacing.
- Measurement window: 60 s per rep, 10 s gaps. Relay CPU/bytes come from a 1 Hz
  out-of-band sampler (`scripts/relay_sampler.py`, `/proc/<pid>/stat` for CPU,
  `moqx_quicBytes*_total` for bytes, host NIC counters as cross-check); delivered
  Mbps come from the subscriber's own log.

## Results (delivered Mbps is the subscriber's own number)

| point | rep | subs | offered obj/s | sub mean Mb/s | sub total Mb/s | relay CPU (cores) | relay bytesWritten | relay cores/Mbps |
|---|---|---|---|---|---|---|---|---|
| R1 | 1 | 1 | 6104 (200 Mb/s) | 197.64 | 197.64 | 0.175 | 146.5 Mb/s | 0.00089 |
| R1 | 2 | 1 | 6104 | 197.18 | 197.18 | 0.175 | 146.5 Mb/s | 0.00089 |
| R1 | 3 | 1 | 6104 | 201.00 | 201.00 | 0.181 | 151.6 Mb/s | 0.00090 |
| R2 | 1 | 1 | max | 759.91 (1.0 s burst) | 759.91 | n/r | n/r | — |
| R2 | 2 | 1 | max | 765.38 (1.3 s burst) | 765.38 | n/r | n/r | — |
| R2 | 3 | 1 | max | 736.79 (1.1 s burst) | 736.79 | n/r | n/r | — |
| R3 | 1 | 1 | max (P=8, g=40) | 587.40 (0.4 s burst) | 587.40 | n/r | n/r | — |
| R3 | 2 | 1 | max | 606.38 (0.4 s burst) | 606.38 | n/r | n/r | — |
| R3 | 3 | 1 | max | 629.53 (0.4 s burst) | 629.53 | n/r | n/r | — |
| R4 | 1 | 5 | 6104 (200 Mb/s) | 169.77 | 848.84 | 0.161 | 253.7 Mb/s | 0.00019 |
| R4 | 2 | 5 | 6104 | — pending — | | | | |
| R4 | 3 | 5 | 6104 | — pending — | | | | |
| R5 | — | 20 | 12207 (400 Mb/s) | — pending — | | | | |
| R6 | — | 5 (WT) | 6104 | — pending — | | | | |

`n/r` = the rep lasted ~1 s; a 1 Hz sampler cannot resolve it and the point's
own summary carries no relay CPU. Nothing in this table is interpolated.

## Reading

**The relay is not the wall at 200 Mb/s.** One subscriber at 197.6 Mb/s of
delivered payload costs 0.175-0.181 core of relay CPU (R1, 3/3 reps, spread
1.7%), i.e. ~0.9 millicore per Mb/s egress. Fan-out does not change that slope
much: R4 delivered 848.8 Mb/s aggregate across 5 subscribers for 0.161 core —
cheaper per bit than R1, because one fan-out pass replaces per-subscriber work.
At that rate the relay would need roughly 1 core to saturate a 1 Gbps link, and
the load host has 6 cores with the publisher and relay co-located, so relay CPU
is not the limiting resource in any R point measured.

**The wall is the 1 Gbps path, and R4 already sits on it.** R4's fan-out ratio
(aggregate delivered ÷ offered) is 848.8 / 200 = **4.24 of a possible 5**, and
its aggregate is **90% of the 941 Mb/s MTU-1500 payload ceiling** of the Realtek
GbE that owns 10.14.25.173 — the same ceiling Phase 1c found from D3. Per
subscriber that is 169.8 Mb/s instead of 197.6, and at least one subscriber in
the R4 smoke lost 56% of its objects. So R4 is not measuring fan-out scaling; it
is measuring the link's ability to carry 5 copies, and it runs out. **R5 (20
subs × 400 Mb/s offered = 8 Gb/s of egress demand on a 1 Gbps link) is
over-subscribed by ~8.5x by construction**; whatever it reports is a
link-limited floor, not a relay limit, and must be read with that in mind.

**The relay's own byte counters cannot be used as an absolute egress meter in
this build.** Everywhere both meters worked they read ~25-30% below what the
subscriber actually received: R1 146.5 Mb/s (relay `moqx_quicBytesWritten_total`)
and 151.4 Mb/s (host NIC tx) against 197.6 Mb/s delivered; R4 253.7 Mb/s and
262.4 Mb/s against 848.8 Mb/s. The relay counters are accumulated from picoquic
path-quality deltas (`src/stats/PicoQuicStatsCollector.cpp:72-85`) and evidently
do not cover every forwarding path. They are recorded, not trusted: the
subscriber-side totals are the primary egress measurement, and the relay counters
are only meaningful as *ratios within one rep*. Relay CPU (from `/proc`) is
sound and is used above.

**Fan-out and tail latency.** R1's tail is flat (no loss, no out-of-order at
197 Mb/s into one subscriber). R4's aggregate is where the link saturates, and
the smoke rep showed the described signature: delivered rate per subscriber
drops (169.8 mean vs 197.6 solo), loss appears on at least one subscriber
(56.45% on that smoke), and latency stops being a network property and becomes
queue occupancy. The 20-subscriber point will push that further; it is also
CPU-constrained on the load host (20 subscriber processes on 6 cores), so its
tail must not be read as pure fan-out scaling.

**Relay-ceiling points abort rather than ceiling.** R2 (`r: 0`, unpaced) and R3
(`P=8, g=40`, unpaced) do not settle at a rate: the relay **resets the
subscriber stream** — `stream 2047 reset by peer: error=3` — after 0.4-1.3 s,
reproducibly 3/3 in both points. The subscriber had taken 19,412 objects
(79.8 MB) at a 736-765 Mb/s *burst* in R2 and 587-630 Mb/s in R3 when the reset
arrived; the publisher logged `Sent: 26,800 objects ... 1.9Gbps (0.5s)` then
`Done.` These numbers are startup bursts, not sustained throughput, and must not
be quoted as a relay ceiling. The finding is the abort: **under an unpaced
publisher this relay tears the subscription down instead of applying
backpressure**, so the R2/R3 design as authored cannot produce a 60 s ceiling
measurement. That is a stack finding, not a NIC or CPU one.

**Clock caveat (carried from Phase 1c, not fixed).** The sweep ran with
`--waive clock_offset_jump,clock_unavailable`; the cross-host offset still moved
up to 373,493 us inside a single point against a 4,496 us tolerance. p50/p95/p99
are **null on every rep** in this file. Nothing above depends on them, but the
latency story for Phase 1d is still unmeasured and stays open.

## Open items

1. R5 (20 subs) and R6 (WT fan-out) — running; publish per-sub logs and the
   fan-out ratio when complete. R5 doubles as the CPU-contention check (20 subs
   on 6 cores), which the current table does not yet show.
2. R2/R3 need a *paced* sweep (or a relay build that applies backpressure
   instead of resetting) before a relay ceiling can be stated at all.
3. Relay byte counters undercount; if absolute egress is a requirement, meter at
   the subscriber (already done here) or fix the path-quality delta accounting.
