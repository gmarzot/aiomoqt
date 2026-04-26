"""B1b — accumulator microbench (pure-Python, no transport, no MoQT
parser dispatch).

Measures raw extend / pull_uint_var / pull_bytes / commit cycles
per second. Isolates the accumulator cost from the rest of the
parser. Run on each branch (`main` for re_buf+qh3.Buffer, this
branch for StreamChain) and diff.

Two tests:
  - varint heavy: many small pulls, exercises pull_uint_var path.
  - bulk pull: pull_bytes(N) with N large, exercises memcpy/slice.

Args:
  --n-objects N      objects-per-stream worth of bytes (default 1000)
  --payload-size N   bytes per object (default 4096)
  --chunk-size N     ingest chunk size, 0 = single shot (default 1500)
  --duration S       seconds per test (default 3)
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


def _bench_qh3_buffer(data: bytes, chunk_size: int, duration: float):
    """Benchmark qh3 Buffer + re_buf-style accumulation."""
    from qh3._hazmat import Buffer
    chunks = chunked(data, chunk_size) if chunk_size else [data]
    n_bytes = len(data)
    end = time.perf_counter() + duration
    iterations = 0
    bytes_consumed = 0
    while time.perf_counter() < end:
        # re_buf approximation: concat all chunks into one Buffer
        # before walking. (The real re_buf uses seek(0)+push_bytes,
        # but for steady-state accumulator throughput the cost shape
        # is similar.)
        re_buf = Buffer(capacity=n_bytes + 1024)
        for c in chunks:
            re_buf.push_bytes(c)
        re_buf.seek(0)
        try:
            re_buf.pull_uint_var()
            re_buf.pull_uint_var()
            re_buf.pull_uint_var()
            re_buf.pull_uint_var()
            re_buf.pull_uint8()
            cap = re_buf.capacity
            while re_buf.tell() < cap:
                re_buf.pull_uint_var()
                payload_len = re_buf.pull_uint_var()
                if payload_len > 0:
                    re_buf.pull_bytes(payload_len)
                else:
                    re_buf.pull_uint_var()
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
    ap.add_argument('--accumulator', choices=['streamchain', 'qh3', 'both'],
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

    if args.accumulator in ('qh3', 'both'):
        try:
            iters, bytes_done = _bench_qh3_buffer(
                data, args.chunk_size, args.duration)
            rate = iters / args.duration
            mbps = (bytes_done * 8) / (args.duration * 1e6)
            print(f"qh3.Buffer:  {iters:,} iters in {args.duration}s "
                  f"= {rate:,.0f} streams/s, {mbps:,.0f} Mbps "
                  f"({rate * args.n_objects:,.0f} obj/s)")
        except ImportError:
            print("qh3.Buffer:  (qh3 not installed, skipping)")


if __name__ == '__main__':
    main()
