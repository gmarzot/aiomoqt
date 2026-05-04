"""B6 — MoQT subgroup-stream churn (data-plane lifecycle frequency).

Hammers the publisher/subscriber pipeline with short-lived subgroup
streams: open uni stream → write subgroup header → write N objects →
FIN. New stream for the next group. This is the codec workload's
churn pattern minimized — small objects + small groups so per-stream
lifecycle cost dominates over per-object payload cost.

Loopback in-process MOQTServer + MOQTClient (raw QUIC). For
measuring MoQT-layer create/destroy overhead, in-process coupling
is fine — both sides exercise the same code paths and the asyncio
fairness concern only distorts tail-latency measurements, not
sustained-throughput-of-streams.

Compares to B3 (transport-only N-stream churn) to expose the cost
of: SubgroupHeader serialize/parse, track_alias bookkeeping,
_subgroup_stream_by_key + _stream_tasks lifecycle, MoQT-level
admission control.

Args:
  --groups N         total groups to send (default 5000)
  --group-size N     objects per group (default 4)
  --subgroups P      parallel subgroups per group (default 1)
  --object-size B    bytes per object (default 64)
  --duration S       overall cap, seconds (default 30)
  --port N           local port (default 47446)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import MOQTMessageType, MOQT_VERSION_DRAFT16


def _find_cert():
    candidates = [
        os.path.realpath(os.path.join(
            os.path.dirname(__file__), '..', '..', '..', 'certs', 'cert.pem')),
        '/home/gmarzot/Projects/moq/aiomoqt/certs/cert.pem',
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


async def _run(args):
    cert = _find_cert()
    if not cert:
        print("ERROR: no cert found", file=sys.stderr)
        sys.exit(1)
    key = cert.replace('cert.pem', 'key.pem')

    # ---- server: publisher generates fixed N groups then quits ----
    pub_done = asyncio.Event()

    async def on_subscribe(session, msg):
        track = PublishedTrack(
            session, namespace="bench", trackname="churn",
            object_size=args.object_size,
            group_size=args.group_size,
            num_subgroups=args.subgroups,
            rate=0,  # max rate
        )
        track._stats_header_printed = True
        track._quiet = True
        ok = session.subscribe_ok(request_msg=msg)
        track.track_alias = ok.track_alias
        track._generating = True
        # Cap groups by stop-cancellation when target hit.
        gen_task = asyncio.create_task(
            track.generate(session, ok.track_alias))
        try:
            while track._total_groups < args.groups:
                await asyncio.sleep(0.05)
                if pub_done.is_set():
                    break
        finally:
            gen_task.cancel()
            try:
                await gen_task
            except (asyncio.CancelledError, Exception):
                pass

    server = MOQTServer(
        host='127.0.0.1', port=args.port,
        certificate=cert, private_key=key,
        path="moq",
        use_quic=True,
        draft_version=MOQT_VERSION_DRAFT16,
    )
    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    server_handle = await server.serve()

    # ---- client: subscribe, count subgroup-stream lifecycles ----
    n_subgroups_seen = 0  # count per-stream completions on subscriber side
    n_objects = 0
    bytes_done = 0
    last_stream_id = -1
    streams_started = 0

    def on_object(msg, size_bytes, recv_time_us,
                  group_id=None, subgroup_id=None):
        nonlocal n_objects, bytes_done, last_stream_id, streams_started
        n_objects += 1
        bytes_done += size_bytes
        # group_id is shared, subgroup_id changes per concurrent subgroup;
        # fall back to (group_id, subgroup_id) tuple uniqueness via a set
        # for accuracy if needed. For sustained-rate, the stream count is
        # n_groups * args.subgroups, which we compute at end.

    client = MOQTClient(
        '127.0.0.1', args.port, path='moq',
        use_quic=True, verify_tls=False,
        draft_version=MOQT_VERSION_DRAFT16,
    )

    t0 = time.perf_counter()
    try:
        async with client.connect() as session:
            await session.client_session_init()
            track = SubscribedTrack(
                session, namespace="bench", trackname="churn",
                on_object=on_object,
            )
            await track.subscribe()
            # Wait for either time cap or expected total.
            target_objs = args.groups * args.group_size
            deadline = t0 + args.duration
            while (n_objects < target_objs
                   and time.perf_counter() < deadline):
                await asyncio.sleep(0.05)
            pub_done.set()
            await asyncio.sleep(0.2)  # let trailing FINs deliver
    finally:
        if hasattr(server_handle, 'close'):
            server_handle.close()

    elapsed = time.perf_counter() - t0
    n_subgroups_seen = (
        (n_objects // args.group_size) * args.subgroups
        if args.group_size > 0 else 0
    )
    rate_groups = (n_subgroups_seen / args.subgroups) / elapsed \
        if elapsed > 0 else 0
    rate_subgroups = n_subgroups_seen / elapsed if elapsed > 0 else 0
    rate_objs = n_objects / elapsed if elapsed > 0 else 0
    mbps = (bytes_done * 8) / (elapsed * 1e6) if elapsed > 0 else 0
    print(f"backend: aiopquic  groups-target={args.groups} "
          f"group-size={args.group_size} subgroups/group={args.subgroups} "
          f"object-size={args.object_size}B")
    print(f"  delivered: {n_subgroups_seen:,} subgroup-streams  "
          f"{n_objects:,} objs  {bytes_done:,} bytes  "
          f"in {elapsed:.2f}s")
    print(f"  rates: {rate_groups:,.0f} groups/s  "
          f"{rate_subgroups:,.0f} subgroup-streams/s  "
          f"{rate_objs:,.0f} obj/s  {mbps:.1f} Mbps")


def main():
    ap = argparse.ArgumentParser(
        description="MoQT subgroup-stream churn (lifecycle frequency)")
    ap.add_argument('--groups', type=int, default=5000)
    ap.add_argument('--group-size', type=int, default=4)
    ap.add_argument('--subgroups', type=int, default=1)
    ap.add_argument('--object-size', type=int, default=64)
    ap.add_argument('--duration', type=float, default=30.0)
    ap.add_argument('--port', type=int, default=47446)
    args = ap.parse_args()

    asyncio.run(_run(args))


if __name__ == '__main__':
    main()
