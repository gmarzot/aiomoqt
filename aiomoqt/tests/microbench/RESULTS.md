# Microbench results — aiomoqt + aiopquic vs qh3 baseline

Recorded 2026-04-26 evening. WSL2 / Linux 6.6.87.2 / Python 3.14.4
(non-free-threaded). All benches run from the
`migrate-aiopquic-transport` branch with the Cython StreamChain in
place; raw-QUIC backend comparisons via `--backend qh3|aiopquic`
flags. MoQT-level benches (B4/B5) currently use the qh3 backend
only — aiopquic backend wires up after Phase D.

## TL;DR

The migration is justified by two findings:

1. **Cython StreamChain is faster than qh3.Buffer** (which is
   Rust-backed). The "is parsing in Python a non-starter?" concern
   is closed.
2. **qh3 saturates the asyncio thread well below the codec
   workload target.** At N=50 sessions × 50 obj/s, qh3 delivers
   only 56% of target with 6.4-second tail spikes. The codec
   workload (100 sessions × 100 fps ≈ 10K obj/s aggregate) needs
   the picoquic-pthread offload that aiopquic provides.

## B1b — accumulator (parser-only, no transport)

1000 objects × 4096 byte payloads, ingested in 1500-byte chunks,
walked end-to-end (extend + pull_uint_var × 5 + pull_bytes ×
payload_len), 5-second measurement window.

| accumulator           | streams/s | obj/s     | Mbps    |
|-----------------------|-----------|-----------|---------|
| Cython StreamChain    | **2,136** | **2,136K**| **70,031** |
| qh3.Buffer (Rust)     |   1,343   | 1,343K    | 44,027  |

**Cython StreamChain is 1.59× faster than the Rust qh3.Buffer
path** on the equivalent workload. (Earlier the pure-Python
StreamChain was 3.6× slower than qh3.Buffer; the Cython rewrite
moved us from ⅓ of qh3 to 1.6× qh3 — a 4.8× swing.)

## B1 — full MoQT parser

