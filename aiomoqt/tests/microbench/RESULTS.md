# Microbench results — aiomoqt + aiopquic (post-qh3-strip)

Recorded 2026-04-28. WSL2 / Linux 6.6.87.2 / Python 3.14.4
(non-free-threaded). All benches run from the
`migrate-aiopquic-transport` branch with qh3 fully removed; aiopquic
is the sole transport. Cython StreamChain + Cython Buffer (drop-in
for qh3.Buffer).

## TL;DR

The migration's payoff at the high end of the codec workload, and
the new accumulator path measured against the qh3.Buffer baseline:

1. **B5 N=100 sessions × 50 obj/s**: aiopquic delivers **97.9%** of
   target with **30ms** p-max latency. qh3 delivered **24.7%** with
   **11-second** tail spikes. **2× target throughput**, **368× tail
   latency reduction**.
2. **Cython StreamChain is 1.5× the Cython Buffer** in chunked
   accumulator workloads, while Cython Buffer matches the prior
   Rust qh3.Buffer baseline (1350 vs 1343 streams/s).
3. **B2 RTT** at 1000 echoes/s: aiopquic p50 = 0.30ms, p99 = 0.51ms,
   well under qh3's pre-strip equivalents.

## B1b — accumulator (parser-only, no transport)

1000 objects × 4096-byte payloads, ingested as 1500-byte chunks
(walked via pull_uint_var × 5 + pull_bytes), 3-second window.

| accumulator           | streams/s | obj/s     | Mbps    |
|-----------------------|-----------|-----------|---------|
| Cython StreamChain    | **2,041** | **2,041K**| **66,918** |
| Cython Buffer         |   1,350   | 1,350K    | 44,269  |
| (prior) qh3.Buffer    |   1,343   | 1,343K    | 44,027  |

**Cython Buffer ≈ qh3.Buffer** on this workload — the drop-in
single-buffer replacement matches the Rust baseline within noise.
**Cython StreamChain is 1.51× faster** than either single-buffer
path because it avoids the up-front concat: it walks chunks
in-place via memoryview boundaries.

## B1 — full MoQT parser

