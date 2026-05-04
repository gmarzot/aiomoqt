"""B4 — Single MoQT session sustained delivery.

One MOQTSession with a publisher generating N objects/sec at S
bytes/object, and a subscriber receiving + recording per-object
e2e latency (via the timestamp extension). Loopback (publisher
runs as in-process server). Reports throughput + p50/p95/p99
latency.

Args:
  --rate R                   objects/sec target (default 100)
  --object-size N            bytes per object (default 4096)
  --duration S               seconds (default 30)
  --port N                   local port (default 47445)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from aiomoqt.tests.microbench._stats import Stats


def _find_cert():
    candidates = [
        os.path.join(os.path.dirname(__file__),
                     '..', '..', '..', 'certs', 'cert.pem'),
        '/home/gmarzot/Projects/moq/aiomoqt/certs/cert.pem',
        os.path.expanduser('~/.local/share/moqt/cert.pem'),
    ]
    for c in candidates:
        c = os.path.realpath(c)
        if os.path.exists(c):
            return c
    return None


async def _run(args):
    from aiomoqt.client import MOQTClient
    from aiomoqt.server import MOQTServer
    from aiomoqt.track import PublishedTrack, SubscribedTrack
    from aiomoqt.types import MOQTMessageType, MOQT_VERSION_DRAFT16

    cert = _find_cert()
    if not cert:
        print("ERROR: no cert found", file=sys.stderr)
        sys.exit(1)
    key = cert.replace('cert.pem', 'key.pem')

    # ---- server: publisher generates on subscribe ----
    async def on_subscribe(session, msg):
        track = PublishedTrack(
            session, namespace="bench", trackname="track",
            object_size=args.object_size, group_size=10000,
            num_subgroups=1, rate=args.rate,
        )
        track._stats_header_printed = True
        track._quiet = True
        ok = session.subscribe_ok(request_msg=msg)
        track.track_alias = ok.track_alias
        track._generating = True
        await track.generate(session, ok.track_alias)

    server = MOQTServer(
        host='127.0.0.1', port=args.port,
        certificate=cert, private_key=key,
        path="moq",
        use_quic=True,
        draft_version=MOQT_VERSION_DRAFT16,
    )
    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    server_handle = await server.serve()

    # ---- client: subscribe + stat per-object ----
    stats = Stats(name='e2e-latency')
    n_objects = 0
    bytes_done = 0
    TIMESTAMP_EXT = 0x20

    def on_object(msg, size_bytes, recv_time_ms,
                   group_id=None, subgroup_id=None):
        nonlocal n_objects, bytes_done
        send_ms = (msg.extensions.get(TIMESTAMP_EXT)
                   if msg.extensions else None)
        if send_ms is not None:
            stats.record(recv_time_ms - send_ms)
        n_objects += 1
        bytes_done += size_bytes

    client = MOQTClient(
        '127.0.0.1', args.port, path='moq',
        use_quic=True,
        verify_tls=False, debug=False,
        draft_version=MOQT_VERSION_DRAFT16,
    )

    t0 = time.perf_counter()
    try:
        async with client.connect() as session:
            await session.client_session_init()
            track = SubscribedTrack(
                session, namespace="bench", trackname="track",
                on_object=on_object,
            )
            await track.subscribe()
            await track.wait_closed(timeout=args.duration)
    finally:
        if hasattr(server_handle, 'close'):
            server_handle.close()

    elapsed = time.perf_counter() - t0
    obj_s = n_objects / elapsed if elapsed > 0 else 0
    mbps = (bytes_done * 8) / (elapsed * 1e6) if elapsed > 0 else 0
    print(f"backend: aiopquic  rate-target={args.rate}/s "
          f"object-size={args.object_size}B duration={args.duration}s")
    print(f"  delivered: {n_objects:,} objs ({obj_s:,.0f}/s) "
          f"{bytes_done:,} bytes ({mbps:.1f} Mbps) in {elapsed:.1f}s")
    print(f"  {stats.summary(unit='ms')}")


def main():
    ap = argparse.ArgumentParser(description="Single MoQT session sustained")
    ap.add_argument('--rate', type=float, default=100.0,
                    help='objects/sec target')
    ap.add_argument('--object-size', type=int, default=4096)
    ap.add_argument('--subgroups', type=int, default=1,
                    help='subgroups per group (each = separate QUIC stream)')
    ap.add_argument('--duration', type=float, default=30.0)
    ap.add_argument('--port', type=int, default=47445)
    args = ap.parse_args()

    asyncio.run(_run(args))


if __name__ == '__main__':
    main()
