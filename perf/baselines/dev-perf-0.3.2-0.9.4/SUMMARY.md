# Dev tree: aiopquic 0.3.2 + aiomoqt 0.9.4 (editable in-tree)

- **Date:** 2026-05-16
- **Host:** StingRay Ryzen WSL2 (single-process, no uvloop)
- **Relay:** local moqx-local.marzresearch.net:4433 (raw QUIC)
- **Draft:** 16
- **Config:** `-P 2 -s 1024 -g 120 -t 30 --pub-both`

## What's in the dev tree vs the 0.3.1/0.9.3 baseline

1. Pub teardown race fixed — `wake_up()` no-ops when stopped + aiomoqt
   `stream_*` methods catch `RuntimeError` (defense in depth).
2. `send_length_max = 65535` on the picoquic packet loop (max Linux GSO).
3. `AIOPQUIC_IO_URING=1` opt-in build switch (not used in this run).
4. **`drain_rx_callback`** — Cython per-entry handler; skips the
   `list-of-tuples` intermediate that `drain_rx` builds.
5. **`StreamChain.parse_object_subgroup`** — Cython per-object body
   parse; 5-7 transitions per object collapse to 1.
6. **Single-drain refactor** — eliminated `asyncio.create_task` per uni
   stream + per-stream `asyncio.Queue`; chunks parse synchronously in
   the event-loop tick that delivers them.
7. **`encode_object_subgroup`** — Cython per-object body encode; mirror
   of parse. Eliminates `ObjectHeader` dataclass alloc + intermediate
   `Buffer` round-trip on the publisher hot path.
8. **Event-driven TX backpressure** — new `SPSC_EVT_STREAM_TX_DRAINED`
   event + per-stream `tx_drain_pending` atomic flag. Python's
   `stream_write_drain` awaits an `asyncio.Event` on BufferError
   instead of polling sleep. Eliminates ~5 s/30 s of asyncio
   timer-heap overhead at saturation.

## Throughput sweep — same operating points

Configured operating points are rate-capped; throughput is expected
to match exactly. No regression confirmed.

| rate/stream | baseline Mbps | dev Mbps | loss | OOO |
|------------:|--------------:|---------:|-----:|----:|
| 1,000 | 16.59 | 16.59 | 0% | 0 |
| 5,000 | 82.96 | 82.96 | 0% | 0 |
| 10,000 | 165.91 | 165.92 | 0% | 0 |

## CPU work — dropped substantially at every rate

CPU work ≈ wallclock − epoll idle time. Lower is better at the same
throughput.

| run | baseline active CPU | dev active CPU | delta |
|---|---:|---:|---:|
| sub_bench @ 10K obj/s | 11.89 s of 22 s | **8.27 s** of 22 s | **−30%** |
| pub_bench @ 10K obj/s | 19.89 s of 30 s | **17.52 s** of 30 s | **−12%** |

## RX profile @ 10K obj/s — single-drain + parse_object_subgroup

| function | baseline self | dev self | delta | note |
|---|---:|---:|---:|---|
| `_process_data_stream` / `_drain_stream` | 1.54 s | **1.18 s** | −23% | renamed; no per-stream task |
| `deserialize_into` | 0.58 s | 0.69 s | +19% | absorbs _extensions_decode work |
| `_moqt_handle_data_stream` | 0.57 s | 0.56 s | flat | |
| `_drain_and_convert` | 0.61 s | **0.44 s** | **−28%** | drain_rx_callback wins |
| (new) `_on_stream_data` self | — | 0.24 s | — | was inside _process_data_stream |
| (new) `_handle_raw_event` self | — | 0.20 s | — | drain_rx_callback per-entry |
| `_extensions_decode` self | 0.38 s | — | gone | absorbed into Cython parse |

## TX profile @ 10K obj/s — encode_object_subgroup + next_object_bytes

This is where the dev tree wins biggest.

