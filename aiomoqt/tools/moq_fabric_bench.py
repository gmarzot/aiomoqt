#!/usr/bin/env python3
"""moq-fabric-bench: send and measure a track through a moq-fabric mesh.

--role pub announces -N/-T into the fabric and, once a node subscribes,
sends -s byte bench objects, -g per group, striped over -P subgroup
streams (or as datagrams with -D) at -r objects/s, to every subscriber.
--role sub subscribes to -N/-T and measures objects, loss, duplicates and
latency: arrival time against each object's LOC TIMESTAMP, so the sender
must share the clock (one host, or --role both). --role both runs both
ends in this process, the publisher on the first --peer and the
subscriber on the last.

Objects are MoQT draft-18 framed inside the fabric streams, as moqx
frames them. -o writes the report JSON (both ends under "pub" / "sub") on
exit; --trace-objects writes one JSON line per received object. Exit
status: 1 when the subscriber got no objects or a bad payload
(--verify-payload), 130 on a signal, else 0.

Examples:
  # publish into moqx-a; a MoQT subscriber elsewhere reads aiomoqt/t0
  moq-fabric-bench --peer 10.0.0.11 --role pub -s 1024 -g 30 -r 30 -t 60

  # subscribe through a relay, trace every object
  moq-fabric-bench --peer 10.0.0.12 --role sub -N moq-test/live -T video \\
      -t 60 --trace-objects objects.jsonl

  # one clock through the fabric: pub on relay A, sub on relay C
  moq-fabric-bench --peer 10.0.0.11 --peer 10.0.0.13 --role both \\
      -s 1024 -g 30 -r 30 -t 60 -o bench.json
"""
import asyncio
import json
import logging
import signal
import sys
import time
from typing import IO, Optional, Tuple

from aiomoqt.moq_fabric import FabricNode, build_report, parse_identity
from aiomoqt.moq_fabric.inner import DATAGRAM_MAX_INNER, encode_datagram
from aiomoqt.moq_fabric.node import FabricTrack, RxTrack
from aiomoqt.moq_fabric.report import write_report
from aiomoqt.types import LOC_TIMESTAMP
from aiomoqt.utils import cli as grid
from aiomoqt.utils.certs import ensure_self_signed, find_cert, find_key
from aiomoqt.utils.payload import counted
from aiomoqt.utils.stats import SenderStats, TrackStats

DEFAULT_LABEL = "aiomoqt-bench"
# Objects written between yields at -r 0.
YIELD_EVERY = 64


def datagram_payload_limit(group_size: int) -> int:
    """Largest -s whose datagram, LOC TIMESTAMP included, fits the cap."""
    overhead = len(encode_datagram((1 << 64) - 1, 1 << 40, group_size, b"",
                                   {LOC_TIMESTAMP: 1 << 55}))
    return DATAGRAM_MAX_INNER - overhead


def bench_payload(group_id: int, object_id: int, size: int) -> bytes:
    """aiomoqt.utils.payload's layout: "{group}.{object}.{size}|" then the
    counting pattern, cut to size."""
    head = b"%d.%d.%d|" % (group_id, object_id, size)
    return (head + counted(size))[:size]


def parse_args(argv=None):
    p = grid.make_parser(
        description="Send and measure a track through a moq-fabric mesh",
        epilog=__doc__)
    grid.add_moq_fabric(p, port=0)
    grid.add_identity(p, trackname="t0")
    grid.add_publisher_media(p)
    grid.add_split_role(p)
    grid.add_run(p)
    grid.add_logging(p)
    grid.add_interval(p)
    grid.add_payload_check(p)
    p.add_argument('-o', '--output-file', default=None, metavar='PATH',
                   help='Report JSON, written on exit')
    p.add_argument('--trace-objects', default=None, metavar='FILE',
                   help='One JSON line per received object: arrival, '
                        'NEW_STREAM header arrival and send times (ns), '
                        'session, stream, SNS, location, status, size')
    grid.add_help(p)
    args = p.parse_args(argv)
    if not args.peer and not args.port:
        p.error('nothing to do: give --peer, or a listen port with -p')
    if args.role == 'both' and not args.peer:
        p.error('--role both dials: give --peer (pub on the first, sub on '
                'the last)')
    if args.mp:
        p.error('--mp is not supported by moq-fabric-bench')
    if args.video:
        p.error('--video is not supported by moq-fabric-bench')
    if args.streams < 1 or args.group_size < 1:
        p.error('-P and -g must be at least 1')
    grid.check_datagram(p, args)
    if args.datagram:
        limit = datagram_payload_limit(args.group_size)
        if args.object_size > limit:
            p.error(f'-D: -s {args.object_size} does not fit a fabric '
                    f'datagram (max {limit} B with its headers)')
    return args


