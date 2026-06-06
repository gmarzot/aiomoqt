#!/usr/bin/env python3
"""aiomoqt-bench loopback - direct pub-to-sub benchmark without a relay.

Runs the publisher as a server that the subscriber connects to directly.
Measures pure Python throughput on the aiopquic stack without relay overhead.

Usage:
  python -m aiomoqt.examples.loopback_bench -s 4096 -r 5000 -t 20
  python -m aiomoqt.examples.loopback_bench -P 4 -s 16384 -r 60 -t 20
"""
import argparse
import asyncio
import logging

from aiomoqt.types import MOQTMessageType
from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.utils import wait_cond_timeout
from aiomoqt.utils.logger import set_log_level
from aiomoqt.examples.sub_bench import BenchStats


def _find_default_cert():
    """Search common locations for test certificates."""
    import os
    candidates = [
        os.path.join(os.path.dirname(__file__),
                     '..', '..', 'certs', 'cert.pem'),
        os.path.expanduser('~/.local/share/moqt/cert.pem'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.realpath(c)
    return None

CERT = _find_default_cert()
KEY = CERT.replace('cert.pem', 'key.pem') if CERT else None


def parse_args():
    parser = argparse.ArgumentParser(
        description='aiomoqt-bench loopback - direct pub/sub, no relay',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '-s', '--object-size', type=int, default=4096,
        help='Object payload size bytes (default: 4096)')
    parser.add_argument(
        '-g', '--group-size', type=int, default=10000,
        help='Objects per group (default: 10000)')
    parser.add_argument(
        '-P', '--streams', type=int, default=1,
        help='Parallel subgroup streams (default: 1)')
    parser.add_argument(
        '-r', '--rate', type=float, default=0,
        help='Aggregate objects/sec across all streams (0=max, '
             'default: max). Per-stream emit rate is rate/streams. '
             '-P only changes parallelism, not offered load.')
    parser.add_argument(
        '-t', '--duration', type=int, default=20,
        help='Duration seconds (default: 20)')
    parser.add_argument(
        '-i', '--interval', type=float, default=5.0,
        help='Report interval seconds (default: 5)')
    parser.add_argument(
        '-p', '--port', type=int, default=4434,
        help='Local port (default: 4434)')
    parser.add_argument(
        '--cert', type=str, default=CERT)
    parser.add_argument(
        '--key', type=str, default=KEY)
    parser.add_argument(
        '-d', '--debug', action='store_true')
    parser.add_argument(
        '--quic', action='store_true',
        help='Use raw QUIC instead of WebTransport (default: WT)')
    parser.add_argument(
        '--cc-algo', type=str, default='bbr',
        help='Congestion control algorithm '
             '(bbr | bbr1 | newreno | cubic | dcubic | prague | fast). '
             'Default: bbr')
    parser.add_argument(
        '--max-inflight-bytes', type=int, default=0,
        help='Per-stream producer byte-budget cap '
             '(stream_tx_buf_used > N parks producer). '
             '0 = off (default). Hysteresis: park at N, '
             'resume at N//2.')
    return parser.parse_args()


def print_banner(args):
    mode = f"SUBGROUP x{args.streams}"
    if args.rate > 0:
        per_stream = (args.rate / args.streams
                      if args.streams > 1 else args.rate)
        rate_s = f"{args.rate}/s total ({per_stream:.1f}/s per stream)"
    else:
        rate_s = "max"
    transport_label = "QUIC" if args.quic else "H3/WebTransport"
    print("─" * 56)
    print("  aiomoqt-bench loopback (no relay)")
    print("─" * 56)
    print(f"  transport:   {transport_label}")
    print(f"  mode:        {mode}")
    print(f"  object size: {args.object_size} B")
    print(f"  group size:  {args.group_size} objects")
    print(f"  rate:        {rate_s}")
    print(f"  duration:    {args.duration}s")
    print(f"  port:        {args.port}")
    print("─" * 56)


async def _on_subscribe(session, msg, args):
    """Server-side subscribe handler using PublishedTrack."""
    track = PublishedTrack(
        session,
        namespace="aiomoqt",
        trackname="track",
        object_size=args.object_size,
        group_size=args.group_size,
        num_subgroups=args.streams,
        rate=args.rate,
    )
    # Suppress publisher periodic stats in loopback mode —
    # both sides print to the same terminal, causing interleaved output
    track._stats_header_printed = True  # skip header
    track._quiet = True  # checked in _generate_subgroup
    # d14 direct connection: respond with subscribe_ok and generate
    ok = session.subscribe_ok(request_msg=msg)
    track.track_alias = ok.track_alias
    track._generating = True
    await track.generate(session, ok.track_alias)


async def run_server(args):
    """Run a MOQTServer that generates data when subscribers connect."""
    from functools import partial

    server = MOQTServer(
        host="localhost", port=args.port,
        certificate=args.cert, private_key=args.key,
        path="/",
        use_quic=args.quic,
        congestion_control_algorithm=args.cc_algo,
        tx_max_inflight_bytes=(args.max_inflight_bytes
                               if args.max_inflight_bytes > 0 else None),
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE,
        partial(_on_subscribe, args=args))
    return await server.serve()


async def run_subscriber(args, stats):
    """Connect as subscriber and collect stats."""
    client = MOQTClient(
        "localhost", args.port,
        path="/",
        use_quic=args.quic,
        verify_tls=False,
        debug=args.debug,
        congestion_control_algorithm=args.cc_algo,
    )

    try:
        async with client.connect() as session:
            await session.client_session_init()

            track = SubscribedTrack(
                session,
                namespace="aiomoqt",
                trackname="track",
                on_object=stats.on_object,
            )
            # Loopback server does not send PUBLISH; use direct SUBSCRIBE.
            # Loopback passes explicit trackname → auto-routes to direct.
            await track.subscribe()

            print("  Subscriber connected, receiving...\n")

            if not await wait_cond_timeout(
                    track.wait_closed(), timeout=args.duration):
                track.completed = True
    except Exception as e:
        print(f"  Subscriber error: {e}")


async def main():
    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)

    # AIOMOQT_TASK_DUMP=1 installs SIGUSR1 (task stacks) + SIGUSR2
    # (aiopquic counters) handlers. No-op when env not set.
    from aiomoqt.utils.taskdump import install as _install_task_dump
    _install_task_dump()

    stats = BenchStats(report_interval=args.interval)
    print_banner(args)

    if not args.cert or not args.key:
        print("  Error: TLS certificate required. "
              "Use --cert and --key,")
        print("  or place cert.pem/key.pem in <project>/certs/")
        return

    # Start server
    print("  Starting server...")
    quic_server = await run_server(args)

    # Give server a moment
    await asyncio.sleep(0.5)

    # Run subscriber
    print("  Connecting subscriber...")
    await run_subscriber(args, stats)

    # Cleanup
    quic_server.close()
    stats.print_summary()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
