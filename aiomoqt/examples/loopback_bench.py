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
        help='Objects/sec per stream (0=max, default: max)')
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
    return parser.parse_args()


def print_banner(args):
    mode = f"SUBGROUP x{args.streams}"
    if args.rate > 0:
        rate_s = f"{args.rate}/s per stream"
    else:
        rate_s = "max"
    print("─" * 56)
    print("  aiomoqt-bench loopback (no relay)")
    print("─" * 56)
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
        path="moq",
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE,
        partial(_on_subscribe, args=args))
    return await server.serve()


async def run_subscriber(args, stats):
    """Connect as subscriber and collect stats."""
    client = MOQTClient(
        "localhost", args.port,
        path="moq",
        verify_tls=False,
        debug=args.debug,
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

            await track.wait_closed(timeout=args.duration)
    except Exception as e:
        print(f"  Subscriber error: {e}")


async def main():
    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)

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
