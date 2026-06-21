#!/usr/bin/env python3
"""aiomoqt-bench multi-sub — 1 publisher, N subscribers through a relay.

Measures relay connection density by spawning one publisher process
and N subscriber processes, each with its own QUIC connection.

Usage:
  # 100 subscribers, 1080p 30fps, through local moqx
  python -m aiomoqt.examples.multi_sub_bench \
    moqt://localhost:4433 --video 1080p -r 30 -n 100 -t 60

  # 50 subscribers, 1KB objects at 120fps
  python -m aiomoqt.examples.multi_sub_bench \
    moqt://localhost:4433 -s 1024 -r 120 -n 50 -t 30 -k
"""
import argparse
import asyncio
import multiprocessing
import os
import sys
import time

from aiomoqt.client import MOQTClient
from aiomoqt.track import PublishedTrack, VideoTrack, SubscribedTrack
from aiomoqt.utils import wait_cond_timeout
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.url import parse_relay_url


SUBSCRIBE_TIMEOUT = 30.0   # seconds to wait for SUBSCRIBE_OK / PUBLISH


def _quiet_logging(debug: bool) -> None:
    """Silence aiomoqt INFO chatter in the parent process so the
    setup header and results stay readable. `-d` opts in to INFO."""
    import logging
    lvl = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=lvl, force=True)
    set_log_level(lvl)


def parse_args():
    parser = argparse.ArgumentParser(
        add_help=False,
        description='aiomoqt-bench multi-sub — 1 pub, N subs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('relay', type=str,
                        help='Relay URL')
    parser.add_argument('-n', '--subscribers', type=int, default=10,
                        help='Number of subscribers (default: 10)')
    parser.add_argument('-s', '--object-size', type=int, default=1024,
                        help='Object size bytes (default: 1024)')
    parser.add_argument('-r', '--rate', type=float, default=30,
                        help='Aggregate objects/sec across all streams '
                             '(default: 30). Per-stream emit rate is '
                             'rate/streams. For VideoTrack, treated as fps.')
    parser.add_argument('-g', '--group-size', type=int, default=None,
                        help='Objects per group (default: rate)')
    parser.add_argument('-t', '--duration', type=int, default=30,
                        help='Duration seconds (default: 30)')
    parser.add_argument('-k', '--insecure', action='store_true',
                        help='Skip TLS verification')
    parser.add_argument('--draft', type=int, default=None,
                        help='MoQT draft version (default: negotiate)')
    parser.add_argument('--video', type=str, default=None,
                        choices=['240p', '270p', '360p', '480p',
                                 '720p', '1080p', '1440p', '4k'],
                        help='Video simulation mode')
    parser.add_argument('--namespace', type=str, default='aiomoqt',
                        help='Namespace (default: aiomoqt)')
    parser.add_argument('--trackname', type=str, default=None,
                        help='Explicit track name (default: '
                             'auto-discover on namespace)')
    parser.add_argument('-P', '--streams', type=int, default=1,
                        help='Parallel subgroup streams on publisher '
                             '(default: 1; matches pub_bench -P)')
    parser.add_argument('--no-pub', action='store_true',
                        help='Skip publisher, run subscribers only '
                             '(publisher must be running elsewhere on '
                             'the same namespace)')
    pub_mode = parser.add_mutually_exclusive_group()
    pub_mode.add_argument('--pub-ns', action='store_true',
                          help='Flow A: PUB_NS only (no PUBLISH). '
                               'Default is Flow B: bare PUBLISH.')
    pub_mode.add_argument('--pub-both', action='store_true',
                          help='Hybrid: PUB_NS + PUBLISH. Breaks on '
                               'CF d14 moq-rs.')
    parser.add_argument('-q', '--quic', '--use-quic', action='store_true',
                        dest='force_quic',
                        help='Raw QUIC')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='Verbose logging (default: WARN+)')
    parser.add_argument('--stagger', type=float, default=0.1,
                        help='Delay between subscriber spawns (default: 0.1s)')
    parser.add_argument('--startup', type=float, default=5.0,
                        help='Seconds to wait for publisher before spawning subs (default: 5)')
    parser.add_argument('--logdir', type=str, default=None,
                        help='Directory for per-process debug logs (pub.log, sub-N.log)')
    parser.add_argument('--cc-algo', type=str, default=None,
                        help='Congestion control algorithm '
                             '(bbr | bbr1 | newreno | cubic | dcubic | '
                             'prague | fast). Default: aiopquic default '
                             '(bbr1)')
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


