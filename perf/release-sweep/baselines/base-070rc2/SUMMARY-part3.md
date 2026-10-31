# Phase 1d — relay egress baseline (R1..R6)

Status: **complete for R1..R6** (3 reps each). R5b is a supplementary point added
to separate "fan-out" from "over-subscription by construction"; R6b was specified
but **not run** (see *R6b*). Blocks: `runs/2026-10-30T23-42-43Z-p1d` (R1, 3 reps),
`runs/2026-10-30T23-57-24Z-p1d-b2` (R2..R6, 3 reps each),
`runs/2026-10-31T16-47-31Z-p1d-b3` (R5b, 3 reps).

**Headline: the relay is not the wall and neither is the link. The relay evicts
25–40% of subscribers in every fan-out configuration tested — at 50, 200 and
400 Mb/s per subscriber, with 5 or 20 subscribers, raw or WebTransport — because a
per-connection QUIC uni-stream budget of 1024 is never replenished, and the relay
escalates the resulting transient block into permanent subscriber removal.
Delivered throughput is the surviving subscribers' rate; fan-out's measured cost is
membership loss, not a raised tail.**

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
  i.e. joiners 0..4 at 0.5 s spacing; R5/R5b (20 subs) spread over +0 .. +9.5 s.
  `run_subs()` starts all joiners concurrently into their slots, so the 60 s window
  is shared, not serialised (measured joins in R5 were 2.3–12.4 s, concurrent).
- Measurement window: 60 s per rep, 10 s gaps. Relay CPU/streams/flow-control
  come from a 1 Hz out-of-band sampler (`scripts/relay_sampler.py`,
  `/proc/<pid>/stat` for CPU, `moqx_quicBytes*_total` for bytes, host NIC
  counters as cross-check); delivered Mbps come from the subscriber's own log.
  `relay_sampler.py` is **standalone** — `sweep.py` neither starts nor stops it.

### How a rep was accepted

A subscriber counts as **sustained** only if it produced ≥ 8 of the twelve
5-second interval lines (i.e. it was still receiving at ~40 s). Subscribers with
1–2 interval lines are startup bursts and are never averaged into a sustained
figure; subscribers with zero interval lines never received at all. `agg_all`
(used only for the 20-subscriber points) sums every subscriber with ≥ 3 intervals,
because the 0.5 s stagger means the last joiner starts ~9.5 s in and can only
produce ~10 intervals inside a 60 s window.

## Results

Delivered Mbps is the subscriber's own number. `full` = sustained subscribers of
`subs` launched.