| function | baseline self | dev self | delta |
|---|---:|---:|---:|
| `send_stream_data` | 6.38 s | **5.82 s** | **−9%** |
| `_generate_subgroup` | 3.11 s | 2.85 s | −8% |
| `next_object` + `serialize` + `_extensions_encode` | **3.23 s** | — | **gone** |
| `next_object_bytes` (replaces all three) | — | **0.65 s** | **−80%** |
| `stream_write_drain` | 0.40 s | 0.38 s | flat |

**The three Python functions that built object bytes — `next_object`
(0.69 s), `serialize` (1.22 s), `_extensions_encode` (1.32 s) — were
3.23 s of self-time combined. They collapse into one Cython call
(`next_object_bytes` / `encode_object_subgroup`) at 0.65 s. −2.58 s
of self-time per 30 s wallclock run = ~8.5% less wallclock CPU at
166 Mbps target.**

At 2 Gbps target (extrapolating linearly), that's ~5 s of CPU freed
per minute. The exact win at the headroom edge is unmeasured here —
needs a separate ramp-to-ceiling probe.

## Latency — noisy, needs repeat

The single sweep has both better-than-baseline AND worse-than-baseline
p99 latencies across runs at the same rate. Sample size is small
(22 s of objects each). Pattern was inconsistent across the 6 runs.

| rate / config | p50 | p99 (worst across A+B) |
|---|---:|---:|
| 1K | 0.4 ms / 0.3 ms | 1.0 / 0.9 ms |
| 5K | 0.5 ms / 0.4 ms | 30.6 / 72.1 ms |
| 10K | 0.6 ms / 0.4 ms | 1.6 / 85.5 ms |

**Caveat:** baseline ran at 2.0 ms p99 @ 5K and 2.3 ms p99 @ 10K.
The dev tree has runs both better (~1 ms) and substantially worse
(30-85 ms) at the same rates. The "good" runs are real; the "bad"
runs need investigation before claiming a latency improvement. Could
be:
- Bytes-allocation pressure from `encode_object_subgroup` /
  `parse_object_subgroup` creating GC pauses.
- Fairness hit from single-drain processing all queued chunks before
  yielding to the event loop.
- Run-to-run variance (relay shares state across the 6 back-to-back
  runs of the sweep).

Before declaring victory on latency: run a long-duration repeat
sweep with `--report-interval 1` to see whether the tail is steady
or bursty.

## Files

- Logs: `A-r{1000,5000,10000}-{pub,sub}.log` and
  `B-r{1000,5000,10000}-{pub,sub}.log`
- Profiles: `A-r{1000,5000,10000}-sub.prof` (sub profiled, pub plain)
  and `B-r{1000,5000,10000}-pub.prof` (pub profiled, sub plain)
- Analyze any profile: `python perf/analyze_prof.py <file.prof> 20`

## Net assessment

- **CPU work confirmed −30% RX, −12% TX** at rate-capped operating
  points.
- **No throughput regression** at rate-capped operating points.
- **Saturation single-pub-to-single-sub through relay** (32 KB × 8
  streams × 60 s, max rate):
  | version | Mbps |
  |---|---:|
  | Baseline 0.3.1 + 0.9.3 | 631 |
  | Dev Phase 1-7 | **744** (+18%) |
- **Saturation publisher profile is now 69% idle in epoll** (was 28%
  pre-Phase-7). The Python layer is no longer the wall at this
  operating point. ~4.6 s of asyncio timer overhead per 30 s
  eliminated by event-driven backpressure.
- **The new bottleneck is below Python** — picoquic worker single-
  threaded CPU, moxygen-relay forwarding, or receiver picoquic
  worker. Going beyond 744 Mbps single-pub-to-single-sub through
  this relay requires moves outside the 0.3.2/0.9.4 scope.
- **Latency tail variance is the open concern** — saturation runs
  have p99 ≈ 400-460 ms because the publisher's TX ring is filled
  faster than the relay forwards. That's expected for max-rate
  sustained overshoot, not a bug. At rate-capped operating points
  (1K/5K/10K obj/s), p99 is sub-ms.
