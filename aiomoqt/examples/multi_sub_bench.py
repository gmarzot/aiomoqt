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
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.url import parse_relay_url


def _quiet_logging(debug: bool) -> None:
    """Silence aiomoqt INFO chatter in the parent process so the
    setup header and results stay readable. `-d` opts in to INFO."""
    import logging
    lvl = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=lvl, force=True)
    set_log_level(lvl)


def parse_args():
    parser = argparse.ArgumentParser(
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
                        help='Objects/sec (default: 30)')
    parser.add_argument('-g', '--group-size', type=int, default=None,
                        help='Objects per group (default: rate)')
    parser.add_argument('-t', '--duration', type=int, default=30,
                        help='Duration seconds (default: 30)')
    parser.add_argument('-k', '--insecure', action='store_true',
                        help='Skip TLS verification')
    parser.add_argument('--draft', type=int, default=16,
                        help='MoQT draft version (default: 16)')
    parser.add_argument('--video', type=str, default=None,
                        choices=['240p', '270p', '360p', '480p',
                                 '720p', '1080p', '1440p', '4k'],
                        help='Video simulation mode')
    parser.add_argument('--namespace', type=str, default='aiomoqt',
                        help='Namespace (default: aiomoqt)')
    parser.add_argument('--trackname', type=str, default=None,
                        help='Explicit track name (default: '
                             'auto-discover on namespace)')
    parser.add_argument('-P', '--no-pub', action='store_true',
                        help='Skip publisher, run subscribers only '
                             '(publisher must be running elsewhere on '
                             'the same namespace)')
    parser.add_argument('-N', '--no-sub-ns', action='store_true',
                        help='Skip subscribe_namespace; send direct '
                             'SUBSCRIBE (requires --trackname)')
    parser.add_argument('-Q', '--force-quic', action='store_true',
                        help='Force raw QUIC')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='Verbose logging (default: WARN+)')
    parser.add_argument('--stagger', type=float, default=0.1,
                        help='Delay between subscriber spawns (default: 0.1s)')
    parser.add_argument('--startup', type=float, default=5.0,
                        help='Seconds to wait for publisher before spawning subs (default: 5)')
    parser.add_argument('--logdir', type=str, default=None,
                        help='Directory for per-process debug logs (pub.log, sub-N.log)')
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
            endpoint=relay.endpoint,
            use_quic=relay.use_quic,
            verify_tls=not args.insecure,
            draft_version=args.draft,
        )
        async with client.connect() as session:
            await session.client_session_init()

            group_size = args.group_size or int(args.rate)
            if args.video:
                track = VideoTrack(
                    session, namespace, trackname,
                    resolution=args.video, fps=args.rate,
                    draft=args.draft,
                )
            else:
                track = PublishedTrack(
                    session, namespace, trackname,
                    object_size=args.object_size,
                    group_size=group_size,
                    rate=args.rate,
                    draft=args.draft,
                )
            await track.publish()
            await track.wait_closed(timeout=args.duration + 10)

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

    def on_object(msg, size_bytes, recv_time_ms,
                  group_id=None, subgroup_id=None):
        stats['objects'] += 1
        stats['bytes'] += size_bytes
        send_ms = (msg.extensions.get(0x20)
                   if msg.extensions else None)
        if send_ms and abs(recv_time_ms - send_ms) < 60000:
            stats['latencies'].append(recv_time_ms - send_ms)

    async def _sub():
        relay = parse_relay_url(relay_url, force_quic=args.force_quic)
        client = MOQTClient(
            relay.host, relay.port,
            endpoint=relay.endpoint,
            use_quic=relay.use_quic,
            verify_tls=not args.insecure,
            draft_version=args.draft,
        )
        start = time.monotonic()
        try:
            async with client.connect() as session:
                await session.client_session_init()
                track = SubscribedTrack(
                    session, namespace,
                    trackname=trackname,
                    draft=args.draft,
                    on_object=on_object,
                )
                await track.subscribe(
                    timeout=30.0,
                    use_namespace=not args.no_sub_ns,
                )
                await track.wait_closed(timeout=args.duration)
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

    if args.no_sub_ns and not args.trackname:
        print("error: --no-sub-ns (-N) requires --trackname",
              file=sys.stderr)
        sys.exit(2)

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
        """Expected objects based on subscriber's actual run time."""
        return int(args.rate * r['duration'] * 0.85)

    for i in range(args.subscribers):
        r = results.get(i)
        if r is None:
            errors += 1
            continue
        if r['error'] == 'StreamReset':
            resets += 1
        elif r['error']:
            errors += 1
        elif r['objects'] < _sub_expected(r):
            resets += 1
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
        elif r['objects'] < _sub_expected(r):
            status = (f"reset ({r['objects']}/"
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