def _listener_cert(args) -> Tuple[Optional[str], Optional[str]]:
    if not args.port:
        return None, None
    cert = args.cert or find_cert()
    key = args.key or find_key(cert)
    if cert and key:
        return cert, key
    if args.cert:
        raise SystemExit(f"--cert {args.cert}: no key beside it; use --key")
    return ensure_self_signed()


async def produce(node: FabricNode, track: FabricTrack, args,
                  stop: asyncio.Event, sent: SenderStats) -> None:
    """Bench objects from the first subscription until stop."""
    await node.wait_until(lambda: track.subscribed or stop.is_set(), None)
    loop = asyncio.get_running_loop()
    period = 1.0 / args.rate if args.rate > 0 else 0.0
    lanes = 1 if args.datagram else min(args.streams, args.group_size)
    size, gsize = args.object_size, args.group_size
    next_t = loop.time()
    group = 0
    while not stop.is_set():
        sent.on_group()
        last = -1
        for oid in range(gsize):
            ext = {LOC_TIMESTAMP: time.time_ns() // 1000}
            payload = bench_payload(group, oid, size)
            if args.datagram:
                track.datagram(group, oid, payload, ext,
                               end_of_group=oid == gsize - 1)
            else:
                await track.write(group, oid % lanes, oid, payload, ext)
            sent.on_object(size)
            last = oid
            if period:
                next_t += period
                delay = next_t - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -1.0:
                    next_t = loop.time()
            elif oid % YIELD_EVERY == YIELD_EVERY - 1:
                await asyncio.sleep(0)
            if stop.is_set():
                break
        if not args.datagram:
            done = last == gsize - 1
            for lane in range(lanes):
                eog = gsize if done and lane == last % lanes else None
                await track.end_subgroup(group, lane, end_of_group=eog)
        group += 1


def _tracer(out: IO[str]):
    def hook(where, obj, rx: RxTrack) -> None:
        ext = obj.extensions or {}
        tx = ext.get(LOC_TIMESTAMP)
        out.write(json.dumps({
            "t_rx_ns": where.stamp[1],
            "t_hdr_ns": where.header_stamp[1] if where.header_stamp else None,
            "t_tx_ns": tx * 1000 if isinstance(tx, int) else None,
            "session": where.session.id,
            "stream_id": where.stream_id,
            "sns": where.sns_id,
            "hash": f"0x{where.fullname_hash:016x}",
            "group": obj.group_id,
            "subgroup": getattr(obj, "subgroup_id", None),
            "object": obj.object_id,
            "status": int(obj.status),
            "size": len(obj.payload)}) + "\n")
    return hook


def print_summary(rx: RxTrack, sent: Optional[SenderStats]) -> None:
    print()
    print("═" * 56)
    print("  moq-fabric-bench results")
    print("═" * 56)
    if sent is not None:
        print(f"  Sent:        {sent.total_objects:,} objects, "
              f"{sent.total_bytes:,} bytes, {sent.total_groups:,} groups")
    st = rx.stats
    print(f"  Received:    {st.total_objects:,} objects, "
          f"{st.total_bytes:,} bytes, {st.total_groups:,} groups")
    print(f"  Lost:        {st.total_lost:,}   dups: {st.total_dups:,}   "
          f"out of order: {st.total_ooo:,}")
    if st.lat_count:
        p50, p90, p99 = st._lat_res.percentiles(50, 90, 99)
        print(f"  Latency:     p50={p50:.2f}  p90={p90:.2f}  p99={p99:.2f} "
              f"max={st.lat_max:.2f} ms  ({st.ts_source})")
    else:
        print("  Latency:     unmeasured (no LOC TIMESTAMP)")
    ends = ", ".join(f"{k} {v}" for k, v in sorted(rx.ends.items()))
    print(f"  Streams:     {ends or 'none ended'}; "
          f"datagrams {rx.datagrams:,}")
    if rx.check is not None:
        print(f"  Payload:     {rx.check.checked:,} checked, "
              f"{rx.check.bad:,} bad")


async def run(args) -> int:
    node_id, label = parse_identity(args.node_id, DEFAULT_LABEL)
    cert, key = _listener_cert(args)
    peers = list(args.peer or ())
    pub = sub = None
    if args.role in ('pub', 'both'):
        pub = FabricNode(node_id=node_id,
                         label=f"{label}-pub" if args.role == 'both' else label,
                         peers=peers[:1] if args.role == 'both' else peers,
                         bind=args.bind, port=args.port, cert=cert, key=key,
                         metric=args.node_metric, keepalive=1.5)
    if args.role in ('sub', 'both'):
        sub = FabricNode(node_id=None if pub else node_id,
                         label=f"{label}-sub" if pub else label,
                         peers=peers[-1:] if pub else peers,
                         bind=None if pub else args.bind,
                         port=0 if pub else args.port, cert=cert, key=key,
                         metric=args.node_metric, keepalive=1.5)
    nodes = [n for n in (pub, sub) if n is not None]
    for n in nodes:
        print(f"moq-fabric-bench: {'pub' if n is pub else 'sub'} node "
              f"{n.local.contact!r} peers="
              f"{','.join(f'{h}:{p}' for h, p in n.peers) or '-'} "
              f"track {args.namespace}/{args.trackname}", flush=True)

    trace = open(args.trace_objects, "w") if args.trace_objects else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signalled = []

    def on_signal():
        signalled.append(True)
        stop.set()
        for n in nodes:
            n.notify()
    handled = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
            handled.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    sent = SenderStats() if pub else None
    rx: Optional[RxTrack] = None
    producer = None
    rc = 0
    try:
        for n in nodes:
            await n.start()
        if sub is not None:
            if trace is not None:
                sub.object_hook = _tracer(trace)
            stats_kw = dict(windowed=False, minimal=args.no_stats)
            rx = sub.receive(args.namespace, args.trackname,
                             verify_every=args.verify_payload,
                             stats=stats_kw)
        if pub is not None and sent is not None:
            track = pub.publish(args.namespace, args.trackname)
            producer = asyncio.ensure_future(
                produce(pub, track, args, stop, sent))
        if rx is not None and args.interval > 0:
            print(TrackStats.header())
            print("  " + "─" * 101)
        start = time.monotonic()
        deadline = start + args.duration if args.duration else None
        tick = start + args.interval if args.interval > 0 else None
        while not stop.is_set():
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            if tick is not None and now >= tick:
                if rx is not None and rx.stats.start_time:
                    print(rx.stats.interval_row(now), flush=True)
                elif rx is None and sent is not None and sent.start_time:
                    print(sent.interval_row(now), flush=True)
                while tick <= now:
                    tick += args.interval
            wakes = [t - now for t in (deadline, tick) if t is not None]
            try:
                await asyncio.wait_for(stop.wait(),
                                       min(wakes) if wakes else None)
            except asyncio.TimeoutError:
                pass
        stop.set()
        for n in nodes:
            n.notify()
        if producer is not None:
            await producer
        if signalled:
            rc = 130
        if rx is not None:
            await asyncio.sleep(0.2)
            print_summary(rx, sent)
            if rx.stats.total_objects == 0:
                print("  FAIL: no objects received", flush=True)
                rc = rc or 1
            if rx.check is not None and rx.check.bad:
                rc = rc or 1
        elif sent is not None:
            print(f"  Sent: {sent.total_objects:,} objects, "
                  f"{sent.total_bytes:,} bytes", flush=True)
        if args.output_file:
            report = {"role": args.role}
            if pub is not None:
                report["pub"] = build_report(pub)
            if sub is not None:
                report["sub"] = build_report(sub)
            write_report(args.output_file, report)
    finally:
        stop.set()
        if producer is not None and not producer.done():
            producer.cancel()
        for n in nodes:
            await n.stop()
        if trace is not None:
            trace.close()
        for sig in handled:
            loop.remove_signal_handler(sig)
    return rc


def main(argv=None) -> int:
    args = parse_args(argv)
    grid.apply_logging(args, default=logging.WARNING)
    return grid.run(run(args))


def cli():
    """Console entry point (moq-fabric-bench)."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