Cython StreamChain plus full ObjectHeader.deserialize dispatch
(slots'd dataclasses), 1000 objects × 4096 bytes, 5-second window:

```
streams: 4,235 in 5.00s = 847 streams/s
objects: 4,235,000 = 847K obj/s
bytes:   17,359,286,175 = 27,775 Mbps
```

The remaining gap to B1b's 2.1M obj/s is Python-side dataclass
construction, not the accumulator. **For the 100×100 codec target
of 10K obj/s the parser is at ~1% of capacity.**

## B2 — round-trip stream latency, loopback (no MoQT)

64-byte echo, server reflects back, paced at target rate. RTT in
milliseconds, 4-second window.

| target rate | qh3 sent | qh3 p50 | qh3 p99 | qh3 max | aiopquic sent | aiopquic p50 | aiopquic p99 | aiopquic max |
|-------------|----------|---------|---------|---------|---------------|--------------|--------------|--------------|
| 200/s       | 801      | 0.327   | 0.813   | 1.946   | 801           | 0.348        | 0.974        | 1.733        |
| 1,000/s     | 4,001    | 0.297   | 0.611   | 2.177   | 4,001         | **0.289**    | **0.567**    | **1.661**    |
| 5,000/s     | 19,994   | **0.144** | **0.448** | 6.427   | 16,420 (rate-limited) | 0.227    | **0.381**    | 6.815        |
| 10,000/s    | 28,425   | **0.117** | **0.359** | 5.630   | 16,458 (rate-limited) | 0.223    | 0.384        | **3.896**    |

**Read carefully:** at high rates qh3 wins on p50 in this single-
stream-pair RTT bench because it has no thread-crossing overhead.
But aiopquic at 5K and 10K targets is rate-limited (only 16K/s
completing instead of the requested rate) — same flow-control cap
that bites in B3 below. This benchmark ISN'T asyncio-saturated;
it's a single bidi-stream-per-echo pattern. The relevant
saturation behavior shows up in B5.

## B3 — stream open/close churn, loopback (no MoQT)

Open N uni streams in a row with 100B payload each, sink server
counts FIN deliveries.

| streams | qh3 elapsed | qh3 rate    | aiopquic elapsed | aiopquic rate |
|---------|-------------|-------------|------------------|---------------|
| 1,000   | 0.05 s      | 19,290/s    | 0.01 s           | **86,302/s**  |
| 5,000   | 0.74 s      | 6,796/s     | 0.02 s           | **306,699/s** |
| 10,000  | 2.89 s      | 3,455/s     | 30.02 s (timeout, 7,882/10,000)  | 333/s |

**aiopquic submits dramatically faster** because the asyncio side
just pushes to the TX ring — picoquic-pthread does the wire work
in parallel. **At 10K streams the QUIC peer-stream-uni cap kicks
in** (default config); both backends would benefit from
`initial_max_streams_uni` tuning.

The "elapsed" semantics differ: qh3 is "submit + deliver"
serialized in one thread; aiopquic is "submit ≪ deliver" with
delivery happening on the picoquic thread asynchronous from
asyncio. So qh3's elapsed ≈ wire time; aiopquic's elapsed ≈
asyncio submission time + the time for the server-side asyncio
loop to count completions.

## B4 — single MoQT session sustained delivery (qh3 baseline)

100 obj/s × 4096 bytes, 15s, in-process publisher+subscriber.

```
backend: qh3  rate-target=100.0/s object-size=4096B duration=15.0s
  delivered: 1,500 objs (99/s) 6,163,500 bytes (3.3 Mbps) in 15.1s
  e2e-latency: n=1500 avg=0.775ms p50=1ms p95=1ms p99=2ms max=3ms
```

**qh3 single-session at codec rate is fine** — sub-ms median, 3ms
worst-case. The single-session story isn't where qh3 fails; it's
multi-session aggregation.

## B5 — multi-session aggregate scaling (qh3 baseline)

N concurrent MoQT sessions in one process, each delivering 50
obj/s × 4096 bytes, 15s per N.

| N    | delivered | % of target | aggregate obj/s | CPU%  | p50  | p95   | p99    | max         |
|------|-----------|-------------|-----------------|-------|------|-------|--------|-------------|
| 1    | 750       | 99.1%       | 50/s            | 6.9%  | 1ms  | 2ms   | 2ms    | 3ms         |
| 5    | 3,751     | 99.1%       | 248/s           | 20.3% | 1ms  | 2ms   | 2ms    | 5ms         |
| 25   | 18,762    | 96.7%       | 1,209/s         | 79.5% | 1ms  | 3ms   | 7ms    | 35ms        |
| 50   | 35,630    | **55.9%**   | 1,396/s         | 77.8% | 4ms  | 132ms | 313ms  | **6,426ms** |
| 100  | 50,617    | **24.7%**   | 1,235/s         | 76.6% | 5ms  | 63ms  | 989ms  | **11,034ms**|

**This is the chart that justifies the migration.** qh3 hits
its ceiling around 1,400 obj/s aggregate. At N=25 it's still
making the rate target; at N=50 it falls to 56% with 6-second
tail spikes; at N=100 it's at 25% target and 11-second tails.

The codec workload (100 sessions × 100 fps = 10,000 obj/s
aggregate) is **7× past qh3's ceiling**. This is direct,
empirical justification for the aiopquic migration.

## After Phase D — what we expect to see

When aiomoqt's MoQT-level benches (B4 + B5) can run on the
aiopquic backend, the expected shape:

- **B4 single-session**: roughly equal latency, slightly tighter
  tail (the asyncio thread isn't doing crypto/congestion).
- **B5 multi-session**: linear scaling out to N=100+ on this
  hardware, p99 staying under ~50ms, throughput approaching
  100 × 50 = 5K obj/s comfortably and reaching the codec target
  of 10K obj/s with headroom.

If those numbers hold, the migration is the win we predicted. If
they don't, we have concrete diagnostic data to pinpoint the
bottleneck and address it before declaring Phase E done.

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

# B1b accumulator
python -m aiomoqt.tests.microbench.b1b_accumulator \
    --duration 5 --n-objects 1000 --payload-size 4096 --chunk-size 1500

# B1 full parser
python -m aiomoqt.tests.microbench.b1_parser \
    --duration 5 --n-objects 1000 --payload-size 4096 --chunk-size 1500

# B2 RTT sweep
for rate in 200 1000 5000 10000; do
    python -m aiomoqt.tests.microbench.b2_rtt --backend qh3 \
        --duration 4 --rate $rate
    python -m aiomoqt.tests.microbench.b2_rtt --backend aiopquic \
        --duration 4 --rate $rate
done

# B3 stream churn sweep
for n in 1000 5000 10000; do
    python -m aiomoqt.tests.microbench.b3_stream_churn --backend qh3 \
        --n-streams $n --payload-size 100
    python -m aiomoqt.tests.microbench.b3_stream_churn --backend aiopquic \
        --n-streams $n --payload-size 100
done

# B4 single-session
python -m aiomoqt.tests.microbench.b4_single_session --backend qh3 \
    --rate 100 --object-size 4096 --duration 15

# B5 multi-session sweep
python -m aiomoqt.tests.microbench.b5_multi_session --backend qh3 \
    --sessions 1,5,25,50,100 --rate 50 --object-size 4096 --duration 15
```
