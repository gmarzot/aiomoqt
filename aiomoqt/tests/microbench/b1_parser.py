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

from aiomoqt.context import profile_for
from aiomoqt.messages.base import MOQTUnderflow
from aiomoqt.messages.track import SubgroupHeader, ObjectHeader
from aiopquic.streamchain import StreamChain
from aiomoqt.tests.microbench._bytestream import (
    make_subgroup_stream, chunked,
)


def _parse_subgroup_stream(chain: StreamChain, prof=None) -> int:
    """Drive the parser over one subgroup stream's worth of bytes.

    Returns object count parsed. Mirrors what _moqt_handle_data_stream
    does (without the protocol-session bookkeeping). `prof` selects the
    draft codec: the chain is tagged vi64 (d18) so its push/pull and the
    fused object parser dispatch to the right integer flavor."""
    vi64 = prof is not None and prof.vi64
    chain.vi64 = vi64

    # Stream type (varint flavor per draft) + SubgroupHeader
    stream_type = chain.pull_uint_var()
    sg = SubgroupHeader.deserialize(chain, type_val=stream_type, prof=prof)

    n_objects = 0
    cap = chain.capacity
    obj = ObjectHeader.__new__(ObjectHeader)
    while chain.tell() < cap:
        chain.save()
        try:
            obj.deserialize_into(
                chain, cap,
                extensions_present=sg.extensions_present,
                prev_object_id=sg._last_object_id,
                vi64=vi64,
            )
        except MOQTUnderflow:
            chain.rollback()
            break
        sg._last_object_id = obj.object_id
        n_objects += 1
        chain.commit()
        cap = chain.capacity
    return n_objects


_COMPARE_DRAFTS = (14, 16, 18)


def _run_draft(draft, n_objects, payload_size, chunk_size,
               duration, warmup) -> dict:
    """Build the draft's corpus and time the RX parse loop over it.
    Returns a result dict (obj/s, ns/obj, Mbps, corpus size)."""
    prof = profile_for(draft)
    data = make_subgroup_stream(n_objects, payload_size, draft=draft)
    chunks = chunked(data, chunk_size) if chunk_size else [data]

    end = time.perf_counter() + warmup
    while time.perf_counter() < end:
        chain = StreamChain()
        for c in chunks:
            chain.extend(c)
        _parse_subgroup_stream(chain, prof=prof)

    t0 = time.perf_counter()
    end = t0 + duration
    streams = objects = bytes_done = 0
    while time.perf_counter() < end:
        chain = StreamChain()
        for c in chunks:
            chain.extend(c)
        objects += _parse_subgroup_stream(chain, prof=prof)
        streams += 1
        bytes_done += len(data)
    elapsed = time.perf_counter() - t0
    return {
        "draft": draft,
        "corpus": len(data),
        "obj_s": objects / elapsed if elapsed else 0.0,
        "ns_per_obj": (elapsed * 1e9) / objects if objects else 0.0,
        "mbps": (bytes_done * 8) / (elapsed * 1e6) if elapsed else 0.0,
    }


def run_compare(n_objects, payload_size, chunk_size, duration, warmup) -> int:
    """Cross-draft RX-parse comparison: parse each draft's valid corpus and
    print an obj/s + ns/obj table with a relative-vs-d16 column + SUMMARY."""
    print(f"B1 — cross-draft RX parse comparison   "
          f"{n_objects} objs × {payload_size}B  duration={duration}s")
    print()
    res = {d: _run_draft(d, n_objects, payload_size, chunk_size,
                         duration, warmup) for d in _COMPARE_DRAFTS}

    hdr = (f"  {'draft':<8}{'obj/s':>16}{'ns/obj':>12}"
           f"{'Mbps':>12}{'ns vs d16':>12}")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    base = res[16]["ns_per_obj"]
    for d in _COMPARE_DRAFTS:
        r = res[d]
        rel = r["ns_per_obj"] / base if base else 0.0
        print(f"  d{d:<7}{r['obj_s']:>16,.0f}{r['ns_per_obj']:>12,.0f}"
              f"{r['mbps']:>12,.0f}{rel:>11.2f}x")

    r18 = res[18]["ns_per_obj"] / base if base else 0.0
    r14 = res[14]["ns_per_obj"] / base if base else 0.0
    print()
    print("SUMMARY  (parse ns/obj relative to d16)")
    print(f"  RX parse   d18/d16 {r18:5.2f}x   d14/d16 {r14:5.2f}x")
    print(f"  -> d18 vi64 subgroup parse is within {abs(r18 - 1.0) * 100:.0f}% "
          f"of d16's RFC9000 path; the fused per-object parser keeps the "
          f"codec fork off the hot loop.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="MoQT parser throughput")
    ap.add_argument('--n-objects', type=int, default=1000)
    ap.add_argument('--payload-size', type=int, default=4096)
    ap.add_argument('--chunk-size', type=int, default=1500)
    ap.add_argument('--duration', type=float, default=5.0)
    ap.add_argument('--warmup', type=float, default=0.5)
    ap.add_argument('--draft', default='16',
                    help="draft number, or 'all' for the cross-draft "
                         "RX-parse comparison")
    args = ap.parse_args()

    if str(args.draft).lower() == 'all':
        return run_compare(args.n_objects, args.payload_size,
                           args.chunk_size, args.duration, args.warmup)

    draft = int(args.draft)
    prof = profile_for(draft)
    data = make_subgroup_stream(args.n_objects, args.payload_size, draft=draft)
    chunks = chunked(data, args.chunk_size) if args.chunk_size else [data]
    print(f"corpus (d{draft}): {len(data):,} bytes, {len(chunks)} chunks of "
          f"~{args.chunk_size}B ({args.n_objects} objs × "
          f"{args.payload_size}B payload)")

    # Warmup
    end = time.perf_counter() + args.warmup
    while time.perf_counter() < end:
        chain = StreamChain()
        for c in chunks:
            chain.extend(c)
        _parse_subgroup_stream(chain, prof=prof)

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
        objects += _parse_subgroup_stream(chain, prof=prof)
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
