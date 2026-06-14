# aiomoqt Performance & Benchmarks

How to benchmark the stack, what governs latency and throughput, and
point-in-time observed numbers. All figures here are **observations on
specific hardware under specific conditions, not absolute capabilities**
— kernel UDP loopback rates, CPU, and scheduler behavior move them
substantially between platforms. Calibrate on your own hardware with
the commands below.

The transport-layer view (TX/RX dataflow, flow control, configuration
parameters) lives in
[aiopquic DATAFLOW.md](https://github.com/gmarzot/aiopquic/blob/main/DATAFLOW.md);
transport-layer baselines and runtime tuning (jemalloc, GSO) in
[aiopquic PERFORMANCE.md](https://github.com/gmarzot/aiopquic/blob/main/PERFORMANCE.md).

## Bench tools

| Tool | Topology | Measures |
|------|----------|----------|
| `loopback_bench` | single process, no relay | canonical stack benchmark — pub and sub share one event loop |
| `pub_server` + `sub_bench` | two processes, no relay | per-side CPU isolation; each side gets its own core(s) |
| `pub_bench` / `sub_bench` | through a relay | end-to-end relay path; relay capacity is usually the wall |
| `relay_bench` | one process, through a relay | combined pub+sub convenience wrapper |
| `multi_sub_bench` | 1 pub, N subs, one process | fanout capacity |
| `adaptive_bench` | loopback or relay | rate ramp until latency/buffer growth degrades |

```bash
# Single-process loopback (no relay; -q = raw QUIC, default WT)
python -m aiomoqt.examples.loopback_bench -P 8 -s 4096 -g 200 -t 30 -q

# Two-process (shell 1 publisher, shell 2 subscriber). The publisher
# serves WT at "/" (raw QUIC with -q has no path); the subscriber needs
# -k for the self-signed loopback cert. -t goes on the subscriber here
# (pub_server runs until Ctrl-C). Omit --trackname to exercise track-
# name discovery; -n must match the server's namespace.
python -m aiomoqt.examples.pub_server -P 8 -s 4096 -g 200 -r 35000 -n aiomoqt
python -m aiomoqt.examples.sub_bench https://localhost:4434/ -n aiomoqt --draft 16 -k -t 30 -i 5

# Through a relay
python -m aiomoqt.examples.pub_bench moqt://relay.ex.com -s 4096 -P 4 -r 8000 -t 30
python -m aiomoqt.examples.sub_bench moqt://relay.ex.com -i 5
```

Convention: set `-t` on the publisher only; the subscriber exits on the
track's natural close.

### Common options

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --object-size` | Payload size (bytes) | tool-specific |
| `-g, --group-size` | Objects per group (each group = fresh stream per subgroup) | 10000 |
| `-P, --streams` | Parallel subgroup streams | 1 |
| `-r, --rate` | Aggregate objects/sec across all streams (0 = max) | 0 |
| `-t, --duration` | Duration seconds (publisher side) | tool-specific |
| `-i, --interval` | Report interval seconds | 5 |
| `-q, --quic` | Raw QUIC (URL tools: override for https:// URLs) | WT |
| `-D, --datagram` | ObjectDatagrams instead of streams | off |
| `--cc-algo` | Congestion control (bbr1, bbr, cubic, newreno, ...) | aiopquic default |
| `--draft` | MoQT draft version | tool-specific |
| `--max-queued-bytes` | Aggregate publisher TX budget, all streams | aiopquic default |
| `--max-inflight-bytes` | Per-stream publisher TX budget | aiomoqt default |

Publisher flow selection for relays that require a specific message
pattern: `--pub-ns` (PUBLISH_NAMESPACE only, wait for SUBSCRIBE),
`--pub-both` (both — required by Cloudflare d14), default PUBLISH only.

## What governs latency: paced vs unpaced

The two runs measure different things:

- **Paced (`-r` below saturation)** measures the *stack*: how fast an
  object moves pub → wire → sub when nothing is queued. Healthy
  numbers are sub-millisecond to a few milliseconds on loopback.
- **Unpaced (`-r 0`)** measures the *TX budget*: producers run until
  backpressure parks them, so a standing queue equal to the budget sits
  in front of every object. Steady-state latency ≈ aggregate budget ÷
  drain rate — e.g. a 4 MiB budget draining at 3 Gbps ≈ 10 ms. That
  latency is the configured queue depth, not stack overhead.

Two budget parameters bound publisher run-ahead (sender-side mirrors of
QUIC's MAX_DATA / MAX_STREAM_DATA):

- `--max-queued-bytes` (aiopquic `tx_max_queued_bytes`) — aggregate
  across ALL streams; the bound that short-stream churn cannot bypass.
  Lower it for tighter unpaced latency at the cost of throughput
  headroom; 0 disables.
- `--max-inflight-bytes` (aiomoqt `tx_max_inflight_bytes`) — one
  stream's queue; fairness across streams and the binding constraint
  for long-lived single streams; 0 disables.

See aiopquic DATAFLOW.md for the full model (park/resume hysteresis,
where each budget engages, counter signatures).

## Observed numbers (point in time)

Observed on AMD Ryzen 7 PRO 7840U, WSL2, Linux 6.6, in-process
loopback (`loopback_bench`, pub and sub sharing one core pool), ~3–4 KiB
objects. **Treat as one sample of what this stack does on that
machine** — same commands on other hardware will differ, and loopback
shares CPU between both endpoints, so two-process and LAN topologies
shift the numbers again.

| scenario | throughput | latency |
|---|---|---|
| raw QUIC, 1 stream, unpaced | ~2.9 Gbps | p50 ~3 ms, p99 ~7 ms |
| raw QUIC, 8 streams, group churn (200-obj groups), unpaced | ~2.9 Gbps @ ~110 K obj/s | p50 ~5–9 ms, p99 ~12–16 ms |
| WebTransport, 8 streams, group churn, unpaced | ~3.0 Gbps @ ~90 K obj/s | p99 ~13 ms |
| paced below saturation (either transport) | (offered rate) | sub-ms to low-ms p50 |

Memory stays bounded in all of the above (RSS well under 100 MB for
the whole two-endpoint process) — sustained churn does not accumulate.

Unpaced latency in this table is dominated by the default TX budgets,
per the section above; lower budgets trade throughput stability for
latency.

## Interop matrix (point in time)

Control-message and pub-sub results against live public relays
(6 [moq-interop-runner](https://github.com/englishm/moq-interop-runner)
cases plus a 3-subscriber pub-sub bench; error codes validated to
spec-conformant values). Probed via
`tests/release_regression_test.py --test-tier interop`; refreshed at
release gates, so treat as indicative — relay deployments change.

| Relay | Draft | Transport | ctrl-msg | pub-sub |
|-------|-------|-----------|----------|---------|
| OpenMoQ moqx | d14 | QUIC | 6/6 | 3/3 |
| OpenMoQ moqx | d14 | H3/WT | 6/6 | 3/3 |
| OpenMoQ moqx | d16 | QUIC | 6/6 | 3/3 |
| OpenMoQ moqx | d16 | H3/WT | 6/6 | 3/3 |
| Meta moxygen | d14 | QUIC | 6/6 | 3/3 |
| Meta moxygen | d14 | H3/WT | 6/6 | 3/3 |
| Meta moxygen | d16 | QUIC | 6/6 | 3/3 |
| Meta moxygen | d16 | H3/WT | 6/6 | 3/3 |
| Cloudflare moq-rs | d14 | QUIC | 5/6 | 3/3 |
| Cloudflare moq-rs (d16 interop branch) | d16 | QUIC | 6/6 | unverified |
| Red5 Pro | d14 | H3/WT | 6/6 | unverified |
| Red5 Pro | d16 | H3/WT | 6/6 | unverified |
| Quicr libquicr | d14 | QUIC | 5/6 | 3/3 |
| Quicr libquicr | d14 | H3/WT | 5/6 | 3/3 |
| Quicr libquicr | d16 | H3/WT | 5/6 | 3/3 |
| Meetecho imquic | d16 | QUIC | 6/6 | unverified |
| Meetecho imquic | d16 | H3/WT | 5/6 | unverified |
| OzU moqtail | d14 | H3/WT | 6/6 | unverified |

`unverified` — suite did not complete end-to-end. Endpoints that did
not answer a QUIC Initial at probe time are omitted. See
[`tests/relays.json`](tests/relays.json) for the full catalog,
per-endpoint notes, and relays disabled by default.

Test cases: `setup-only`, `announce-only`, `publish-namespace-done`,
`subscribe-error`, `announce-subscribe`, `subscribe-before-announce`,
plus `fetch` and `join` probes (not in the default matrix — most
relays do not implement these yet).

## Appendix: example loopback runs (single box)

Raw results from one machine, for shape — not averaged, not a
benchmark claim. `loopback_bench`, single process (publisher and
subscriber share the host), no relay.

**Box:** OCI Ampere A1 (free tier), 4 vCPU aarch64, Ubuntu, CPython
3.14.6 (no WSL). aiopquic/aiomoqt built on-box.

| transport | object | rate | result |
|---|---|---|---|
| WT  | 3100 B | max     | 1.83 Gbps, p50 14.3 ms, p99 31.8 ms, 0 loss |
| raw | 3100 B | max     | 1.99 Gbps, p50 7.2 ms,  p99 9.5 ms,  0 loss |
| raw | 2048 B | max     | 1.37 Gbps, p50 61.1 ms, p99 66.6 ms, 0 loss |
| raw | 2048 B | 80k/s   | 1.24 Gbps, p50 0.4 ms,  p99 0.5 ms,  0 loss |
| WT  | 2048 B | 80k/s   | 1.11 Gbps, p50 0.3 ms,  p99 0.6 ms,  0 loss |
| WT  | 4096 B | max, -P8 -g200 | 1.98 Gbps, p50 2.1 ms, p99 4.1 ms, 0 loss |
| raw | 4096 B | max, -P8 -g200 | 2.14 Gbps, p50 3.1 ms, p99 3.8 ms, 0 loss |

Notes on reading these:
- **Unpaced (`max`) latency is queue depth, not stack latency.** Same
  2048 B config: unpaced p50 61 ms vs paced (`-r 80000`) p50 0.4 ms —
  the difference is the TX budget the unpaced producer keeps full (see
  the paced-vs-unpaced section above), not stack overhead.
- The paced p50/p99 (~0.3–0.6 ms) is the stack's latency floor on this
  box; unpaced numbers trade that for throughput by design.
- Per-core bound: single-stream throughput here (~1.8–2.0 Gbps) is
  below x86 desktop figures — single-stream loopback is CPU-bound on
  one core, and these are small shared cores. AES-GCM also differs:
  picoquic's x86 Fusion path is unavailable on aarch64.
