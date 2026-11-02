#!/usr/bin/env python3
"""Standalone MoQT publisher server — no relay needed.

Starts a QUIC/H3 server that generates data when a subscriber connects.
Pair with sub_bench.py in a separate shell for multi-process throughput
testing where each side gets its own CPU core.

Usage:
  # Shell 1: start publisher server
  python -m aiomoqt.tools.pub_server -s 16384 -g 10000 -P 4 -r 0
  python -m aiomoqt.tools.pub_server --video 1080p        # media profile

  # Shell 2: connect subscriber
  python -m aiomoqt.tools.sub_bench https://localhost:4434/ -k -t 30 -i 5
"""
import argparse
import asyncio
import signal
import sys

from aiomoqt.server import MOQTServer
from aiomoqt.types import (D18MessageType, MOQT_DEFAULT_DRAFTS, MOQTMessageType,
                           parse_draft_spec)
from aiomoqt.track import PublishedTrack, VideoTrack
from aiomoqt.types import ForwardingPreference
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.certs import find_cert, find_key
from aiomoqt.utils.hostinfo import cpu_summary
from aiomoqt.utils.logger import get_logger

logger = get_logger(__name__)


CERT = find_cert()
KEY = find_key(CERT)


def parse_args():
    parser = argparse.ArgumentParser(
        add_help=False,
        description='MoQT publisher server — standalone, no relay',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--bind', dest='host', type=str, default=None,
        help='Bind address (default: all interfaces, IPv4 and IPv6)')
    parser.add_argument(
        '-p', '--port', type=int, default=4434,
        help='Listen port (default: 4434)')
    _cli.add_publisher_media(parser)
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
        '-Q', '--quic', action='store_true', help='Serve raw QUIC')
    parser.add_argument(
        '-W', '--wt', action='store_true',
        help='Serve H3/WebTransport (default if neither given). With -Q, '
             'serves both on one port.')
    parser.add_argument(
        '--draft', type=parse_draft_spec, default=None,
        help='MoQT draft(s) to accept: one draft, or a comma list such '
             f'as 22,20,18 (default: {",".join(map(str, MOQT_DEFAULT_DRAFTS))})')
    parser.add_argument(
        '-N', '--namespace', type=str, default='aiomoqt',
        help='Track namespace (default: aiomoqt)')
    parser.add_argument(
        '-T', '--trackname', type=str, default='track',
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
        '--cc-algo', type=str, default='bbr',
        help='Congestion control (bbr | bbr1 | c4 | cubic | dcubic | '
             'fast | newreno | prague). Default: bbr')
    _cli.add_logging(parser)
    _cli.add_help(parser)
    _cli.add_timestamps(parser)
    _cli.add_fetch_history(parser)
    args = parser.parse_args()
    if not args.quic and not args.wt:
        args.wt = True
    _cli.check_video(parser, args)
    _cli.check_datagram(parser, args)
    return args


def _make_track(session, args):
    """The served track: a video profile under --video, else the -s/-g/-P/-r
    shape."""
    common = dict(namespace=args.namespace, trackname=args.trackname,
                  timestamps=args.timestamps,
                  fetch_history_bytes=args.fetch_history)
    if args.video:
        return VideoTrack(session, resolution=args.video, fps=args.rate,
                          gop_size=args.group_size, **common)
    return PublishedTrack(
        session,
        object_size=args.object_size,
        group_size=args.group_size,
        num_subgroups=args.streams,
        rate=args.rate,
        forwarding=(ForwardingPreference.DATAGRAM if args.datagram
                    else ForwardingPreference.SUBGROUP),
        **common)


