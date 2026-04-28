"""B1b — accumulator microbench (pure-Python, no transport, no MoQT
parser dispatch).

Measures raw extend / pull_uint_var / pull_bytes / commit cycles
per second. Isolates the accumulator cost from the rest of the
parser. Two backends:

  - StreamChain: chunked PEP-3118 model (push memoryview chunks,
    walk across boundaries). The aiomoqt RX hot path.
  - Buffer: single-contiguous-buffer model (push_bytes once, walk).
    Mirrors the qh3.Buffer-era accumulator shape — kept as baseline
    so we can spot regressions in either lane.

Args:
  --n-objects N      objects-per-stream worth of bytes (default 1000)
  --payload-size N   bytes per object (default 4096)
  --chunk-size N     ingest chunk size, 0 = single shot (default 1500)
  --duration S       seconds per test (default 3)
  --accumulator      streamchain | buffer | both  (default both)
"""
from __future__ import annotations

import argparse
import time

from aiomoqt.tests.microbench._bytestream import (
    make_subgroup_stream, chunked,
)


def _bench_streamchain(data: bytes, chunk_size: int, duration: float):
    """Benchmark StreamChain: extend + walk-and-commit."""
    from aiopquic.streamchain import StreamChain
    chunks = chunked(data, chunk_size) if chunk_size else [data]
    n_bytes = len(data)
    end = time.perf_counter() + duration
    iterations = 0
    bytes_consumed = 0
    while time.perf_counter() < end:
        chain = StreamChain()
        for c in chunks:
            chain.extend(c)
        # Walk: pull_uint_var(many), pull_bytes(payload). Approximates
        # what _moqt_handle_data_stream does.
        try:
            # subgroup header: type, track_alias, group_id, subgroup_id, prio (uint8)
            chain.pull_uint_var()
            chain.pull_uint_var()
            chain.pull_uint_var()
            chain.pull_uint_var()
            chain.pull_uint8()
            # objects: delta, payload_len, payload
            while chain.capacity - chain.tell() > 0:
                chain.pull_uint_var()       # object id delta
                payload_len = chain.pull_uint_var()
                if payload_len > 0:
                    chain.pull_bytes(payload_len)
                else:
                    chain.pull_uint_var()   # status
        except Exception:
            pass
        iterations += 1
        bytes_consumed += n_bytes
    return iterations, bytes_consumed


def _bench_buffer(data: bytes, chunk_size: int, duration: float):
    """Benchmark aiopquic Buffer in the single-contiguous accumulator
    pattern. Push all chunks once, seek(0), walk."""
    from aiopquic.buffer import Buffer
    chunks = chunked(data, chunk_size) if chunk_size else [data]
    n_bytes = len(data)
    end = time.perf_counter() + duration
    iterations = 0
    bytes_consumed = 0
    while time.perf_counter() < end:
        buf = Buffer(capacity=n_bytes + 1024)
        for c in chunks:
            buf.push_bytes(c)
        buf.seek(0)
        try:
            buf.pull_uint_var()
            buf.pull_uint_var()
            buf.pull_uint_var()
            buf.pull_uint_var()
            buf.pull_uint8()
            cap = buf.capacity
            while buf.tell() < cap:
                buf.pull_uint_var()
                payload_len = buf.pull_uint_var()
                if payload_len > 0:
                    buf.pull_bytes(payload_len)
                else:
                    buf.pull_uint_var()
        except Exception:
            pass
        iterations += 1
        bytes_consumed += n_bytes
    return iterations, bytes_consumed


def main():
    ap = argparse.ArgumentParser(description="Accumulator microbench")
    ap.add_argument('--n-objects', type=int, default=1000)
    ap.add_argument('--payload-size', type=int, default=4096)
    ap.add_argument('--chunk-size', type=int, default=1500)
    ap.add_argument('--duration', type=float, default=3.0)
    ap.add_argument('--accumulator',
                    choices=['streamchain', 'buffer', 'both'],
                    default='both')
    args = ap.parse_args()

    data = make_subgroup_stream(args.n_objects, args.payload_size)
    print(f"corpus: {len(data):,} bytes "
          f"({args.n_objects} objs × {args.payload_size}B)")

    if args.accumulator in ('streamchain', 'both'):
        iters, bytes_done = _bench_streamchain(
            data, args.chunk_size, args.duration)
        rate = iters / args.duration
        mbps = (bytes_done * 8) / (args.duration * 1e6)
        print(f"streamchain: {iters:,} iters in {args.duration}s "
              f"= {rate:,.0f} streams/s, {mbps:,.0f} Mbps "
              f"({rate * args.n_objects:,.0f} obj/s)")

    if args.accumulator in ('buffer', 'both'):
        iters, bytes_done = _bench_buffer(
            data, args.chunk_size, args.duration)
        rate = iters / args.duration
        mbps = (bytes_done * 8) / (args.duration * 1e6)
        print(f"buffer:      {iters:,} iters in {args.duration}s "
              f"= {rate:,.0f} streams/s, {mbps:,.0f} Mbps "
              f"({rate * args.n_objects:,.0f} obj/s)")


if __name__ == '__main__':
    main()