| point | rep | subs | full | delivered Mb/s (sustained) | agg, all subs ≥3 int | relay CPU (cores) | relay cores per Mb/s | relay sub-streams peak | fcBlock stream/conn | loss on sustained | loss on dropped |
|---|---|---|---|---|---|---|---|---|---|---|---|
| R1 | 1 | 1 | 1 | 197.64 | 197.6 | 0.173 | 0.00088 | 7 | 0 / 0 | 0.00% | — |
| R1 | 2 | 1 | 1 | 197.18 | 197.2 | 0.172 | 0.00087 | 6 | 0 / 0 | 0.00% | — |
| R1 | 3 | 1 | 1 | 201.00 | 201.0 | 0.174 | 0.00087 | 4 | 0 / 0 | 0.00% | — |
| R2 | 1 | 1 | 0 | *aborted* (759.91 burst, 1.0 s) | — | 0.009 | — | 0 | 0 / 0 | — | — |
| R2 | 2 | 1 | 0 | *aborted* (765.38 burst, 1.3 s) | — | 0.008 | — | 0 | 0 / 0 | — | 1.82% |
| R2 | 3 | 1 | 0 | *aborted* (736.79 burst, 1.1 s) | — | 0.009 | — | 0 | 0 / 0 | — | 4.30% |
| R3 | 1 | 1 | 0 | *aborted* (587.40 burst, 0.4 s) | — | 0.002 | — | 0 | 0 / 0 | — | 0.00% |
| R3 | 2 | 1 | 0 | *aborted* (606.38 burst, 0.4 s) | — | 0.002 | — | 0 | 0 / 0 | — | 0.00% |
| R3 | 3 | 1 | 0 | *aborted* (629.53 burst, 0.4 s) | — | 0.001 | — | 0 | 0 / 0 | — | 0.00% |
| R4 | 1 | 5 | **3** | 602.3 | 602.3 | 0.253 | 0.00042 | 20 | 0 / 0 | 0.00% | 57.10% |
| R4 | 2 | 5 | **3** | 601.0 | 601.0 | 0.241 | 0.00040 | 20 | 0 / 0 | 0.00% | 43.70% |
| R4 | 3 | 5 | **3** | 602.9 | 602.9 | 0.243 | 0.00040 | 20 | 0 / 0 | 0.00% | 54.90% |
| R5 | 1 | 20 | **1** | 402.4 | 402.4 | 0.123 | 0.00031 | 32 | 0 / 0 | 0.00% | 74.10% (19 subs) |
| R5 | 2 | 20 | **1** | 401.1 | 401.1 | 0.125 | 0.00031 | 25 | 0 / 0 | 0.00% | 74.40% (19 subs) |
| R5 | 3 | 20 | **1** | 401.9 | 401.9 | 0.122 | 0.00030 | 28 | 0 / 0 | 0.00% | 74.30% (18 subs) |
| R6 | 1 | 5 | **3** | 602.5 | 602.5 | 0.241 | 0.00040 | 20 | 0 / 0 | 0.00% | 43.20% |
| R6 | 2 | 5 | **3** | 600.6 | 600.6 | 0.253 | 0.00042 | 20 | 0 / 0 | 0.00% | 47.50% |
| R6 | 3 | 5 | **3** | 603.7 | 603.7 | 0.246 | 0.00041 | 19 | 0 / 0 | 0.00% | 52.00% |
| R5b | 1 | 20 | **14** | 663.2 | 836.4 | 0.115 | 0.00014 | 80 | 0 / 0 | 0.00% | 24–58% (6 subs) |
| R5b | 2 | 20 | **15** | 714.5 | 815.8 | 0.116 | 0.00014 | 80 | 0 / 0 | 0.00% | 28–51% (5 subs) |
| R5b | 3 | 20 | **14** | 666.2 | 814.2 | 0.110 | 0.00014 | 80 | 0 / 0 | 0.00% | 29–53% (6 subs) |
| R6b | — | 5 | — | *not run* | — | — | — | — | — | — | — |

`aborted` = the rep ended in a relay-side stream reset rather than a 60 s window
(R2/R3, 3/3 each); the burst figure is the startup rate and is **not** a ceiling.
R2/R3 relay CPU is averaged over a ~1 s life, so it is a floor, not a steady-state
cost.

For R5b the two aggregates differ on purpose: `agg_all` (814–836 Mb/s) counts every
subscriber that produced ≥ 3 intervals including the 5–6 that were evicted
mid-window, while the sustained column (663–715 Mb/s) counts only the 14–15 that
ran the full window at ~47.4 Mb/s each. **`agg_all` is the delivered throughput;
the sustained column is the survivors' share.**

### R6b

R6b (5 × 160 Mb/s, WT) was created to test WT fan-out at a feasible per-subscriber
rate but **was not run**. The b3 block ran R5b then R6b; after R5b rep-03 the
harness aborted the whole block with

```
aborted: short run: subscriber measured 35.8s of 60s (see sub.log / relay.log)
```

That message is the harness's short-run guard reading **one** subscriber log —
`sub.log`, the byte-identical duplicate of `sub1.log` (see *Harness hygiene*) — and
in rep-03 `sub1` was one of the six evicted subscribers, so it stopped at 35 s of
the 60 s window. **The uni-stream eviction bug therefore aborts the run block, not
just the subscription**: 14 of rep-03's 20 subscribers sustained the full window and
its aggregate (814.2 Mb/s) is complete and valid, but the guard looked at the one
file that happened to hold the earliest casualty and killed the block. Nothing above
depends on R6b, since R6 (WT, 200 Mb/s) and R5b (raw, 50 Mb/s) fail identically; it
is recorded as a gap for Phase 1e rather than borrowed from another block.

