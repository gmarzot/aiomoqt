#!/usr/bin/env python3
"""Standalone MoQT publisher server — no relay needed.

Starts a QUIC/H3 server that generates data when a subscriber connects.
Pair with sub_bench.py in a separate shell for multi-process throughput
testing where each side gets its own CPU core.

Usage:
  # Shell 1: start publisher server
  python -m aiomoqt.examples.pub_server -s 16384 -g 10000 -P 4 -r 0

  # Shell 2: connect subscriber
  python -m aiomoqt.examples.sub_bench https://localhost:4434/moq -t 30 -i 5
"""
import argparse
import asyncio
import logging
import os

from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, MOQT_VERSION_DRAFT16
from aiomoqt.track import PublishedTrack
from aiomoqt.utils.logger import set_log_level, get_logger

logger = get_logger(__name__)


def _find_default_cert():
    """Search common locations for test certificates."""
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
        description='MoQT publisher server — standalone, no relay',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '-H', '--host', type=str, default='localhost',
        help='Bind address (default: localhost)')
    parser.add_argument(
        '-p', '--port', type=int, default=4434,
        help='Listen port (default: 4434)')
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
        '-Q', '--quic', action='store_true',
        help='Serve raw QUIC (aiopquic) instead of H3/WebTransport')
    parser.add_argument(
        '--draft', type=int, default=16,
        help='MoQT draft version when --quic (default: 16)')
    parser.add_argument(
        '-n', '--namespace', type=str, default='bench',
        help='Track namespace (default: bench)')
    parser.add_argument(
        '--trackname', type=str, default='track',
        help='Track name (default: track)')
    parser.add_argument(
        '--cert', type=str, default=CERT,
        help='TLS certificate file')
    parser.add_argument(
        '--key', type=str, default=KEY,
        help='TLS key file')
    parser.add_argument(
        '-d', '--debug', action='store_true',
        help='Enable debug output')
    return parser.parse_args()


async def _on_subscribe(session, msg, args):
    """Server-side subscribe handler using PublishedTrack."""
    track = PublishedTrack(
        session,
        namespace=args.namespace,
        trackname=args.trackname,
        object_size=args.object_size,
        group_size=args.group_size,
        num_subgroups=args.streams,
        rate=args.rate,
    )
    ok = session.subscribe_ok(request_msg=msg)
    track.track_alias = ok.track_alias
    track._generating = True
    logger.info(f"Subscriber connected, generating data "
                f"({args.object_size}B x {args.streams} streams)")
    await track.generate(session, ok.track_alias)


async def main():
    from functools import partial

    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)

    if not args.cert or not args.key:
        print("Error: TLS certificate required. "
              "Use --cert and --key, or place cert.pem/key.pem in certs/")
        return

    draft_version = (
        {14: 0xff00000e, 15: 0xff00000f, 16: 0xff000010}[args.draft]
        if args.quic else None
    )
    server = MOQTServer(
        host=args.host, port=args.port,
        certificate=args.cert, private_key=args.key,
        path="moq",
        use_quic=args.quic,
        draft_version=draft_version,
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE,
        partial(_on_subscribe, args=args))
    quic_server = await server.serve()

    transport = "raw QUIC (aiopquic)" if args.quic else "H3/WebTransport (qh3)"
    rate_s = f"{args.rate}/s" if args.rate > 0 else "max"
    print(f"MoQT publisher server ready on {args.host}:{args.port}")
    print(f"  transport: {transport}")
    print(f"  namespace: {args.namespace}/{args.trackname}")
    print(f"  objects:   {args.object_size}B x {args.streams} streams")
    print(f"  groups:    {args.group_size} objects/group")
    print(f"  rate:      {rate_s}")
    print("\nConnect subscriber:")
    if args.quic:
        print(f"  python -m aiomoqt.examples.sub_bench "
              f"moqt://{args.host}:{args.port} -t 30 -i 5 --draft {args.draft} -k")
    else:
        print(f"  python -m aiomoqt.examples.sub_bench "
              f"https://{args.host}:{args.port}/moq -t 30 -i 5 -k")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        quic_server.close()
        print("\nServer stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
