"""B5 — Multi-session aggregate scaling.

Sweeps N sessions in one process, each delivering a constant-rate
constant-size MoQT object stream. Reports aggregate objects/sec,
asyncio-thread CPU%, and p99 latency at each N. Plots scaling
behavior across the architectures.

Today only `--backend qh3` is wired. aiopquic path enables after
Phase D.

Args:
  --backend qh3|aiopquic   transport (default qh3)
  --sessions LIST          comma list of N values (default 1,5,25,50,100)
  --rate R                 obj/sec per session (default 100)
  --object-size N          bytes per object (default 4096)
  --duration S             seconds per N (default 20)
  --port N                 base port (default 47500)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import resource
import ssl
import sys
import time

from aiomoqt.tests.microbench._stats import Stats


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


async def _run_qh3_for_n(n_sessions, args):
    from qh3.quic.configuration import QuicConfiguration
    from qh3.asyncio.server import serve
    from qh3.h3.connection import H3_ALPN

    from aiomoqt.client import MOQTClient
    from aiomoqt.protocol import MOQTPeer, MOQTSession
    from aiomoqt.track import PublishedTrack, SubscribedTrack
    from aiomoqt.types import MOQTMessageType

    cert = _find_cert()
    if not cert:
        print("ERROR: no cert found", file=sys.stderr)
        sys.exit(1)
    key = cert.replace('cert.pem', 'key.pem')

    server_peer = MOQTPeer()
    server_peer.endpoint = "moq"

    async def on_subscribe(session, msg):
        track = PublishedTrack(
            session, namespace="bench", trackname="track",
            object_size=args.object_size, group_size=100000,
            num_subgroups=1, rate=args.rate,
        )
        track._stats_header_printed = True
        track._quiet = True
        ok = session.subscribe_ok(request_msg=msg)
        track.track_alias = ok.track_alias
        track._generating = True
        await track.generate(session, ok.track_alias)

    server_peer.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)

    server_cfg = QuicConfiguration(
        is_client=False, alpn_protocols=H3_ALPN,
        verify_mode=ssl.CERT_NONE,
        max_data=2**24, max_stream_data=2**24,
        max_datagram_frame_size=64 * 1024,
    )
    server_cfg.load_cert_chain(cert, key)

    server = await serve(
        host='127.0.0.1', port=args.port,
        configuration=server_cfg,
        create_protocol=lambda *a, **kw: MOQTSession(
            *a, **kw, session=server_peer),
    )

    stats = Stats(name=f"n={n_sessions}")
    counters = {'objs': 0, 'bytes': 0}
    TIMESTAMP_EXT = 0x20

    def on_object(msg, size_bytes, recv_time_ms,
                  group_id=None, subgroup_id=None):
        send_ms = (msg.extensions.get(TIMESTAMP_EXT)
                   if msg.extensions else None)
        if send_ms is not None:
            stats.record(recv_time_ms - send_ms)
        counters['objs'] += 1
        counters['bytes'] += size_bytes

    async def one_subscriber(i):
        client = MOQTClient(
            '127.0.0.1', args.port, endpoint='moq',
            verify_tls=False, debug=False,
        )
        try:
            async with client.connect() as session:
                await session.client_session_init()
                track = SubscribedTrack(
                    session, namespace="bench",
                    trackname=f"track-{i}",
                    on_object=on_object,
                )
                await track.subscribe()
                await track.wait_closed(timeout=args.duration)
        except Exception as e:
            print(f"  session {i}: {type(e).__name__}: {e}",
                  file=sys.stderr)

    cpu_t0 = resource.getrusage(resource.RUSAGE_SELF)
    t0 = time.perf_counter()
    try:
        await asyncio.gather(
            *(one_subscriber(i) for i in range(n_sessions)),
            return_exceptions=True,
        )
    finally:
        if hasattr(server, 'close'):
            server.close()
    elapsed = time.perf_counter() - t0
    cpu_t1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu_used = ((cpu_t1.ru_utime + cpu_t1.ru_stime)
                - (cpu_t0.ru_utime + cpu_t0.ru_stime))
    cpu_pct = (cpu_used / elapsed) * 100 if elapsed > 0 else 0

    obj_s = counters['objs'] / elapsed if elapsed > 0 else 0
    mbps = (counters['bytes'] * 8) / (elapsed * 1e6) if elapsed > 0 else 0
    expected = n_sessions * args.rate
    pct_target = (obj_s / expected) * 100 if expected else 0
    print(f"n={n_sessions:>3}  delivered={counters['objs']:>9,}  "
          f"agg={obj_s:>8,.0f} obj/s ({pct_target:5.1f}% of target)  "
          f"{mbps:>6.1f} Mbps  cpu={cpu_pct:5.1f}%  "
          f"{stats.summary(unit='ms')}")


def main():
    ap = argparse.ArgumentParser(description="Multi-session aggregate scaling")
    ap.add_argument('--backend', choices=['qh3', 'aiopquic'], default='qh3')
    ap.add_argument('--sessions', default='1,5,25,50,100',
                    help='comma list of N values')
    ap.add_argument('--rate', type=float, default=100.0)
    ap.add_argument('--object-size', type=int, default=4096)
    ap.add_argument('--duration', type=float, default=20.0)
    ap.add_argument('--port', type=int, default=47500)
    args = ap.parse_args()

    if args.backend == 'aiopquic':
        print("backend=aiopquic: not wired yet — enables after Phase D",
              file=sys.stderr)
        sys.exit(2)

    Ns = [int(x.strip()) for x in args.sessions.split(',') if x.strip()]
    print(f"sweep: backend=qh3  sessions={Ns}  "
          f"per-session rate={args.rate}/s × {args.object_size}B  "
          f"duration={args.duration}s each")
    print()
    for n in Ns:
        asyncio.run(_run_qh3_for_n(n, args))


if __name__ == '__main__':
    main()
