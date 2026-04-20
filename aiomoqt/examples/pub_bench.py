#!/usr/bin/env python3
"""aiomoqt-bench publisher - sends timestamped MoQT objects through a relay.

Usage:
  # H3/WebTransport (default)
  python -m aiomoqt.examples.bench_pub https://relay.example.com/moq

  # Raw QUIC
  python -m aiomoqt.examples.bench_pub moqt://relay.example.com

  # Bare hostname (defaults to https, port 443, endpoint /moq)
  python -m aiomoqt.examples.bench_pub relay.example.com

  # With options
  python -m aiomoqt.examples.bench_pub relay.example.com -s 4096 -P 4 -r 120 -t 60
"""
import argparse
import asyncio
import logging

from aiomoqt.client import MOQTClient
from aiomoqt.track import PublishedTrack, TrackState
from aiomoqt.utils.logger import set_log_level, get_logger
from aiomoqt.utils.url import parse_relay_url


def parse_args():
    parser = argparse.ArgumentParser(
        description='aiomoqt-bench publisher - MoQT benchmark sender',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
relay URL forms:
  moqt://host:port            raw QUIC (default port 443)
  https://host:port/endpoint  H3/WebTransport (default port 443)
  host:port                   H3/WebTransport
  host                        H3/WebTransport, port 443, endpoint /moq

examples:
  %(prog)s moqt://relay.example.com
  %(prog)s relay.example.com -s 4096 -P 4
  %(prog)s relay.example.com --datagram -s 1100 -r 120 -t 60
""")
    parser.add_argument('relay', type=str,
                        help='Relay URL: moqt://host:port, https://host:port/ep, or host[:port]')
    parser.add_argument('-Q', '--force-quic', action='store_true',
                        help='Force raw QUIC even for https:// URLs')
    parser.add_argument('-n', '--namespace', type=str, default='aiomoqt',
                        help='MoQT namespace (default: aiomoqt)')
    parser.add_argument('--trackname', type=str, default=None,
                        help='MoQT track name (default: <profile>-<uuid4>)')
    parser.add_argument('-D', '--datagram', action='store_true',
                        help='Use datagrams instead of streams')
    parser.add_argument('-s', '--object-size', type=int, default=1024,
                        help='Object payload size in bytes (default: 1024)')
    parser.add_argument('-g', '--group-size', type=int, default=10000,
                        help='Objects per group (default: 10000)')
    parser.add_argument('-P', '--streams', type=int, default=1,
                        help='Parallel subgroup streams (default: 1)')
    parser.add_argument('-r', '--rate', type=float, default=0,
                        help='Objects/sec per stream (0=max, default: max)')
    parser.add_argument('-t', '--duration', type=int, default=30,
                        help='Duration in seconds (default: 30)')
    pub_mode = parser.add_mutually_exclusive_group()
    pub_mode.add_argument('--pub-ns', action='store_true',
                          help='Flow A: PUB_NS only, wait for SUBSCRIBE '
                               '(no PUBLISH). Default is Flow B: bare '
                               'PUBLISH.')
    pub_mode.add_argument('--pub-both', action='store_true',
                          help='Hybrid: PUB_NS + PUBLISH (legacy relays '
                               'that want both; breaks on CF d14).')
    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('--keylogfile', type=str, default=None)
    parser.add_argument('-k', '--insecure', action='store_true',
                        help='Skip TLS certificate verification')
    parser.add_argument('--draft', type=int, default=None,
                        help='MoQT draft version (e.g. 14, 16)')
    args = parser.parse_args()
    if args.trackname is None:
        import uuid
        sz = args.object_size
        sz_s = f"{sz // 1000}k" if sz >= 1000 else f"{sz}b"
        rate_s = f"{int(args.rate)}fps" if args.rate > 0 else "max"
        mode = "dgram" if args.datagram else f"x{args.streams}"
        uid = uuid.uuid4().hex[:4]
        args.trackname = f"{sz_s}-{rate_s}-{mode}-{uid}"
    return args


def print_banner(relay, args):
    mode = "DATAGRAM" if args.datagram else f"SUBGROUP x{args.streams}"
    if args.rate > 0:
        n = 1 if args.datagram else args.streams
        mbps = args.object_size * args.rate * n * 8 / 1e6
        rate_s = f"{args.rate}/s per stream"
        target_s = f"{mbps:.2f} Mbps"
    else:
        rate_s = "max"
        target_s = "max"
    print("─" * 56)
    print("  aiomoqt-bench publisher")
    print("─" * 56)
    print(f"  relay:       {relay}")
    print(f"  transport:   {relay.transport_name}")
    print(f"  namespace:   {args.namespace}")
    print(f"  trackname:   {args.trackname}")
    print(f"  mode:        {mode}")
    print(f"  object size: {args.object_size} B")
    print(f"  group size:  {args.group_size} objects")
    print(f"  rate:        {rate_s}")
    print(f"  target:      {target_s}")
    print(f"  duration:    {args.duration}s")
    print("─" * 56)


async def run(args):
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)

    relay = parse_relay_url(args.relay, force_quic=args.force_quic)
    print_banner(relay, args)

    client = MOQTClient(
        relay.host, relay.port,
        endpoint=relay.endpoint,
        use_quic=relay.use_quic,
        verify_tls=not args.insecure,
        draft_version=args.draft,
        debug=args.debug,
        keylog_filename=args.keylogfile,
    )

    print(f"  Connecting...")
    async with client.connect() as session:
        try:
            await session.client_session_init()

            track = PublishedTrack(
                session,
                namespace=args.namespace,
                trackname=args.trackname,
                object_size=args.object_size,
                group_size=args.group_size,
                num_subgroups=args.streams,
                rate=args.rate,
                draft=args.draft,
            )
            await track.publish(
                announce_namespace=(args.pub_ns or args.pub_both),
                publish_track=(not args.pub_ns or args.pub_both),
            )
            print(f"  Published '{track.fqtn}', waiting for subscriber...")

            await track.wait_closed(timeout=args.duration)
            if track.state != TrackState.CLOSED:
                print(f"\n  Duration {args.duration}s reached.")
        except Exception as e:
            print(f"  Error: {e}")

    print("  Done.")


if __name__ == "__main__":
    try:
        args = parse_args()
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
