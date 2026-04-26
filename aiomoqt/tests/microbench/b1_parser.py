"""B1 — MoQT parser throughput (pure-Python, no transport).

Builds a single subgroup stream worth of wire bytes (header + N
object headers + payloads) and drives them through the data-stream
parser entry point — exercising the full path:
  - StreamChain (or qh3 Buffer on `main`) accumulator
  - SubgroupHeader.deserialize
  - ObjectHeader.deserialize × N (extension decoding, status decode)
  - dispatch via _moqt_handle_data_stream

Measures objects/sec and Mbps. No connection, no asyncio, no
session machinery — just the parser CPU cost.

Args:
  --n-objects N      objects per stream (default 1000)
  --payload-size N   bytes per object (default 4096)
  --chunk-size N     ingest chunk size, 0 = single shot (default 1500)
  --duration S       seconds of bench loop (default 5)
"""
from __future__ import annotations

import argparse
import time
from typing import Optional

from aiomoqt.context import set_moqt_ctx_version
from aiomoqt.messages import ObjectStatus
from aiomoqt.messages.base import MOQTUnderflow
from aiomoqt.messages.track import (
    SubgroupHeader, ObjectHeader, FetchHeader, FetchObject,
)
from aiomoqt.types import MOQT_VERSION_DRAFT16
from aiomoqt.utils.streamchain import StreamChain
from aiomoqt.tests.microbench._bytestream import (
    make_subgroup_stream, chunked,
)


def _parse_subgroup_stream(chain: StreamChain) -> int:
    """Drive the parser over one subgroup stream's worth of bytes.

    Returns object count parsed. Mirrors what _moqt_handle_data_stream
    does (without the protocol-session bookkeeping)."""
    set_moqt_ctx_version(MOQT_VERSION_DRAFT16)

    # Stream type byte (varint) + SubgroupHeader
    stream_type = chain.pull_uint_var()
    sg = SubgroupHeader.deserialize(chain, type_val=stream_type)

    n_objects = 0
    cap = chain.capacity
    while chain.tell() < cap:
        chain.save()
        try:
            obj = ObjectHeader.deserialize(
                chain, cap,
                extensions_present=sg.extensions_present,
                prev_object_id=sg._last_object_id,
            )
        except MOQTUnderflow:
            chain.rollback()
            break
        sg._last_object_id = obj.object_id
        n_objects += 1
        chain.commit()
        cap = chain.capacity
    return n_objects


def main():
    ap = argparse.ArgumentParser(description="MoQT parser throughput")
    ap.add_argument('--n-objects', type=int, default=1000)
    ap.add_argument('--payload-size', type=int, default=4096)
    ap.add_argument('--chunk-size', type=int, default=1500)
    ap.add_argument('--duration', type=float, default=5.0)
    ap.add_argument('--warmup', type=float, default=0.5)
    args = ap.parse_args()

    data = make_subgroup_stream(args.n_objects, args.payload_size)
    chunks = chunked(data, args.chunk_size) if args.chunk_size else [data]
    print(f"corpus: {len(data):,} bytes, {len(chunks)} chunks of "
          f"~{args.chunk_size}B ({args.n_objects} objs × "
          f"{args.payload_size}B payload)")

    # Warmup
    end = time.perf_counter() + args.warmup
    while time.perf_counter() < end:
        chain = StreamChain()
        for c in chunks:
            chain.extend(c)
        _parse_subgroup_stream(chain)

    # Measure
    t0 = time.perf_counter()
    end = t0 + args.duration
    streams = 0
    objects = 0
    bytes_done = 0
    while time.perf_counter() < end:
        chain = StreamChain()
        for c in chunks:
            chain.extend(c)
        objects += _parse_subgroup_stream(chain)
        streams += 1
        bytes_done += len(data)

    elapsed = time.perf_counter() - t0
    obj_s = objects / elapsed
    str_s = streams / elapsed
    mbps = (bytes_done * 8) / (elapsed * 1e6)
    print(f"streams: {streams:,} in {elapsed:.2f}s = {str_s:,.0f} streams/s")
    print(f"objects: {objects:,} = {obj_s:,.0f} obj/s")
    print(f"bytes:   {bytes_done:,} = {mbps:,.0f} Mbps")


if __name__ == '__main__':
    main()
