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
from aiomoqt.types import MOQTMessageType, MOQT_VERSION_DRAFT16, parse_draft_spec
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
        add_help=False,
        description='MoQT publisher server — standalone, no relay',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '-h', '--host', type=str, default='localhost',
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
        help='Aggregate objects/sec across all streams (0=max, '
             'default: max). Per-stream emit rate is rate/streams.')
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
             'exceed this. Steady-state latency ~ value / throughput '
             '(4 MiB ~ 8 ms @ 3 Gbps). Default: aiopquic default '
             '(4 MiB). Pass 0 to disable.')
    parser.add_argument(
        '-q', '--quic', '--use-quic', action='store_true',
        help='Serve raw QUIC (aiopquic) instead of H3/WebTransport')
    parser.add_argument(
        '--draft', type=parse_draft_spec, default=16,
        help='MoQT draft version when --quic (default: 16)')
    parser.add_argument(
        '-n', '--namespace', type=str, default='aiomoqt',
        help='Track namespace (default: aiomoqt)')
    parser.add_argument(
        '--trackname', type=str, default='track',
        help='Track name (default: track)')
    parser.add_argument(
        '--pub-ns', action='store_true',
        help='On discovery (SUBSCRIBE_NAMESPACE) announce the namespace '
             '(PUBLISH_NAMESPACE) only, not the track')
    parser.add_argument(
        '--pub-both', action='store_true',
        help='On discovery announce BOTH namespace (PUBLISH_NAMESPACE) '
             'and track (PUBLISH). Default: track (PUBLISH) only')
    parser.add_argument(
        '--cert', type=str, default=CERT,
        help='TLS certificate file')
    parser.add_argument(
        '--key', type=str, default=KEY,
        help='TLS key file')
    parser.add_argument(
        '-d', '--debug', action='store_true',
        help='Enable debug output')
    parser.add_argument(
        '--cc-algo', type=str, default=None,
        help='Congestion control algorithm '
             '(bbr | bbr1 | newreno | cubic | dcubic | prague | fast). '
             'Default: aiopquic default (bbr1)')
    parser.add_argument(
        '-?', '--help', action='help',
        help='Show this help message and exit')
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
    try:
        await track.generate(session, ok.track_alias)
    except BaseException as e:
        import sys, traceback
        print(f"\n=== _on_subscribe: track.generate raised {type(e).__name__}: {e} ===",
              file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        print("=== end ===\n", file=sys.stderr, flush=True)
        raise


async def _on_subscribe_namespace(session, msg, args):
    """Discovery responder: a subscriber sent SUBSCRIBE_NAMESPACE to
    learn what we publish (it omitted --trackname). Ack it, then
    announce the track with PUBLISH (and PUBLISH_NAMESPACE under
    --pub-ns/--pub-both). The subscriber's await_publish learns the
    trackname and replies PUBLISH_OK(forward=1), which the track's own
    handler turns into generation. Keeps the track alive for the
    session via wait_closed()."""
    stream_id = session._bidi_streams.get(msg.request_id)
    session.subscribe_namespace_ok(msg, stream_id=stream_id)
    track = PublishedTrack(
        session,
        namespace=args.namespace,
        trackname=args.trackname,
        object_size=args.object_size,
        group_size=args.group_size,
        num_subgroups=args.streams,
        rate=args.rate,
    )
    await track.publish(
        announce_namespace=(args.pub_ns or args.pub_both),
        publish_track=(not args.pub_ns or args.pub_both),
        forward=0,
    )
    logger.info(f"Discovery: announced '{track.fqtn}' to subscriber")
    try:
        await track.wait_closed()
    except asyncio.CancelledError:
        pass


async def main():
    from functools import partial

    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)

    # AIOMOQT_TASK_DUMP=1 installs a SIGUSR1 handler that dumps every
    # asyncio task's stack to stderr. Useful for diagnosing hangs:
    # `kill -USR1 <pid>` while the server is stuck. No-op when unset.
    from aiomoqt.utils.taskdump import install as _install_task_dump
    _install_task_dump()

    if not args.cert or not args.key:
        print("Error: TLS certificate required. "
              "Use --cert and --key, or place cert.pem/key.pem in certs/")
        return

    # Public API takes the draft NUMBER (14, 16, ...); MOQTServer
    # normalizes to the wire-form internally. The MoQT session layer
    # needs the draft for either transport (raw QUIC or WT) to pick
    # the right message-encoding rules; only the QUIC ALPN derivation
    # differs.
    # CLI semantics: None = honor protocol-layer default (16 MB);
    # 0 = explicit opt-out (unbounded); >0 = explicit value.
    if args.max_inflight_bytes is None:
        _tx_max = ...  # let MOQTServer/MOQTPeer apply DEFAULT
    elif args.max_inflight_bytes == 0:
        _tx_max = None  # explicit opt-out
    else:
        _tx_max = args.max_inflight_bytes
    _server_kwargs = dict(
        host=args.host, port=args.port,
        certificate=args.cert, private_key=args.key,
        path="/",
        use_quic=args.quic,
        draft_version=args.draft,
        congestion_control_algorithm=args.cc_algo,
    )
    if _tx_max is not ...:
        _server_kwargs['tx_max_inflight_bytes'] = _tx_max
    if args.max_queued_bytes is not None:
        _server_kwargs['tx_max_queued_bytes'] = args.max_queued_bytes
    server = MOQTServer(**_server_kwargs)
    server.register_handler(
        MOQTMessageType.SUBSCRIBE,
        partial(_on_subscribe, args=args))
    server.register_handler(
        MOQTMessageType.SUBSCRIBE_NAMESPACE,
        partial(_on_subscribe_namespace, args=args))
    quic_server = await server.serve()

    transport = "raw QUIC" if args.quic else "H3/WebTransport"
    rate_s = f"{args.rate}/s" if args.rate > 0 else "max"
    print(f"MoQT publisher server ready on {args.host}:{args.port}")
    print(f"  transport: {transport}")
    print(f"  namespace: {args.namespace}/{args.trackname}")
    print(f"  objects:   {args.object_size}B x {args.streams} streams")
    print(f"  groups:    {args.group_size} objects/group")
    print(f"  rate:      {rate_s}")
    print("\nConnect subscriber (track-name discovery; -t on the sub):")
    # URL carries the path: raw QUIC has none; WT serves at "/" (the
    # MOQTServer(path="/") above) — not "/moq". sub_bench needs -k for
    # the self-signed loopback cert. With discovery, --trackname is
    # optional; -n must match this server's namespace.
    if args.quic:
        url = f"moqt://{args.host}:{args.port}"
    else:
        url = f"https://{args.host}:{args.port}/"
    print(f"  python -m aiomoqt.examples.sub_bench {url} "
          f"-n {args.namespace} -t 30 -i 5 --draft {args.draft} -k")

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