### Fan-out

| point | offered per sub | subs | offered aggregate | delivered (all subs) | ratio delivered/offered | subscribers retained | link ceiling used |
|---|---|---|---|---|---|---|---|
| R4 | 200 Mb/s | 5 | 1000 Mb/s | 602 Mb/s | 0.60 | **3.00 of 5** | 64% |
| R5 | 400 Mb/s | 20 | **8000 Mb/s** | 402 Mb/s | 0.05 | **1.00 of 20** | 43% |
| R6 | 200 Mb/s (WT) | 5 | 1000 Mb/s | 602 Mb/s | 0.60 | **3.00 of 5** | 64% |
| R5b | 50 Mb/s | 20 | 1000 Mb/s | 815–836 Mb/s | 0.82 | **14.3 of 20** | 87% |

Retention is 3/5, 3/5, 1/20 and 14.3/20 across 3/3 reps each. That is the invariant
discussed below: at a fixed ~1 Gb/s offered aggregate, whether the fan-out is 5
subscribers at 200 Mb/s or 20 at 50 Mb/s changes the ratio only from 0.60 to 0.82,
i.e. from 40% loss to 28% loss — while R5, over-subscribed 8.5×, loses 95%.

## Reading

**Where the wall is: not relay CPU, not the link, not byte flow control. It is the
relay's handling of a per-connection stream-credit limit.**

Relay CPU is flat and small across the matrix: **0.17 core** delivering 197.6 Mb/s
to one subscriber (R1), **0.24–0.25 core** delivering ~602 Mb/s to three of five
(R4/R6, 6/6 reps, 5% spread), **0.11 core** delivering ~820 Mb/s to fourteen of
twenty (R5b). Per delivered bit that is 0.88 mcore/Mb/s at one subscriber, 0.41 at
five, **0.14 at twenty** — fan-out gets *cheaper* per bit, because one ingress pass
is amortised over N egresses. The worst observed slope puts a saturated 1 Gbps
egress under 0.5 core of the two CPUs the relay is pinned to. And it is not
byte-level flow control: `fc_block_stream` and `fc_block_conn` are **0 in every
sampler row of every point**, including the reps that dropped 19 of 20 subscribers.

It is also not the link — which is the correction this phase forces. The earlier
partial reading — "R4 delivers 848.8 Mb/s aggregate, fan-out ratio 4.24 of 5, 90%
of the 941 Mb/s payload ceiling, so the wall is the link" — was **peak-inclusive**:
it summed each subscriber's best interval, including two subscribers that survived
one interval. Sustained, R4 delivers **602.3 Mb/s = 3 of 5 subscribers = 64% of
the link**, with two subscribers dropped and a third of the link idle. R5 is worse
than the old text implies: as authored (`r: 12207` = 400 Mb/s **per** subscriber)
it offers **8 Gb/s on a 1 Gbps link**, so its 402 Mb/s is one survivor holding the
full publisher rate, not a fan-out measurement. That is why R5b exists: same
subscriber count, feasible per-subscriber rate.

**Membership loss is the invariant; eviction time is what rate changes.** Every
fan-out point loses subscribers at every rate:

| point | per-sub rate | evicted per rep | when | subgroups/s/track (derived) | credit life (derived) |
|---|---|---|---|---|---|
| R5 | 400 Mb/s | 19, 19, 18 of 20 | within ~10 s | ~61 | ~4 s |
| R4 / R6 | 200 Mb/s | 2 of 5 | early window | ~30 | ~8 s |
| R5b | 50 Mb/s | 6, 5, 6 of 20 | **15–35 s** | ~7.6 | **~34 s** |

