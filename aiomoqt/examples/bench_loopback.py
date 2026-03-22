#!/usr/bin/env python3
"""moqperf loopback - direct pub-to-sub benchmark without a relay.

Runs the publisher as a server that the subscriber connects to directly.
This measures pure Python/qh3 throughput without relay overhead.

Usage:
  python -m aiomoqt.examples.bench_loopback -s 4096 -r 5000 -t 20
  python -m aiomoqt.examples.bench_loopback -D -s 1100 -r 20000
"""
import argparse
import asyncio
import logging
import ssl
from functools import partial

from qh3.quic.configuration import QuicConfiguration
from qh3.asyncio.server import serve
from qh3.h3.connection import H3_ALPN

from aiomoqt.types import MOQTMessageType, ParamType
from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTPeer, MOQTSession
from aiomoqt.messages import SubscribeError, SubscribeNamespaceError
from aiomoqt.utils.logger import set_log_level, get_logger
from aiomoqt.examples.bench_pub import (
    subscribe_data_generator,
    dgram_subscribe_data_generator,
)
from aiomoqt.examples.bench_sub import BenchStats

CERT = "/home/gmarzot/Projects/moq/picoquic/certs/cert.pem"
KEY = "/home/gmarzot/Projects/moq/picoquic/certs/key.pem"


def parse_args():
    parser = argparse.ArgumentParser(
        description='moqperf loopback - direct pub/sub, no relay',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '-D', '--datagram', action='store_true',
        help='Use datagrams instead of streams')
    parser.add_argument(
        '-s', '--object-size', type=int, default=4096,
        help='Object payload size bytes (default: 4096)')
    parser.add_argument(
        '-g', '--group-size', type=int, default=100000,
        help='Objects per group (default: 100000)')
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
    mode = "DATAGRAM" if args.datagram else f"STREAM x{args.streams}"
    if args.rate > 0:
        n = 1 if args.datagram else args.streams
        rate_s = f"{args.rate}/s per stream"
    else:
        rate_s = "max"
    print("─" * 56)
    print("  moqperf loopback (no relay)")
    print("─" * 56)
    print(f"  mode:        {mode}")
    print(f"  object size: {args.object_size} B")
    print(f"  group size:  {args.group_size} objects")
    print(f"  rate:        {rate_s}")
    print(f"  duration:    {args.duration}s")
    print(f"  port:        {args.port}")
    print("─" * 56)


async def run_server(args):
    """Run a server that generates data when subscribers connect."""
    server_peer = MOQTPeer()
    server_peer.endpoint = "moq"

    # Register the data generator as the subscribe handler
    if args.datagram:
        handler = partial(dgram_subscribe_data_generator,
                          object_size=args.object_size,
                          group_size=args.group_size,
                          rate=args.rate)
    else:
        handler = partial(subscribe_data_generator,
                          num_tasks=args.streams,
                          object_size=args.object_size,
                          group_size=args.group_size,
                          rate=args.rate)
    server_peer.register_handler(MOQTMessageType.SUBSCRIBE, handler)

    config = QuicConfiguration(
        is_client=False,
        alpn_protocols=H3_ALPN,
        verify_mode=ssl.CERT_NONE,
        max_data=2**24,
        max_stream_data=2**24,
        max_datagram_frame_size=64 * 1024,
    )
    config.load_cert_chain(args.cert, args.key)

    protocol_factory = (
        lambda *a, **kw: MOQTSession(*a, **kw, session=server_peer)
    )

    quic_server = await serve(
        "localhost", args.port,
        configuration=config,
        create_protocol=protocol_factory,
    )
    return quic_server


async def run_subscriber(args, stats):
    """Connect as subscriber and collect stats."""
    client = MOQTClient(
        "localhost", args.port,
        endpoint="moq",
        debug=args.debug,
    )

    try:
        async with client.connect() as session:
            session.on_object_received = stats.on_object

            await session.client_session_init()

            resp = await session.subscribe_namespace(
                namespace_prefix="bench",
                parameters={ParamType.AUTH_TOKEN: b"bench"},
                wait_response=True,
            )
            if isinstance(resp, SubscribeNamespaceError):
                print(f"  SubscribeNamespace error: {resp}")
                return

            resp = await session.subscribe(
                namespace="bench",
                track_name="track",
                parameters={
                    ParamType.AUTH_TOKEN: b"bench",
                },
                wait_response=True,
            )
            if isinstance(resp, SubscribeError):
                print(f"  Subscribe error: {resp}")
                return

            print("  Subscriber connected, receiving...\n")

            try:
                await asyncio.wait_for(
                    session.async_closed(),
                    timeout=args.duration,
                )
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        print(f"  Subscriber error: {e}")


async def main():
    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)

    stats = BenchStats(report_interval=args.interval)
    print_banner(args)

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
