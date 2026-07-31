#!/usr/bin/env python3
"""aiomoqt publisher bench — sends timestamped MoQT objects to a relay.

  moq-pub-bench moqt://relay.example.com:4433
  moq-pub-bench https://relay.example.com/moq -s 4096 -P 4 -r 120 -t 60
  moq-pub-bench moqt://relay:4433 -D -s 1100 -r 500      # datagrams
  moq-pub-bench moqt://relay:4433 --video 1080p          # media profile
"""
import asyncio
import logging

from aiomoqt.client import MOQTClient
from aiomoqt.types import ForwardingPreference
from aiomoqt.track import PublishedTrack, VideoTrack
from aiomoqt.utils import wait_cond_timeout
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.url import parse_relay_url


def parse_args():
    parser = _cli.make_parser(
        'aiomoqt publisher bench — MoQT benchmark sender',
        epilog="""
The URL scheme selects the transport:
  moqt://host[:port][/path]   raw QUIC
  https://host[:port][/path]  H3/WebTransport
  host[:port]                 H3/WebTransport
""")
    _cli.add_endpoint(parser)
    _cli.add_identity(parser)
    _cli.add_publisher_media(parser)
    _cli.add_run(parser, interval=False)
    _cli.add_session(parser)
    pub_mode = parser.add_mutually_exclusive_group()
    pub_mode.add_argument('--pub-ns', action='store_true',
                          help='Flow A: PUB_NS only, wait for SUBSCRIBE '
                               '(no PUBLISH). Default is Flow B: bare '
                               'PUBLISH.')
    pub_mode.add_argument('--pub-both', action='store_true',
                          help='Hybrid: PUB_NS + PUBLISH (legacy relays '
                               'that want both; breaks on CF d14).')
    parser.add_argument('--forward', type=int, nargs='?', const=1,
                        default=0, choices=(0, 1),
                        help='[EXPERIMENTAL] Initial Forward State in '
                             'PUBLISH (d16 §8.2). Default 0 = '
                             'spec-conservative. NOT supported by the '
                             'spec: relays reject the unsolicited uni '
                             'streams. Retained for wire experiments.')
    _cli.add_help(parser)
    args = parser.parse_args()
    if args.video and args.datagram:
        parser.error('--video cannot use -D/--datagram: profile I-frames '
                     f'({VideoTrack.PROFILES[args.video]["i_frame"]} B) '
                     'far exceed the one-packet datagram ceiling')
    _cli.check_datagram(parser, args,
                       serve_quic=parse_relay_url(args.url).use_quic)
    if args.trackname is None:
        import uuid
        sz = args.object_size
        sz_s = f"{sz // 1000}k" if sz >= 1000 else f"{sz}b"
        rate_s = f"{int(args.rate)}fps" if args.rate > 0 else "max"
        mode = "dgram" if args.datagram else f"x{args.streams}"
        args.trackname = f"{sz_s}-{rate_s}-{mode}-{uuid.uuid4().hex[:4]}"
    return args


def print_banner(relay, args):
    mode = "DATAGRAM" if args.datagram else f"SUBGROUP x{args.streams}"
    if args.rate > 0:
        # rate is aggregate; per-stream is rate/streams (datagrams = 1 stream)
        n = 1 if args.datagram else args.streams
        per_stream = args.rate / n
        mbps = args.object_size * args.rate * 8 / 1e6
        rate_s = f"{args.rate}/s total ({per_stream:.1f}/s per stream)"
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

    # AIOMOQT_TASK_DUMP=1 installs a SIGUSR1 handler that dumps every
    # asyncio task's stack to stderr. Useful for diagnosing hangs:
    # `kill -USR1 <pid>` while the bench is stuck. No-op when unset.
    from aiomoqt.utils.taskdump import install as _install_task_dump
    _install_task_dump()

    relay = parse_relay_url(args.url)
    print_banner(relay, args)

    client = MOQTClient(
        relay.host, relay.port,
        path=relay.path,
        use_quic=relay.use_quic,
        verify_tls=not args.insecure,
        supported_drafts=args.draft,
        debug=args.debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=args.cc_algo,
        tx_max_queued_bytes=args.max_queued_bytes,
        **({'tx_max_inflight_bytes':
            (None if args.max_inflight_bytes == 0
             else args.max_inflight_bytes)}
           if args.max_inflight_bytes is not None else {}),
    )

    print("  Connecting...")
    async with client.connect() as session:
        try:
            await session.client_session_init()

            if args.video:
                # Profile drives object size, GOP and fps; -r is fps.
                track = VideoTrack(
                    session,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    resolution=args.video,
                    fps=args.rate or 30,
                )
            else:
                track = PublishedTrack(
                    session,
                    namespace=args.namespace,
                    trackname=args.trackname,
                    object_size=args.object_size,
                    group_size=args.group_size,
                    num_subgroups=args.streams,
                    rate=args.rate,
                    forwarding=(ForwardingPreference.DATAGRAM
                                if args.datagram
                                else ForwardingPreference.SUBGROUP),
                )
            await track.publish(
                announce_namespace=(args.pub_ns or args.pub_both),
                publish_track=(not args.pub_ns or args.pub_both),
                forward=args.forward,
            )
            print(f"  Published '{track.fqtn}', waiting for subscriber...")

            if not await wait_cond_timeout(
                    track.wait_closed(), timeout=args.duration):
                print(f"\n  Duration {args.duration}s reached.")
        except Exception as e:
            print(f"  Error: {e}")

    print("  Done.")


def cli():
    """Console entry point (moq-pub-bench)."""
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\n  Interrupted.")


if __name__ == "__main__":
    try:
        args = parse_args()
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