So a lower per-subscriber rate does **not** avoid the failure, it only postpones it
past the end of the 60 s window for most subscribers. This kills the earlier
hypothesis that eviction was purely a consequence of over-subscription: at
47 Mb/s per subscriber — a twentieth of what R5 asked for — the relay still removed
5–6 of 20 subscribers in each rep, with evicted subscribers showing 24–58% loss
while the 14–15 survivors showed 0.00%.

The eviction *timing* is the strongest quantitative signal in the phase: across the
three R5b reps, every one of the 17 evictions stopped at the same two boundaries —
**14 of 17 logged exactly 7 of the twelve 5 s intervals (died at ~35 s) and 3 of 17
logged 3 (died at ~15 s)**; none died at 4–6 or 8–11 intervals. Clustered death
times, not a spread, are what a hard per-connection budget produces. Survival is
also **not** index-ordered at 20 subscribers (evicted indices 4, 8, 9, 13, 15, 17 /
5, 6, 13, 15, 18 / 1, 11, 12, 14, 15, 16; only 13 and 15 die more than once, 15 in
all three reps), unlike R4/R6 where sub4 and sub5 survived all 6 reps — consistent
with a per-connection budget whose fill time depends on when that specific
connection was established.

**The mechanism is a per-connection uni-stream budget, escalated to a fatal error.**
Each removal is logged relay-side as
`Removing subscriber after error in beginSubgroup: Failed to create uni stream`
(`deps/moxygen/moxygen/relay/MoQForwarder.cpp`, `removeSubscriberOnError`). The
subscriber side of the same event is `stream 4095 reset by peer: error=3` — and
server-initiated uni streams have ids 3, 7, 11, … so **4095 is the 1024th such
stream on that connection**: the peer's `MAX_STREAMS_UNI` credit, exhausted. The
budget is never replenished because the WebTransport layer **logs `MaxStreamsUni`
as "Unhandled"** (`openmoq/transport/pico/PicoWebTransportBase.cpp`): the peer's
credit increase is received and ignored, so the connection keeps its initial 1024
uni streams for its whole life no matter how many complete. Two independent checks
rule out a reservation bug: the only two paths that can fail *inside* stream
creation both log at `XLOG(ERR)` — `Failed to reserve stream ID`
(`PicoQuicWebTransport.cpp:61`) and `failed to create egress handle`
(`PicoWebTransportBase.cpp:103`) — and **neither string appears in any relay log**,
at a log level that did surface the `ERR`-level `Failed to create uni stream`. What
is left is `MoQSession::createUniStream()` failing because the session was already
closed. The observable sequence: subscriber stops draining → outstanding uni
streams accumulate against a fixed budget → the relay cannot open the next subgroup
stream → **a transient credit block is converted into permanent subscriber
removal** instead of the subscriber being paused or the track back-pressured. A
subscriber that was merely slow is gone for the rest of the rep. **This is the
release-relevant finding of Phase 1d.**

The derived "credit life" column is arithmetic from the measured 1024 budget and
the manifest's object rate (streams/s ≈ rate ÷ (8 × 4096 B × g), × P tracks with
g = 200). It is *consistent with* the measured eviction times — 4 s predicted vs
<10 s observed at 400 Mb/s, ~34 s predicted vs 15–35 s observed at 50 Mb/s — but it
is a model, not an instrument reading, and the 1024 parameter itself is inferred
from the stream id 4095, not read from a config.

**R2/R3 are the same escalation with a different trigger.** The unpaced points do
not settle at a relay ceiling: the relay **resets the subscriber stream**
(`stream 2047 reset by peer: error=3`, the **512th** server uni stream, reached in
0.4–1.3 s because an unpaced publisher fills the budget in under two seconds),
reproducibly 3/3 in both points. The subscriber had taken 19,412 objects (79.8 MB)
when the reset arrived; the 736–765 Mb/s (R2) and 587–630 Mb/s (R3) figures are
startup bursts, not throughput. Over-rate and slow-drain are **one bug with two
triggers**: both end in a torn-down subscription rather than backpressure.

