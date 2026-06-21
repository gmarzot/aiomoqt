#!/usr/bin/env python3
"""moqperf - run publisher and subscriber against a relay.

Convenience wrapper that launches both bench_pub and bench_sub
as concurrent tasks against the same relay. Useful when both
relay and clients are on the same machine.

Usage:
  python -m aiomoqt.examples.bench_relay relay.example.com
  python -m aiomoqt.examples.bench_relay moqt://relay -s 4096 -P 4
  python -m aiomoqt.examples.bench_relay relay --datagram -t 60
"""
import argparse
import asyncio
import sys

from aiomoqt.types import parse_draft_spec


def parse_args():
    parser = argparse.ArgumentParser(
        add_help=False,
        description='aiomoqt-bench relay - combined pub/sub benchmark',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Runs bench_pub and bench_sub concurrently against the same
relay. All publisher options are passed through.

relay URL forms:
  moqt://host:port            raw QUIC (port default 443)
  https://host:port/path  H3/WT (port default 443)
  host:port                   H3/WT
  host                        H3/WT, port 443, no default path

examples:
  %(prog)s relay.example.com
  %(prog)s relay.example.com -s 4096 -P 4 -r 120
  %(prog)s moqt://relay --datagram -t 60
""")
    parser.add_argument(
        'relay', type=str,
        help='Relay URL (see forms above)')
    parser.add_argument(
        '-q', '--quic', '--use-quic', action='store_true',
        dest='force_quic',
        help='Raw QUIC even for https:// URLs')
    parser.add_argument(
        '-n', '--namespace', type=str, default='aiomoqt')
    parser.add_argument(
        '--trackname', type=str, default=None)
    parser.add_argument(
        '-D', '--datagram', action='store_true',
        help='Use datagrams instead of streams')
    parser.add_argument(
        '-s', '--object-size', type=int, default=1024,
        help='Object payload size bytes (default: 1024)')
    parser.add_argument(
        '-g', '--group-size', type=int, default=10000,
        help='Objects per group (default: 10000)')
    parser.add_argument(
        '-P', '--streams', type=int, default=1,
        help='Parallel subgroup streams (default: 1)')
    parser.add_argument(
        '-r', '--rate', type=float, default=0,
        help='Aggregate objects/sec across all streams (0=max, '
             'default: max). Per-stream emit rate is rate/streams.')
    parser.add_argument(
        '-t', '--duration', type=int, default=30,
        help='Duration seconds (default: 30)')
    parser.add_argument(
        '-i', '--interval', type=float, default=5.0,
        help='Report interval seconds (default: 5)')
    parser.add_argument(
        '-d', '--debug', action='store_true')
    parser.add_argument(
        '--keylogfile', type=str, default=None)
    parser.add_argument(
        '-k', '--insecure', action='store_true',
        help='Skip TLS certificate verification')
    parser.add_argument(
        '--draft', type=parse_draft_spec, default=None,
        help='MoQT draft version (default: tool default)')
    parser.add_argument(
        '--cc-algo', type=str, default=None,
        help='Congestion control algorithm '
             '(bbr | bbr1 | newreno | cubic | dcubic | prague | fast). '
             'Default: aiopquic default (bbr1)')
    parser.add_argument(
        '--max-queued-bytes', type=int, default=None,
        help='Aggregate publisher byte budget across ALL streams '
             '(QuicConfiguration.tx_max_queued_bytes): producer parks '
             'at stream rollover while total un-transmitted TX bytes '
             'exceed this. Steady-state latency ~ value / throughput. '
             'Default: aiopquic default (4 MiB). Pass 0 to disable.')
    parser.add_argument(
        '--max-inflight-bytes', type=int, default=None,
        help='Per-stream TX budget (aiomoqt tx_max_inflight_bytes): '
             'producer pauses while one stream\'s un-transmitted bytes '
             'exceed this. Default: aiomoqt default (1 MiB). '
             'Pass 0 to disable.')
    parser.add_argument(
        '-?', '--help', action='help',
        help='Show this help message and exit')
    return parser.parse_args()


async def main():
    args = parse_args()

    # Build sub-command arg namespaces
    pub_args = argparse.Namespace(
        relay=args.relay,
        force_quic=args.force_quic,
        namespace=args.namespace,
        trackname=args.trackname,
        datagram=args.datagram,
        object_size=args.object_size,
        group_size=args.group_size,
        streams=args.streams,
        rate=args.rate,
        duration=args.duration,
        debug=args.debug,
        keylogfile=args.keylogfile,
        insecure=args.insecure,
        draft=args.draft,
        cc_algo=args.cc_algo,
        max_queued_bytes=args.max_queued_bytes,
        max_inflight_bytes=args.max_inflight_bytes,
        pub_ns=False,
        pub_both=False,
        forward=0,
    )

    sub_args = argparse.Namespace(
        relay=args.relay,
        force_quic=args.force_quic,
        namespace=args.namespace,
        trackname=args.trackname,
        duration=args.duration,
        interval=args.interval,
        debug=args.debug,
        keylogfile=args.keylogfile,
        insecure=args.insecure,
        draft=args.draft,
        cc_algo=args.cc_algo,
        auth_token=None,
    )

    # Import run functions
    from aiomoqt.examples.pub_bench import run as pub_run
    from aiomoqt.examples.sub_bench import run as sub_run

    # Start publisher first so it registers the namespace,
    # then subscriber connects and subscribes.
    pub_task = asyncio.create_task(pub_run(pub_args))

    # Brief delay for publisher to set up
    await asyncio.sleep(1.0)

    sub_task = asyncio.create_task(sub_run(sub_args))

    await asyncio.gather(pub_task, sub_task,
                         return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