async def _on_subscribe(session, msg, args):
    """Server-side subscribe handler: generate the served track."""
    track = _make_track(session, args)
    ok = session.subscribe_ok(
        request_msg=msg, track_properties=track._track_properties(session))
    track.track_alias = ok.track_alias
    track._generating = True
    logger.info(f"Subscriber connected, generating data "
                f"({args.object_size}B x {args.streams} streams)")
    try:
        await track.generate(session, ok.track_alias)
    except BaseException as e:
        import sys
        import traceback
        print(f"\n=== _on_subscribe: track.generate raised {type(e).__name__}: {e} ===",
              file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        print("=== end ===\n", file=sys.stderr, flush=True)
        raise


async def _on_subscribe_namespace(session, msg, args):
    """Discovery responder: a subscriber sent SUBSCRIBE_NAMESPACE to
    learn what we publish (it omitted --trackname).

    d14/d16 fuse discovery into this one request, so ack and announce.
    d18 reports only namespaces here; the tracks follow on
    SUBSCRIBE_TRACKS.
    """
    stream_id = session._bidi_streams.get(msg.request_id)
    session.subscribe_namespace_ok(msg, stream_id=stream_id)
    if session._profile.two_level_discovery:
        session.namespace(stream_id=stream_id)
        return
    await _announce_track(session, args)


async def _on_subscribe_tracks(session, msg, args):
    """d18 second-level discovery: the subscriber picked one of the
    namespaces we reported and is asking for its tracks."""
    session.subscribe_tracks_ok(msg)
    await _announce_track(session, args)


async def _announce_track(session, args):
    """Announce the track with PUBLISH (and PUBLISH_NAMESPACE under
    --pub-ns/--pub-both). The subscriber's await_publish learns the
    trackname and replies PUBLISH_OK(forward=1), which the track's own
    handler turns into generation. Keeps the track alive for the
    session via wait_closed()."""
    track = _make_track(session, args)
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
    _cli.apply_logging(args)

    # AIOMOQT_TASK_DUMP=1 installs a SIGUSR1 handler that dumps every
    # asyncio task's stack to stderr. Useful for diagnosing hangs:
    # `kill -USR1 <pid>` while the server is stuck. No-op when unset.
    from aiomoqt.utils.taskdump import install as _install_task_dump
    _install_task_dump()

    if not args.cert or not args.key:
        print("Error: TLS certificate required. "
              "Use --cert and --key, or place cert.pem/key.pem in certs/",
              file=sys.stderr)
        sys.exit(2)

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
        supported_drafts=args.draft,
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
    server.register_handler(
        D18MessageType.SUBSCRIBE_TRACKS,
        partial(_on_subscribe_tracks, args=args))
    dual = args.quic and args.wt
    quic_server = await (server.serve_dual() if dual else server.serve())

    transport = ("raw QUIC + H3/WebTransport" if dual
                 else "raw QUIC" if args.quic else "H3/WebTransport")
    rate_s = f"{args.rate}/s" if args.rate > 0 else "max"
    print(f"MoQT publisher server ready on {args.host}:{args.port}")
    print(f"  transport: {transport}")
    print(f"  namespace: {args.namespace}/{args.trackname}")
    if args.video:
        print(f"  objects:   video {args.video}, mean {args.object_size}B,"
              f" 1 stream")
    else:
        print(f"  objects:   {args.object_size}B x {args.streams} streams")
    print(f"  groups:    {args.group_size} objects/group")
    print(f"  rate:      {rate_s}")
    print(f"  cpu:       {cpu_summary()}")
    print("\nConnect subscriber (track-name discovery; -t on the sub):")
    # WT serves at "/" (path="/" above). -k: self-signed loopback cert.
    urls = []
    if args.quic:
        urls.append(f"moqt://{args.host}:{args.port}")
    if args.wt:
        urls.append(f"https://{args.host}:{args.port}/")
    draft = ("" if args.draft is None else " --draft " + ",".join(
        map(str, args.draft if isinstance(args.draft, list) else [args.draft])))
    for url in urls:
        print(f"  python -m aiomoqt.tools.sub_bench {url} "
              f"-N {args.namespace} -t 30 -i 5{draft} -k")

    # SIGTERM shuts down like Ctrl-C: peers get a CONNECTION_CLOSE.
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGTERM, asyncio.current_task().cancel)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await server.close_sessions()
        quic_server.close()
        print("\nServer stopped.")


def cli():
    """Console entry point (moq-pub-server)."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
