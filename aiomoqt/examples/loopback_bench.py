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


def _rss_mb() -> float:
    """Current RSS in MB (Linux /proc; ru_maxrss high-water fallback)."""
    import os
    try:
        with open('/proc/self/statm') as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf('SC_PAGESIZE') / 1e6
    except Exception:
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3
        except Exception:
            return 0.0


class _BackpressureMonitor:
    """Per-interval sampler that localizes where bytes pool between
    producer encode and subscriber delivery.

    pub_txq: sum of the publisher session's per-stream sc->tx ring
        depths (bytes encoded but not yet pulled by the picoquic
        worker) and the count of streams holding bytes. Growth here
        means producer run-ahead past the wire drain rate.
    sub_chain: sum of the subscriber session's StreamChain capacities
        (bytes received but not yet parsed past an object boundary)
        and the live parser-state count. Growth here means the parse
        pipeline is the bottleneck.
    sc/chk/rxq: process-wide aiopquic counters (loopback runs both
        sides in one process, so these aggregate pub+sub).
    drop/fc/scD: per-interval deltas summed across both transports.
    """

    def __init__(self, interval: float):
        self.interval = interval
        self.pub_session = None
        self.sub_session = None
        self._prev = {}

    @staticmethod
    def _tx_queued(session):
        quic = getattr(session, '_quic', None)
        if quic is None:
            return 0, 0
        sids = (getattr(quic, '_stream_tx_ctxs', None)
                or getattr(quic, '_stream_ctxs', None))
        if not sids:
            return 0, 0
        total = n = 0
        for sid in list(sids):
            try:
                used = quic.stream_tx_buf_used(sid)
            except Exception:
                continue
            if used:
                total += used
                n += 1
        return total, n

    @staticmethod
    def _chain_backlog(session):
        ds = getattr(session, '_data_streams', None)
        if not ds:
            return 0, 0
        total = 0
        for state in list(ds.values()):
            try:
                total += state.chain.capacity
            except Exception:
                continue
        return total, len(ds)

    @staticmethod
    def _counters(session):
        quic = getattr(session, '_quic', None)
        t = getattr(quic, '_transport', None)
        if t is None:
            return {}
        try:
            c = t.counters
            return c() if callable(c) else c
        except Exception:
            return {}

    def sample(self):
        pub_q = pub_n = sub_q = sub_n = 0
        if self.pub_session is not None:
            pub_q, pub_n = self._tx_queued(self.pub_session)
        if self.sub_session is not None:
            sub_q, sub_n = self._chain_backlog(self.sub_session)
        c_pub = self._counters(self.pub_session) if self.pub_session else {}
        c_sub = self._counters(self.sub_session) if self.sub_session else {}
        g = c_pub or c_sub  # process-wide totals identical either side
        drops = (c_pub.get('rx_event_drops', 0)
                 + c_sub.get('rx_event_drops', 0))
        fc_p = (c_pub.get('fc_credit_pushed', 0)
                + c_sub.get('fc_credit_pushed', 0))
        fc_h = (c_pub.get('fc_credit_handled', 0)
                + c_sub.get('fc_credit_handled', 0))
        created = g.get('sc_created_total', 0)
        destroyed = g.get('sc_destroyed_total', 0)
        prev = self._prev
        print(f"  [mon] pub_txq={pub_q/1e6:7.1f}MB/{pub_n:<3d}"
              f" sub_chain={sub_q/1e6:8.1f}MB/{sub_n:<4d}"
              f" sc={g.get('sc_alive_total', 0):<5d}"
              f" chk={g.get('chunks_alive_total', 0):<6d}"
              f" rxq={g.get('sc_rx_bytes_in_flight', 0)/1e6:6.1f}MB"
              f" drop=+{drops - prev.get('drops', 0):<3d}"
              f" fc=+{fc_p - prev.get('fc_p', 0)}"
              f"/+{fc_h - prev.get('fc_h', 0)}"
              f" scD=+{created - prev.get('created', 0)}"
              f"/-{destroyed - prev.get('destroyed', 0)}"
              f" rss={_rss_mb():.0f}MB")
        self._prev = dict(drops=drops, fc_p=fc_p, fc_h=fc_h,
                          created=created, destroyed=destroyed)

    async def run(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                self.sample()
            except Exception as e:
                print(f"  [mon] sample error: {e}")


def parse_args():
    parser = argparse.ArgumentParser(
        add_help=False,
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
        '-q', '--quic', '--use-quic', action='store_true',
        help='Use raw QUIC instead of WebTransport (default: WT)')
    parser.add_argument(
        '--cc-algo', type=str, default=None,
        help='Congestion control algorithm '
             '(bbr | bbr1 | newreno | cubic | dcubic | prague | fast). '
             'Default: aiopquic default (bbr1)')
    parser.add_argument(
        '--max-inflight-bytes', type=int, default=None,
        help='Per-stream TX budget (aiomoqt tx_max_inflight_bytes): '
             'producer pauses while one stream\'s un-transmitted bytes '
             'exceed this. Default: aiomoqt default (1 MiB). '
             'Pass 0 to disable.')
    parser.add_argument(
        '--max-queued-bytes', type=int, default=None,
        help='Aggregate publisher byte budget across ALL streams '
             '(QuicConfiguration.tx_max_queued_bytes): producer parks '
             'at stream rollover while total un-transmitted TX bytes '
             'exceed this. Steady-state latency ~ value / throughput. '
             'Default: aiopquic default (4 MiB). Pass 0 to disable.')
    parser.add_argument(
        '-?', '--help', action='help',
        help='Show this help message and exit')
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


async def _on_subscribe(session, msg, args, mon=None):
    """Server-side subscribe handler using PublishedTrack."""
    if mon is not None:
        mon.pub_session = session
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


async def run_server(args, mon=None):
    """Run a MOQTServer that generates data when subscribers connect."""
    from functools import partial

    server = MOQTServer(
        host="localhost", port=args.port,
        certificate=args.cert, private_key=args.key,
        path="/",
        use_quic=args.quic,
        congestion_control_algorithm=args.cc_algo,
        # None = honor protocol default (16 MB); 0 = opt out.
        **({'tx_max_inflight_bytes':
            (None if args.max_inflight_bytes == 0
             else args.max_inflight_bytes)}
           if args.max_inflight_bytes is not None else {}),
        **({'tx_max_queued_bytes': args.max_queued_bytes}
           if args.max_queued_bytes is not None else {}),
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE,
        partial(_on_subscribe, args=args, mon=mon))
    return await server.serve()


async def run_subscriber(args, stats, mon=None):
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
            if mon is not None:
                mon.sub_session = session
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

    # AIOMOQT_TRACEMALLOC=1 enables Python-level allocation tracking.
    # Baseline snap is taken 2s after subscriber connects; end snap is
    # taken right before cleanup. The diff (end - baseline) localizes
    # what GREW during steady-state operation — the sub-side retention
    # signature is exactly this case.
    import os as _os
    import tracemalloc as _tm
    _trace_enabled = _os.environ.get("AIOMOQT_TRACEMALLOC") == "1"
    if _trace_enabled:
        _tm.start(25)

    stats = BenchStats(report_interval=args.interval)
    print_banner(args)

    if not args.cert or not args.key:
        print("  Error: TLS certificate required. "
              "Use --cert and --key,")
        print("  or place cert.pem/key.pem in <project>/certs/")
        return

    # Start server. AIOMOQT_MON=1 enables the per-interval
    # backpressure sampler line (diagnostic; off by default).
    print("  Starting server...")
    mon = None
    mon_task = None
    if _os.environ.get("AIOMOQT_MON") == "1":
        mon = _BackpressureMonitor(args.interval)
        mon_task = asyncio.create_task(mon.run())
    quic_server = await run_server(args, mon=mon)

    # Give server a moment
    await asyncio.sleep(0.5)

    # Baseline tracemalloc snap 2 s after subscriber connects.
    _baseline_snap = [None]
    if _trace_enabled:
        async def _take_baseline():
            await asyncio.sleep(2.0)
            _baseline_snap[0] = _tm.take_snapshot()
        asyncio.create_task(_take_baseline())

    # Run subscriber
    print("  Connecting subscriber...")
    await run_subscriber(args, stats, mon=mon)
    if mon_task is not None:
        mon_task.cancel()
        # Final sample after the run so end-state pooling is visible.
        mon.sample()

    # End tracemalloc snap before cleanup; diff against baseline.
    if _trace_enabled and _baseline_snap[0] is not None:
        end_snap = _tm.take_snapshot()
        diff = end_snap.compare_to(_baseline_snap[0], 'lineno')
        print()
        print("=" * 70)
        print("  tracemalloc: top 30 growers (baseline @ +2s → end)")
        print("=" * 70)
        for s in diff[:30]:
            frame = s.traceback[0] if s.traceback else None
            loc = (f"{frame.filename.split('/')[-1]}:{frame.lineno}"
                   if frame else "<no frame>")
            sign = "+" if s.size_diff >= 0 else ""
            print(f"  {sign}{s.size_diff/1024/1024:7.2f} MB  "
                  f"{sign}{s.count_diff:7d} blocks  {loc}")
        print("=" * 70)

    # Cleanup
    quic_server.close()
    # Give worker thread + close walker a tick to run before we sample.
    await asyncio.sleep(0.5)
    stats.print_summary()

    # AIOMOQT_TASK_DUMP=1 also enables a final post-shutdown counter dump
    # so we can tell whether the close-walker swept leaked WT links
    # (deferred-destroy pattern) or they truly leak.
    import os as _os2
    if _os2.environ.get("AIOMOQT_TASK_DUMP") == "1":
        import sys as _sys2
        try:
            from aiopquic._binding._transport import dump_all_counters
            print("\n=== aiopquic counter-dump (post-shutdown) ===",
                  file=_sys2.stderr)
            dump_all_counters(file=_sys2.stderr)
        except Exception as _e2:
            print(f"(post-shutdown counter dump failed: {_e2})",
                  file=_sys2.stderr)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
