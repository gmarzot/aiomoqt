"""B7 — MoQT TX framer microbench.

Exercises the publisher-side serialization hot path with no I/O to
isolate where Python overhead lives between Cython entry points.
Adaptive_bench tops out around ~700-800 Mbps single-process; this
bench breaks that ceiling down per-component so we can decide where
to spend optimization effort (batching vs Cython framer vs both).

Knobs:
  --object-size B    bytes per object payload (default 8192)
  --duration S       per-bench wall-clock (default 3.0s)
  --warmup S         per-bench warmup (default 0.5s)
  --extensions {0,1} include MOQT_TIMESTAMP_EXT extension dict (default 1)
  --batch N          batched framing burst size (default 16)
  --csv PATH         emit results CSV (one row per bench)

Run: python -m aiomoqt.tests.microbench.b7_framer
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from typing import Callable

from aiomoqt.messages.track import (
    ObjectHeader, SubgroupHeader, SUBGROUP_ID_EXPLICIT,
)
from aiomoqt.types import (
    MOQT_TIMESTAMP_EXT, ObjectStatus,
)
from aiomoqt.utils.buffer import Buffer

from ._stats import time_loop


# ---------------------------------------------------------------------------
# Per-bench probes — each isolates one component.
# ---------------------------------------------------------------------------

def make_payload(size: int) -> bytes:
    """Pre-allocated counted-bytes payload. Re-used across iterations
    so payload alloc itself isn't measured (we measure that separately
    in B7.payload-alloc)."""
    return bytes(i & 0xFF for i in range(size))


def bench_payload_alloc(size: int) -> Callable[[], None]:
    """B7.payload-alloc: cost of the publisher's per-object payload
    construction `(seq + b'|' + pad)[:size]`. Two allocations + slice
    fired per emit in the live publisher today."""
    pad = b'\x49' * size

    def _fn():
        seq = b"123.456.I"
        _ = (seq + b'|' + pad)[:size]
    return _fn


def bench_extensions_dict() -> Callable[[], None]:
    """B7.ext-dict: cost of building the per-object extension dict
    that today's publisher passes into next_object()."""
    def _fn():
        _ = {MOQT_TIMESTAMP_EXT: int(time.time() * 1_000_000)}
    return _fn


def bench_buffer_alloc(size: int) -> Callable[[], None]:
    """B7.buffer-alloc: bare aiopquic Buffer instantiation +
    push_bytes(payload). Cython floor — nothing else."""
    payload = make_payload(size)

    def _fn():
        b = Buffer(capacity=size + 32)
        b.push_uint_var(0)
        b.push_uint_var(len(payload))
        b.push_bytes(payload)
    return _fn


def bench_object_serialize(size: int, ext: bool) -> Callable[[], None]:
    """B7.obj-serialize: ObjectHeader.serialize() in isolation.
    Skips the SubgroupHeader wrapper, dict alloc, payload alloc."""
    payload = make_payload(size)
    exts = {MOQT_TIMESTAMP_EXT: 1_700_000_000_000_000} if ext else None
    obj_id = 0
    last = -1

    def _fn():
        nonlocal obj_id, last
        h = ObjectHeader(
            object_id=obj_id, extensions=exts,
            status=ObjectStatus.NORMAL, payload=payload,
        )
        h.serialize(extensions_present=ext, prev_object_id=last)
        last = obj_id
        obj_id += 1
    return _fn


def bench_next_object(size: int, ext: bool) -> Callable[[], None]:
    """B7.next-object: full SubgroupHeader.next_object() per object —
    same call the publisher's _generate_subgroup makes today.
    Includes ObjectHeader.__init__ + .serialize() + extensions encode."""
    payload = make_payload(size)
    sg = SubgroupHeader(
        track_alias=1, group_id=0, subgroup_id=0,
        publisher_priority=128, extensions_present=ext,
        subgroup_id_mode=SUBGROUP_ID_EXPLICIT,
    )
    obj_id = 0

    def _fn():
        nonlocal obj_id
        ts = int(time.time() * 1_000_000)
        exts = {MOQT_TIMESTAMP_EXT: ts} if ext else None
        sg.next_object(payload=payload, extensions=exts,
                       object_id=obj_id)
        obj_id += 1
    return _fn


def bench_full_emit(size: int, ext: bool) -> Callable[[], None]:
    """B7.full-emit: the complete per-object publisher hot path
    (modulo network) — payload build + ext dict + next_object +
    immediately discard the resulting Buffer (analog of writing it
    to the SPSC ring without actually crossing into C)."""
    pad = b'\x49' * size
    sg = SubgroupHeader(
        track_alias=1, group_id=0, subgroup_id=0,
        publisher_priority=128, extensions_present=ext,
        subgroup_id_mode=SUBGROUP_ID_EXPLICIT,
    )
    obj_id = 0
    pending: list[bytes] = []

    def _fn():
        nonlocal obj_id
        seq = f"0.{obj_id}.I".encode()
        payload = (seq + b'|' + pad)[:size]
        ts = int(time.time() * 1_000_000)
        exts = {MOQT_TIMESTAMP_EXT: ts} if ext else None
        buf = sg.next_object(payload=payload, extensions=exts,
                             object_id=obj_id)
        pending.append(buf.data)
        if len(pending) > 32:
            pending.clear()
        obj_id += 1
    return _fn