**Tail latency: still unmeasured, and it is not what fan-out damages first.**
p50/p95/p99 are **null on every rep** — the sweep ran with
`--waive clock_offset_jump,clock_unavailable` and the cross-host offset still moved
up to 373,493 µs inside a single point against a 4,496 µs tolerance, so no
one-way latency number here is trustworthy and none is quoted. What fan-out
measurably does is produce a **bimodal delivery distribution**: sustained
subscribers sit at their full offered rate with **0.00% loss in 15 of 15 fan-out
reps**, while evicted ones show **43–74% loss** and then stop logging. Averaging a
tail over evicted subscribers would measure the eviction, not the queue. Any
fan-out tail claim needs the clock fixed first, and must be conditioned on
survival.

**Relay byte counters are not absolute egress meters in this build.** Where both
meters worked they read 25–30% below what subscribers received: R1 146.5 Mb/s
(relay `moqx_quicBytesWritten_total`) and 151.4 Mb/s (host NIC tx) against
197.6 Mb/s delivered; R4 253.7 and 262.4 against 602 Mb/s sustained. They are
accumulated from picoquic path-quality deltas
(`src/stats/PicoQuicStatsCollector.cpp:72-85`) and evidently miss forwarding paths.
Recorded, not trusted: subscriber-side totals are the primary egress measurement,
relay counters are meaningful only as within-rep ratios, and relay CPU from
`/proc` is sound — the CPU claims above rest on it.

**Standing constraint on the 20-subscriber points.** Twenty subscriber processes on
a 6-core load host is CPU-constrained, and R5b additionally offers ~1 Gb/s against
a 941 Mb/s MTU-1500 ceiling (87% used), so it is link-limited too. R5b's retention
of 14.3/20 therefore measures the relay's eviction behaviour; it is **not** a
fan-out ceiling and **not** headroom above 20 subscribers.

### Harness hygiene

Two defects found while reducing these numbers, recorded so Phase 1e does not
re-discover them:

1. **`sub.log` is a byte-identical duplicate of `sub1.log`** (`diff -q` verified).
   Any glob on `sub*.log` double-counts subscriber 1, inflating subscriber count
   and aggregate throughput by exactly one subscriber (it is how R1 briefly read
   395 Mb/s and R4 read 6 subscribers). Count only stems containing a digit.
2. **`scripts/relay_report.py` reports the peak-inclusive aggregate.** Its per-rep
   "sub total" sums best-interval rates for subscribers that died, so it reported
   R5 as a single 402 Mb/s figure and R4 as 848.84. Not wrong arithmetic, wrong
   statistic for a sustained baseline; every number here was recomputed from the
   subscriber logs with the ≥ 8-interval rule.
3. **The short-run guard reads that same single file.** `sweep.py` aborts an entire
   run block when "subscriber measured 35.8s of 60s", and the subscriber it means is
   `sub.log` (= `sub1.log`). In R5b rep-03 sub1 was an evicted subscriber, so the
   guard killed the block and R6b never started, even though 14 of 20 subscribers
   sustained the full window. The guard must evaluate the rep (or all subscribers),
   not one arbitrary log — otherwise any per-subscriber failure becomes a
   block-level abort and silently truncates the manifest.

## Open items

1. **Uni-stream credit exhaustion → permanent eviction is the top stack bug (P0).**
   Deterministic in every fan-out point (12/12 reps). Cheapest reproduction is R5b
   (20 × 50 Mb/s, evictions at 15–35 s); R5 as authored compresses it to <10 s.
   Fix candidates in order: process `MaxStreamsUni` instead of logging it
   "Unhandled"; stop converting a blocked send into `removeSubscriberOnError`
   (pause the subscriber, or drop to a lower rate, instead of tearing it down).
2. R6b not run (b3 aborted on R5b rep-03's `short_run`). The only
   WT-at-feasible-rate point; worth one rep in Phase 1e.
3. R2/R3 need a *paced* sweep (or a relay that back-pressures instead of
   resetting) before a relay ceiling can be stated at all.
4. Relay byte counters undercount; meter at the subscriber (done here) or fix the
   path-quality delta accounting.
5. Cross-host clock remains unsolved, so no fan-out tail-latency claim exists.