def run_publisher(relay_url, namespace, trackname, args):
    """Publisher process."""
    import logging
    devnull = open(os.devnull, 'w')
    sys.stdout = devnull
    sys.stderr = devnull
    if args.logdir:
        os.makedirs(args.logdir, exist_ok=True)
        logfile = os.path.join(args.logdir, 'pub.log')
        lvl = logging.DEBUG if args.debug else logging.INFO
        logging.basicConfig(filename=logfile, force=True,
                            level=lvl,
                            format='%(asctime)s %(levelname)s %(message)s')
        set_log_level(lvl)
    else:
        logging.basicConfig(stream=devnull, force=True)
        set_log_level(logging.CRITICAL)

    async def _pub():
        relay = parse_relay_url(relay_url, force_quic=args.force_quic)
        client = MOQTClient(
            relay.host, relay.port,
            path=relay.path,
            use_quic=relay.use_quic,
            verify_tls=not args.insecure,
            draft_version=args.draft,
            congestion_control_algorithm=args.cc_algo,
            tx_max_queued_bytes=args.max_queued_bytes,
            **({'tx_max_inflight_bytes':
                (None if args.max_inflight_bytes == 0
                 else args.max_inflight_bytes)}
               if args.max_inflight_bytes is not None else {}),
        )
        async with client.connect() as session:
            await session.client_session_init()

            group_size = args.group_size or int(args.rate)
            if args.video:
                track = VideoTrack(
                    session, namespace, trackname,
                    resolution=args.video, fps=args.rate,
                )
            else:
                track = PublishedTrack(
                    session, namespace, trackname,
                    object_size=args.object_size,
                    group_size=group_size,
                    num_subgroups=args.streams,
                    rate=args.rate,
                )
            await track.publish(
                announce_namespace=(args.pub_ns or args.pub_both),
                publish_track=(not args.pub_ns or args.pub_both),
            )
            await wait_cond_timeout(
                track.wait_closed(), timeout=args.duration)

    try:
        asyncio.run(_pub())
    except KeyboardInterrupt:
        pass