def bench_batched_emit(size: int, ext: bool, batch: int) -> Callable[[], None]:
    """B7.batched-emit: pack `batch` objects into one Buffer + one
    'send' (stand-in: bytes() of the underlying buffer). Models
    pattern (A) from the publisher discussion — amortizes per-call
    Python orchestration across N objects.

    Iteration here = one BATCH (not one object). Per-object rate is
    `batch * iters / elapsed`; the runner reports both."""
    pad = b'\x49' * size
    sg = SubgroupHeader(
        track_alias=1, group_id=0, subgroup_id=0,
        publisher_priority=128, extensions_present=ext,
        subgroup_id_mode=SUBGROUP_ID_EXPLICIT,
    )
    obj_id = 0

    def _fn():
        nonlocal obj_id
        cap = (size + 64) * batch
        out = Buffer(capacity=cap)
        for _ in range(batch):
            seq = f"0.{obj_id}.I".encode()
            payload = (seq + b'|' + pad)[:size]
            ts = int(time.time() * 1_000_000)
            exts = {MOQT_TIMESTAMP_EXT: ts} if ext else None
            inner = sg.next_object(payload=payload, extensions=exts,
                                   object_id=obj_id)
            out.push_bytes(inner.data)
            obj_id += 1
        _ = out.data    # represents the one send_stream_data call
    return _fn


def bench_subgroup_header(ext: bool) -> Callable[[], None]:
    """B7.subgroup-header: SubgroupHeader.serialize() — one per new
    subgroup stream open. Measured separately because the controller
    can amortize this over many objects."""
    def _fn():
        sg = SubgroupHeader(
            track_alias=1, group_id=42, subgroup_id=0,
            publisher_priority=128, extensions_present=ext,
            subgroup_id_mode=SUBGROUP_ID_EXPLICIT,
        )
        sg.serialize()
    return _fn


# ---------------------------------------------------------------------------
# Async probes — measure asyncio orchestration overhead in isolation.
# The actual publisher does an `await stream_write_drain` + `await
# asyncio.sleep(1/rate)` per object. These benches expose what each
# costs without any framer work in the way.
# ---------------------------------------------------------------------------

async def _async_loop(fn_async, target_seconds: float,
                      warmup_seconds: float) -> tuple[int, float]:
    end_warmup = time.perf_counter() + warmup_seconds
    while time.perf_counter() < end_warmup:
        await fn_async()
    t0 = time.perf_counter()
    end = t0 + target_seconds
    n = 0
    while time.perf_counter() < end:
        await fn_async()
        n += 1
    return n, time.perf_counter() - t0


def run_async(label: str, fn_async, duration: float, warmup: float,
              obj_size: int) -> dict:
    iters, elapsed = asyncio.run(_async_loop(fn_async, duration, warmup))
    obj_per_s = iters / elapsed if elapsed > 0 else 0.0
    ns_per_obj = (elapsed * 1e9) / iters if iters > 0 else 0.0
    return {
        "bench": label,
        "iters": iters,
        "objs": iters,
        "elapsed_s": round(elapsed, 4),
        "obj_per_s": round(obj_per_s, 1),
        "ns_per_obj": round(ns_per_obj, 1),
        "Mbps": round(obj_per_s * obj_size * 8 / 1e6, 1),
    }


def bench_asyncio_yield():
    """B7.async-yield: bare `await asyncio.sleep(0)` cost. Floor for
    any per-object asyncio orchestration."""
    async def _fn():
        await asyncio.sleep(0)
    return _fn


def bench_asyncio_paced(rate_objs: float):
    """B7.async-paced: `await asyncio.sleep(1/rate)` — what
    _generate_subgroup does today between objects."""
    interval = 1.0 / rate_objs
    next_t = [time.monotonic()]

    async def _fn():
        next_t[0] += interval
        delay = max(0.0, next_t[0] - time.monotonic())
        await asyncio.sleep(delay)
    return _fn


