#!/usr/bin/env python3
import asyncio
import argparse
import logging

from aiomoqt.types import ParamType, MOQTException, parse_draft_spec
from aiomoqt.client import MOQTClient
from aiomoqt.utils.url import parse_relay_url
from aiomoqt.messages import SubscribeError, SubscribeNamespaceError
from aiomoqt.utils.logger import *

def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Client', add_help=False)
    parser.add_argument('url', metavar='URL',
                        help='Endpoint. moqt://host[:port][/path] '
                             '= raw QUIC; https://host[:port][/path] '
                             '= WebTransport; host[:port] = WebTransport.')
    parser.add_argument('-N', '--namespace', type=str, default="live/test", help='Track Namespace')
    parser.add_argument(
        '-T', '--trackname', type=str, default=None,
        help='Track Name (default: auto-discover via SUBSCRIBE_NAMESPACE)')
    parser.add_argument('-k', '--insecure', action='store_true',
                        help='Skip TLS certificate verification')
    parser.add_argument('--draft', type=parse_draft_spec, default=None,
                        help='MoQT draft version: 14, 16, or 18')
    parser.add_argument('-t', '--duration', type=int, default=30,
                        help='Duration seconds (default: 30)')
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
    parser.add_argument('--cc-algo', type=str, default=None,
                        help='Congestion control algorithm '
                             '(bbr | bbr1 | newreno | cubic | dcubic | '
                             'prague | fast). Default: aiopquic default (bbr1)')
    parser.add_argument(
        '-?', '--help', action='help',
        help='Show this help message and exit')
    args = parser.parse_args()
    # One positional URL replaces -h/--port/--path/-q: the scheme
    # selects the transport, exactly like the aiomoqt tools.
    _r = parse_relay_url(args.url)
    args.host, args.port = _r.host, _r.port
    args.path, args.use_quic = _r.path or "", _r.use_quic
    return args


async def main(host: str, port: int, path: str, namespace: str, track_name: str,
               use_quic: bool, debug: bool, cc_algo: str = None,
               insecure: bool = False, draft=None, duration: int = 30):
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
        keylog_filename=args.keylogfile,
        debug=debug,
        congestion_control_algorithm=cc_algo,
    )
    logger.info(f"MOQT app: join session connecting: {client}")
    try:
        async with client.connect() as session:
            try:
                response = await session.client_session_init()

                response = await session.subscribe_namespace(
                    namespace_prefix=namespace,
                    parameters={ParamType.AUTH_TOKEN: b"auth-token-123"},
                    wait_response=True
                )

                if isinstance(response, SubscribeNamespaceError):
                    logger.error(f"MOQT app: {response}")
                    raise MOQTException(response.error_code, response.reason)

                sub_response, fetch_response = await session.join(
                    namespace=namespace,
                    track_name=track_name,
                    parameters={
                        ParamType.MAX_CACHE_DURATION: 100,
                        ParamType.AUTH_TOKEN: b"auth-token-123",
                        ParamType.DELIVERY_TIMEOUT: 10,
                    },
                    joining_start=2,  # 2 groups before live edge
                    wait_response=True
                )

                if isinstance(sub_response, SubscribeError):
                    logger.error(f"MOQT app: {sub_response}")
                    raise MOQTException(sub_response.error_code, sub_response.reason)

                # process subscription - publisher will open stream and send data
                await session.async_closed()
                logger.info(f"MOQT app: exiting client session")
            except MOQTException as e:
                logger.error(f"MOQT app: session exception: {e}")
                session.close(e.error_code, e.reason_phrase)
            except Exception as e:
                logger.error(f"MOQT app: connection failed: {e}")
    except Exception as e:
        logger.error(f"MOQT app: connection failed: {e}")

    logger.info(f"MOQT app: join session closed: {class_name(client)}")

if __name__ == "__main__":
    try:
        args = parse_args()
        asyncio.run(main(
            host=args.host,
            port=args.port,
            path=args.path,
            namespace=args.namespace,
            track_name=args.trackname,
            use_quic=args.use_quic,
            debug=args.debug,
            cc_algo=args.cc_algo,
            insecure=args.insecure,
            draft=args.draft,
            duration=args.duration,
        ), debug=args.debug)

    except KeyboardInterrupt:
        pass
