# Baseline: aiopquic 0.3.1 + aiomoqt 0.9.3 (released wheels)

- **Date:** 2026-05-16
- **Host:** StingRay Ryzen WSL2 (single-process, no uvloop)
- **Relay:** local moqx-local.marzresearch.net:4433 (raw QUIC)
- **Draft:** 16
- **Config:** `-P 2 -s 1024 -g 120 -t 30 --pub-both`

## Throughput sweep (Run A — sub profiled, pub plain)

| rate/stream | total obj/s | throughput | p50 / p95 / p99 | loss | OOO |
|------------:|------------:|-----------:|----------------:|-----:|----:|
| 1,000 | 2,000 | **16.59 Mbps** | 0.4 / 0.7 / 1.2 ms | 0% | 0 |
| 5,000 | 10,000 | **82.96 Mbps** | 0.6 / 1.2 / 2.0 ms | 0% | 0 |
| 10,000 | 20,000 | **165.91 Mbps** | 0.7 / 1.3 / 2.3 ms | 0% | 0 |

## RX top costs (sub_bench cProfile, A-r10000-sub.prof, 22 s wallclock)

| function | tottime | ncalls |
|---|---:|---:|
| `select.epoll.poll` (IDLE) | 10.43 s | 81 K |
| `sub_bench.on_object` (bench-only) | 1.56 s | 440 K |
| `protocol._process_data_stream` | 1.54 s | 84 K |
| `connection._drain_and_convert` | 0.61 s | 187 K |
| `track.deserialize_into` | 0.58 s | 500 K |
| `protocol._moqt_handle_data_stream` | 0.57 s | 508 K |
| `messages/base._extensions_decode` | 0.38 s | 500 K |
| `protocol.quic_event_received` | 0.28 s | 147 K |

Active CPU ≈ 22 − 10.4 ≈ 11.6 s of 22 s wallclock (~53% one core at 166 Mbps).

## TX top costs (pub_bench cProfile, B-r10000-pub.prof, 30 s wallclock)

| function | tottime | ncalls |
|---|---:|---:|
| `select.epoll.poll` (IDLE) | 10.18 s | 318 K |
| **`connection.send_stream_data` (aiopquic)** | **6.38 s** | 579 K |
| **`track._generate_subgroup` (pub loop)** | **3.11 s** | 561 K |
| `messages/base._extensions_encode` | 1.32 s | 565 K |
| `messages/track.serialize` (Subgroup/ObjectHeader) | 1.22 s | 565 K |
| `track.next_object` | 0.69 s | 565 K |
| `protocol.stream_write_drain` | 0.40 s | 570 K |

**Active CPU ≈ 30 − 10.2 ≈ 19.8 s of 30 s wallclock (~66% one core at 166 Mbps).**

Extrapolating linearly, single-core publisher ceiling on this host is
~250 Mbps before the asyncio thread saturates. Publisher is the heavier
CPU consumer at this operating point — not the receiver.

## Key takeaways for v0.3.2 / v0.9.4 plan

1. **`send_stream_data` is the single biggest TX cost (6.4 s).** Each
   per-object send is an aiopquic boundary crossing into
   `tx_send_atomic`. Anything that batches header+body or reduces calls
   per object goes here. **This is the strongest empirical case for
   `send_data_vec` (one call, multiple buffer-protocol chunks) — the
   header + body bytes go through aiopquic as one send.**

2. **`_generate_subgroup` self-time 3.1 s is the per-object Python
   orchestration.** A Cython publish-object helper that subsumes
   extension-dict build + serialize + next_object would attack this
   directly (mirror of `parse_object` on the RX side).

3. **`_extensions_encode` 1.3 s + `serialize` 1.2 s.** Both fall out
   automatically once the Cython publish-object helper exists. Today
   they're separate Python frames doing varint encoding.

4. **`_drain_and_convert` 0.61 s on RX, 187 K calls.** Still in the top
   5; the list-of-tuples + dataclass alloc is real. `drain_rx_callback`
   directly into `self._events` is the right shape.

5. **`deserialize_into` 0.58 s, 500 K calls.** Already optimized
   in-place (post-0.3.1), but still in top 10. A Cython
   `StreamChain.parse_object` that subsumes the pull_* calls into one
   transition is the next step.

## Ceiling probe (not run as baseline)

The 1K/5K/10K sweep is well under the single-core wall. Future runs
should add a 20K (-r 20000, ~330 Mbps target) operating point once we
ship the helpers, to confirm headroom growth.