def bench_full_emit_async(size: int, ext: bool):
    """B7.async-full-emit: full per-object publisher work wrapped in
    an async coroutine with an `await asyncio.sleep(0)` yield. Same
    Python work as full-emit but with the asyncio orchestration tax."""
    pad = b'\x49' * size
    sg = SubgroupHeader(
        track_alias=1, group_id=0, subgroup_id=0,
        publisher_priority=128, extensions_present=ext,
        subgroup_id_mode=SUBGROUP_ID_EXPLICIT,
    )
    state = {"obj_id": 0, "pending": 0}

    async def _fn():
        oid = state["obj_id"]
        seq = f"0.{oid}.I".encode()
        payload = (seq + b'|' + pad)[:size]
        ts = int(time.time() * 1_000_000)
        exts = {MOQT_TIMESTAMP_EXT: ts} if ext else None
        sg.next_object(payload=payload, extensions=exts, object_id=oid)
        state["obj_id"] = oid + 1
        await asyncio.sleep(0)
    return _fn


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_one(label: str, fn: Callable[[], None],
            duration: float, warmup: float,
            obj_size: int, batch: int = 1) -> dict:
    iters, elapsed = time_loop(fn, target_seconds=duration,
                               warmup_seconds=warmup)
    objs = iters * batch
    obj_per_s = objs / elapsed if elapsed > 0 else 0.0
    ns_per_obj = (elapsed * 1e9) / objs if objs > 0 else 0.0
    bytes_per_s = obj_per_s * obj_size
    mbps = bytes_per_s * 8 / 1e6
    return {
        "bench": label,
        "iters": iters,
        "objs": objs,
        "elapsed_s": round(elapsed, 4),
        "obj_per_s": round(obj_per_s, 1),
        "ns_per_obj": round(ns_per_obj, 1),
        "Mbps": round(mbps, 1),
    }


def fmt_row(r: dict) -> str:
    return (
        f"  {r['bench']:<28} "
        f"{r['obj_per_s']:>14,.0f} obj/s   "
        f"{r['ns_per_obj']:>8,.0f} ns/obj   "
        f"{r['Mbps']:>9,.1f} Mbps"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--object-size", type=int, default=8192)
    p.add_argument("--duration", type=float, default=3.0)
    p.add_argument("--warmup", type=float, default=0.5)
    p.add_argument("--extensions", type=int, choices=(0, 1), default=1)
    p.add_argument("--batch", type=int, default=16)
    p.add_argument("--csv", default=None)
    p.add_argument("--draft", type=int, default=16)
    args = p.parse_args()

    ext = bool(args.extensions)
    sz = args.object_size

    print(f"B7 — MoQT TX framer microbench   "
          f"obj_size={sz}B  ext={ext}  duration={args.duration}s")
    print()
    header = (
        f"  {'bench':<28} "
        f"{'objects/sec':>14}        "
        f"{'ns/obj':>8}    "
        f"{'wire equiv':>9}"
    )
    print(header)
    print("  " + "─" * (len(header) - 2))

    results = []

    # 1) Allocation primitives — establish the floor.
    results.append(run_one("payload-alloc",
                           bench_payload_alloc(sz),
                           args.duration, args.warmup, sz))
    results.append(run_one("ext-dict-alloc",
                           bench_extensions_dict(),
                           args.duration, args.warmup, sz))
    results.append(run_one("buffer-alloc-only",
                           bench_buffer_alloc(sz),
                           args.duration, args.warmup, sz))

    # 2) Header serialization — the moving parts.
    results.append(run_one("subgroup-header",
                           bench_subgroup_header(ext),
                           args.duration, args.warmup, sz))
    results.append(run_one("object-serialize",
                           bench_object_serialize(sz, ext),
                           args.duration, args.warmup, sz))

    # 3) Full per-object publisher path.
    results.append(run_one("next-object",
                           bench_next_object(sz, ext),
                           args.duration, args.warmup, sz))
    results.append(run_one("full-emit (alloc+ext+next)",
                           bench_full_emit(sz, ext),
                           args.duration, args.warmup, sz))

    # 4) Batched emit — the (A) pattern.
    results.append(run_one(f"batched-emit (N={args.batch})",
                           bench_batched_emit(sz, ext, args.batch),
                           args.duration, args.warmup, sz,
                           batch=args.batch))

    # 5) Asyncio orchestration tax — what `await sleep(0)` costs and
    #    what the full per-object emit costs *inside* a coroutine.
    results.append(run_async("async-yield",
                             bench_asyncio_yield(),
                             args.duration, args.warmup, sz))
    results.append(run_async("async-full-emit",
                             bench_full_emit_async(sz, ext),
                             args.duration, args.warmup, sz))

    # 6) Pacer ceiling: what's the max effective rate of an
    #    `await asyncio.sleep(1/rate)` pacer at increasing target
    #    rates? Reveals the asyncio scheduling quantum on this host.
    for r in (1_000, 5_000, 10_000, 50_000, 100_000):
        results.append(run_async(f"async-paced (target={r:,}/s)",
                                 bench_asyncio_paced(r),
                                 args.duration, args.warmup, sz))

    for r in results:
        print(fmt_row(r))

    # Compare batched vs unbatched as the headline takeaway.
    one = next(r for r in results if r["bench"] == "full-emit (alloc+ext+next)")
    bat = next(r for r in results if r["bench"].startswith("batched-emit"))
    if one["obj_per_s"] > 0:
        speedup = bat["obj_per_s"] / one["obj_per_s"]
        print()
        print(f"  batched/unbatched speedup: {speedup:.2f}x")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"  wrote {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
