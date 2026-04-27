#!/usr/bin/env python3
import asyncio
import argparse
import logging

from aiomoqt.types import ParamType, MOQTException
from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubscribeError, SubscribeNamespaceError
from aiomoqt.utils.logger import *

def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Client')
    parser.add_argument('--host', type=str, default='localhost', help='Host to connect to')
    parser.add_argument('--port', type=int, default=443, help='Port to connect to')
    parser.add_argument('--namespace', type=str, default="live/test", help='Track Namespace')
    parser.add_argument('--trackname', type=str, default="track", help='Track Name')
    parser.add_argument('--path', type=str, default="", help='MOQT WT path (default: "/")')
    parser.add_argument('--use-quic', action='store_true', help='Enable QUIC transport')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
    return parser.parse_args()


async def main(host: str, port: int, path: str, namespace: str, track_name: str,
               use_quic: bool, debug: bool):
    log_level = logging.DEBUG if debug else logging.INFO
    set_log_level(log_level)
    logger = get_logger(__name__)

    client = MOQTClient(
        host,
        port,
        path=path,
        use_quic=use_quic,
        keylog_filename=args.keylogfile,
        debug=debug
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
            debug=args.debug
        ), debug=args.debug)

    except KeyboardInterrupt:
        pass