def run_subscriber(sub_id, relay_url, namespace, trackname, args,
                   result_dict):
    """Subscriber process — results in shared dict."""
    import logging
    devnull = open(os.devnull, 'w')
    sys.stdout = devnull
    sys.stderr = devnull
    if args.logdir:
        os.makedirs(args.logdir, exist_ok=True)
        logfile = os.path.join(args.logdir, f'sub-{sub_id}.log')
        lvl = logging.DEBUG if args.debug else logging.INFO
        logging.basicConfig(filename=logfile, force=True,
                            level=lvl,
                            format='%(asctime)s %(levelname)s %(message)s')
        set_log_level(lvl)
    else:
        logging.basicConfig(stream=devnull, force=True)
        set_log_level(logging.CRITICAL)

    stats = {'id': sub_id, 'objects': 0, 'bytes': 0,
             'latencies': [], 'duration': 0, 'error': None}

    def on_object(msg, size_bytes, recv_time_us,
                  group_id=None, subgroup_id=None):
        # protocol.py passes microseconds (int(time.time() * 1_000_000));
        # MOQT_TIMESTAMP_EXT is in the same unit. Store latencies as
        # float ms. Bounds match sub_bench: -1s to +10min in µs.
        stats['objects'] += 1
        stats['bytes'] += size_bytes
        send_us = (msg.extensions.get(0x20)
                   if msg.extensions else None)
        if send_us is not None:
            raw_us = recv_time_us - send_us
            if -1_000_000 <= raw_us <= 600_000_000:
                stats['latencies'].append(raw_us / 1000.0)

    async def _sub():
        relay = parse_relay_url(relay_url, force_quic=args.force_quic)
        client = MOQTClient(
            relay.host, relay.port,
            path=relay.path,
            use_quic=relay.use_quic,
            verify_tls=not args.insecure,
            draft_version=args.draft,
            congestion_control_algorithm=args.cc_algo,
        )
        start = time.monotonic()
        try:
            async with client.connect() as session:
                await session.client_session_init()
                track = SubscribedTrack(
                    session, namespace,
                    trackname=trackname,
                    on_object=on_object,
                )
                await track.subscribe(timeout=SUBSCRIBE_TIMEOUT)
                if not await wait_cond_timeout(
                        track.wait_closed(), timeout=args.duration):
                    track.completed = True
                if not track.completed:
                    stats['error'] = 'StreamReset'
        except Exception as e:
            stats['error'] = str(e)
        stats['duration'] = time.monotonic() - start

    try:
        asyncio.run(_sub())
    except KeyboardInterrupt:
        pass

    # Store results
    lats = stats['latencies']
    result_dict[sub_id] = {
        'objects': stats['objects'],
        'bytes': stats['bytes'],
        'duration': stats['duration'],
        'error': stats['error'],
        'lat_avg': sum(lats) / len(lats) if lats else 0,
        'lat_p50': sorted(lats)[len(lats) // 2] if lats else 0,
        'lat_p99': sorted(lats)[int(len(lats) * 0.99)] if lats else 0,
    }


def main():
    args = parse_args()
    relay_url = args.relay

    # Default-quiet aiomoqt logging in the parent; workers handle their own.
    _quiet_logging(args.debug)

    # Subs always auto-discover unless --trackname is explicit.
    # The embedded publisher (when not --no-pub) needs a concrete name;
    # generate one if not supplied.
    sub_trackname = args.trackname
    if args.no_pub:
        pub_trackname = None
    else:
        import uuid
        pub_trackname = args.trackname or f"bench-{uuid.uuid4().hex[:4]}"

    print("─" * 60)
    print("  aiomoqt-bench multi-sub")
    print("─" * 60)
    print(f"  relay:        {relay_url}")
    print(f"  namespace:    {args.namespace}")
    print(f"  trackname:    {sub_trackname or '(auto-discover)'}")
    print(f"  subscribers:  {args.subscribers}")
    if args.video:
        print(f"  video:        {args.video} {args.rate}fps")
    else:
        print(f"  object size:  {args.object_size} B")
        print(f"  rate:         {args.rate}/s")
    print(f"  duration:     {args.duration}s")
    print(f"  stagger:      {args.stagger}s")
    print(f"  publisher:    "
          f"{'external (--no-pub)' if args.no_pub else 'embedded'}")
    print("─" * 60)

    # Shared dict for results
    manager = multiprocessing.Manager()
    results = manager.dict()

    pub_proc = None
    if not args.no_pub:
        print("\n  Starting publisher...")
        pub_proc = multiprocessing.Process(
            target=run_publisher,
            args=(relay_url, args.namespace, pub_trackname, args),
            daemon=True,
        )
        pub_proc.start()
        print(f"  Waiting {args.startup}s for publisher to register...")
        time.sleep(args.startup)
    else:
        print(f"\n  Skipping publisher (--no-pub). Subscribers will "
              f"auto-discover on namespace '{args.namespace}'.")

    # Spawn subscribers
    print(f"  Spawning {args.subscribers} subscribers "
          f"({args.stagger}s stagger)...")
    sub_procs = []
    for i in range(args.subscribers):
        p = multiprocessing.Process(
            target=run_subscriber,
            args=(i, relay_url, args.namespace, sub_trackname,
                  args, results),
            daemon=True,
        )
        p.start()
        sub_procs.append(p)
        if args.stagger > 0 and i < args.subscribers - 1:
            time.sleep(args.stagger)

    print(f"  All {args.subscribers} subscribers spawned, "
          f"running {args.duration}s...")

    # Wait with progress
    start = time.monotonic()
    deadline = start + args.duration + 15
    while time.monotonic() < deadline:
        alive = sum(1 for p in sub_procs if p.is_alive())
        done = len(results)
        elapsed = int(time.monotonic() - start)
        print(f"\r  [{elapsed}s] {done}/{args.subscribers} "
              f"complete, {alive} active   ", end="", flush=True)
        if done >= args.subscribers:
            break
        time.sleep(2.0)
    print()

    if pub_proc is not None:
        pub_proc.terminate()
        pub_proc.join(timeout=5)

    # Report
    print("\n" + "═" * 60)
    print("  aiomoqt-bench multi-sub results")
    print("═" * 60)

    total_objects = 0
    total_bytes = 0
    all_lats = []
    errors = 0
    resets = 0

    def _sub_expected(r):
        """Informational target — rate × subscriber's actual run time.
        Used for low-count flagging in the per-sub status, NOT for
        pass/fail (the 0.85 fudge was too sensitive to pub-startup
        offset and produced flaky 'reset' verdicts on healthy runs).
        Actual stream resets are surfaced as r['error']=='StreamReset'."""
        return int(args.rate * r['duration'])

    for i in range(args.subscribers):
        r = results.get(i)
        if r is None:
            errors += 1
            continue
        if r['error'] == 'StreamReset':
            resets += 1
        elif r['error']:
            errors += 1
        total_objects += r['objects']
        total_bytes += r['bytes']
        if r['lat_avg'] > 0:
            all_lats.append(r['lat_avg'])

    n = args.subscribers
    ok = n - errors - resets
    avg_objects = total_objects / n if n else 0
    avg_mbps = (total_bytes * 8) / (args.duration * 1e6) if n else 0
    per_sub_mbps = avg_mbps / n if n else 0

    print(f"  Subscribers:   {ok}/{n} ok"
          f"  ({errors} errors, {resets} resets)")
    print(f"  Total objects: {total_objects:,} "
          f"({avg_objects:,.0f} avg/sub)")
    print(f"  Total output:  {avg_mbps:.2f} Mbps "
          f"({per_sub_mbps:.2f} Mbps/sub)")
    if all_lats:
        avg_lat = sum(all_lats) / len(all_lats)
        print(f"  Avg latency:   {avg_lat:.1f} ms")

    # Per-subscriber detail
    print(f"\n  {'Sub':>4}  {'Objects':>8}  {'Mbps':>8}  "
          f"{'Latency':>8}  {'Status'}")
    print("  " + "─" * 52)
    for i in range(min(n, 20)):  # show first 20
        r = results.get(i)
        if r is None:
            print(f"  {i:>4}  {'???':>8}  {'???':>8}  "
                  f"{'???':>8}  NO RESULT")
            continue
        dur = r['duration'] or 1
        mbps = (r['bytes'] * 8) / (dur * 1e6)
        lat = f"{r['lat_avg']:.0f}ms" if r['lat_avg'] else "n/a"
        if r['error']:
            status = r['error']
        elif r['objects'] < int(_sub_expected(r) * 0.85):
            status = (f"low ({r['objects']}/"
                      f"{_sub_expected(r)})")
        else:
            status = "ok"
        print(f"  {i:>4}  {r['objects']:>8}  {mbps:>7.2f}"
              f"  {lat:>8}  {status}")
    if n > 20:
        print(f"  ... ({n - 20} more)")

    print("═" * 60)


if __name__ == "__main__":
    main()