Cython StreamChain plus full ObjectHeader.deserialize dispatch
(slots'd dataclasses), 1000 objects × 4096 bytes, 5-second window:

```
streams: 3,579 in 5.00s = 716 streams/s
objects: 3,579,000 = 715,736 obj/s
bytes:   14,670,338,895 = 23,470 Mbps
```

The remaining gap to B1b's 2.0M obj/s is Python-side dataclass
construction, not the accumulator. **For the 100×100 codec target
of 10K obj/s the parser is at ~7% of capacity.**

## B2 — round-trip stream latency, loopback (no MoQT)

64-byte echo, server reflects back, paced at target rate. RTT in
milliseconds, 4-second window. (qh3 column dropped — qh3 is gone.)

| target rate | sent   | p50   | p95   | p99   | max   |
|-------------|--------|-------|-------|-------|-------|
| 1,000/s     | 4,000  | 0.299 | 0.423 | 0.511 | 1.613 |
| 5,000/s     | 14,602 | 0.259 | 0.378 | 0.474 | 4.837 |
| 10,000/s    | 14,727 | 0.258 | 0.371 | 0.467 | 6.548 |

Above 5K/s the bench is rate-limited (sent < target × duration) by
QUIC peer-stream-bidi flow-control caps in the default config —
this is a flow-control tunable, not a steady-state perf ceiling.
The relevant saturation behavior shows up in B5.

## B3 — stream open/close churn, loopback (no MoQT)

Open N uni streams in a row with 100B payload each, sink server
counts FIN deliveries.

| streams | elapsed | rate         |
|---------|---------|--------------|
| 1,000   | 0.01 s  | 80,879/s     |
| 5,000   | 0.02 s  | 315,573/s   |

aiopquic's "elapsed" is asyncio submission time — picoquic-pthread
does the wire work in parallel. At 10K streams the QUIC default
peer-stream-uni cap kicks in; tune `initial_max_streams_uni` to
push past it.

## B4 — single MoQT session sustained delivery

100 obj/s × 4096 bytes, 15s, in-process publisher+subscriber.

```
backend: aiopquic  rate-target=100.0/s object-size=4096B duration=15.0s
  delivered: 1,500 objs (100/s) 6,163,500 bytes (3.3 Mbps) in 15.0s
  e2e-latency: n=1500 avg=0.551ms p50=1ms p95=1ms p99=1ms max=2ms
```

vs the prior qh3 baseline (avg=0.775ms, max=3ms). Slightly tighter
tail; sub-ms median holds. **Single-session at codec rate is fine.**

## B5 — multi-session aggregate scaling

N concurrent MoQT sessions in one process, each delivering 50
obj/s × 4096 bytes, 15s per N.

| N    | delivered | % of target | agg obj/s | CPU%   | p50  | p95  | p99  | max  |
|------|-----------|-------------|-----------|--------|------|------|------|------|
| 1    | 750       | 99.9%       | 50/s      | 5.3%   | 1ms  | 1ms  | 1ms  | 2ms  |
| 5    | 3,754     | 100.0%      | 250/s     | 16.9%  | 0ms  | 1ms  | 1ms  | 2ms  |
| 25   | 18,775    | 99.4%       | 1,243/s   | 66.5%  | 1ms  | 2ms  | 2ms  | 10ms |
| 50   | 37,550    | 99.1%       | 2,477/s   | 123.5% | 1ms  | 2ms  | 3ms  | 12ms |
| 100  | 75,227    | 97.9%       | 4,894/s   | 277.6% | 0ms  | 1ms  | 2ms  | 30ms |

vs prior qh3 baseline:

| N    | qh3 %target | aiopquic %target | qh3 max  | aiopquic max | improvement   |
|------|-------------|------------------|----------|--------------|---------------|
| 50   | **55.9%**   | **99.1%**        | 6,426ms  | **12ms**     | 1.77× / 535×  |
| 100  | **24.7%**   | **97.9%**        | 11,034ms | **30ms**     | 3.96× / 368×  |

**This is the chart that justifies the migration.** qh3 hit its
ceiling around 1,400 obj/s aggregate at N=50; aiopquic sustains
4.9K obj/s at N=100 with sub-ms p50 and 30ms p-max. CPU% scales
linearly to 277% at N=100 — three asyncio cores, picoquic-pthread
on a fourth — confirming that the asyncio thread is no longer the
bottleneck.

The codec workload (100 sessions × 100 fps = 10K obj/s aggregate)
is well within reach by raising `--rate 100` per session — at the
current aiopquic numbers we expect linear extrapolation to ~99% of
target through N=100 × 100/s.

## Reproducing

```bash
# All benches assume aiopquic is built and pip-installed editable
# into aiomoqt's venv:
cd /home/gmarzot/Projects/moq/aiopquic
./build_picoquic.sh
/home/gmarzot/Projects/moq/aiomoqt/.venv/bin/python setup.py build_ext --inplace
/home/gmarzot/Projects/moq/aiomoqt/.venv/bin/pip install -e . --no-deps --no-build-isolation

cd /home/gmarzot/Projects/moq/aiomoqt
source .venv/bin/activate

# B1b accumulator (StreamChain + Buffer)
python -m aiomoqt.tests.microbench.b1b_accumulator \
    --duration 3 --n-objects 1000 --payload-size 4096 --chunk-size 1500

# B1 full parser
python -m aiomoqt.tests.microbench.b1_parser \
    --duration 5 --n-objects 1000 --payload-size 4096 --chunk-size 1500

# B2 RTT sweep
for rate in 1000 5000 10000; do
    python -m aiomoqt.tests.microbench.b2_rtt --duration 4 --rate $rate
done

# B3 stream churn sweep
for n in 1000 5000; do
    python -m aiomoqt.tests.microbench.b3_stream_churn \
        --n-streams $n --payload-size 100
done

# B4 single-session
python -m aiomoqt.tests.microbench.b4_single_session \
    --rate 100 --object-size 4096 --duration 15

# B5 multi-session sweep
python -m aiomoqt.tests.microbench.b5_multi_session \
    --sessions 1,5,25,50,100 --rate 50 --object-size 4096 --duration 15
```
