#!/usr/bin/env python3
import argparse
import asyncio
import logging
import uuid

from aiomoqt.types import ForwardingPreference, parse_draft_spec
from aiomoqt.client import MOQTClient
from aiomoqt.track import PublishedTrack, VideoTrack
from aiomoqt.utils import class_name, get_logger, set_log_level, wait_cond_timeout
from aiomoqt.utils.url import parse_relay_url

GROUP_SIZE = 30


def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Client', add_help=False)
    parser.add_argument('url', metavar='URL',
                        help='Endpoint. moqt://host[:port][/path] '
                             '= raw QUIC; https://host[:port][/path] '
                             '= WebTransport; host[:port] = WebTransport.')
    parser.add_argument('-N', '--namespace', type=str, default='test', help='Namespace')
    parser.add_argument(
        '-T', '--trackname', type=str,
        default=f"track-{uuid.uuid4().hex[:4]}",
        help='Track name (default: track-<rand4> — relay caches '
             'reject differing payload bytes for the same trackname '
             'across runs, so the random suffix avoids spurious '
             'cache mismatches)')
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quic-debug', action='store_true', help='Enable quic debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
    parser.add_argument('-k', '--insecure', action='store_true', help='Skip TLS certificate verification')
    parser.add_argument('--auth-token', type=str, default=None, help='Auth token')
    parser.add_argument('--draft', type=parse_draft_spec, default=None, help='MoQT draft version: 14, 16, or 18')
    parser.add_argument('-P', '--streams', type=int, default=1, help='Parallel subgroup streams (default: 1)')
    parser.add_argument('-s', '--object-size', type=int, default=1024, help='Object payload size bytes (default: 1024)')
    parser.add_argument('-r', '--rate', type=float, default=30, help='Frames per second (default: 30)')
    parser.add_argument('-t', '--duration', type=int, default=120, help='Duration in seconds (default: 120)')
    parser.add_argument('--video', type=str, default=None, metavar='RES',
                        choices=['720p', '1080p', '1440p', '4k'],
                        help='Video simulation mode with I/B/P frames (720p, 1080p, 1440p, 4k)')
    parser.add_argument('--gop-pattern', type=str, default='ibp',
                        choices=['ibp', 'ip', 'ionly'],
                        help='GOP pattern (default: ibp)')
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
    parser.add_argument(
        '--datagram', action='store_true',
        help='ObjectDatagrams instead of subgroup streams (raw QUIC '
             'only; object must fit one packet; not valid with --video)')
    args = parser.parse_args()
    # One positional URL replaces -h/--port/--path/-q: the scheme
    # selects the transport, exactly like the aiomoqt tools.
    _r = parse_relay_url(args.url)
    args.host, args.port = _r.host, _r.port
    args.path, args.use_quic = _r.path or "", _r.use_quic
    if args.datagram and args.video:
        parser.error("--datagram does not support --video yet")
    if args.datagram and not args.use_quic:
        parser.error("--datagram requires a moqt:// URL (raw QUIC); "
                     "WT datagram TX is not wired yet")
    return args


async def main(host: str, port: int, path: str, namespace: str, trackname: str,
               debug: bool, use_quic: bool, quic_debug: bool,
               datagram: bool = False,
               insecure: bool = False, auth_token: str = None, draft: int = None,
               streams: int = 1, object_size: int = 1024, rate: float = 30,
               duration: int = 120, video: str = None, gop_pattern: str = 'ibp',
               cc_algo: str = None, max_queued_bytes: int = None,
               max_inflight_bytes: int = None):
    log_level = logging.DEBUG if debug else logging.INFO
    set_log_level(log_level)
    logger = get_logger(__name__)

    client = MOQTClient(
        host,
        port,
        path=path,
        use_quic=use_quic,
        verify_tls=not insecure,
        supported_drafts=draft,
        debug=debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=cc_algo,
        tx_max_queued_bytes=max_queued_bytes,
        **({'tx_max_inflight_bytes':
            (None if max_inflight_bytes == 0 else max_inflight_bytes)}
           if max_inflight_bytes is not None else {}),
    )

    auth = auth_token.encode() if auth_token else b""

    logger.info(f"MOQT app: publish session connecting: {client}")
    async with client.connect() as session:
        try:
            await session.client_session_init()

            if video:
                track = VideoTrack(
                    session,
                    namespace=namespace,
                    trackname=trackname,
                    resolution=video,
                    fps=rate,
                    gop_pattern=gop_pattern,
                    auth_token=auth,
                )
            else:
                track = PublishedTrack(
                    session,
                    namespace=namespace,
                    trackname=trackname,
                    object_size=object_size,
                    group_size=GROUP_SIZE,
                    num_subgroups=streams,
                    rate=rate,
                    auth_token=auth,
                    forwarding=(ForwardingPreference.DATAGRAM if datagram
                                else ForwardingPreference.SUBGROUP),
                )
            await track.publish()
            logger.info(f"MOQT app: published {track.fqtn}")

            await wait_cond_timeout(track.wait_closed(), timeout=duration)
        except Exception as e:
            logger.error(f"MOQT session exception: {e}")

    logger.info(f"MOQT app: publish session closed: {class_name(client)}")


if __name__ == "__main__":

    try:
        args = parse_args()
        asyncio.run(main(
            host=args.host,
            port=args.port,
            path=args.path,
            use_quic=args.use_quic,
            namespace=args.namespace,
            trackname=args.trackname,
            debug=args.debug,
            datagram=args.datagram,
            quic_debug=args.quic_debug,
            insecure=args.insecure,
            auth_token=args.auth_token,
            draft=args.draft,
            streams=args.streams,
            object_size=args.object_size,
            rate=args.rate,
            duration=args.duration,
            video=args.video,
            gop_pattern=args.gop_pattern,
            cc_algo=args.cc_algo,
            max_queued_bytes=args.max_queued_bytes,
            max_inflight_bytes=args.max_inflight_bytes,
        ))

    except KeyboardInterrupt:
        pass
