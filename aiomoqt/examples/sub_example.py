#!/usr/bin/env python3

import asyncio
import argparse
import logging

from aiomoqt.types import ParamType, MOQTException, MOQTRequestError
from aiomoqt.client import MOQTClient
from aiomoqt.utils.logger import *


def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Client')
    parser.add_argument('--host', type=str, default='localhost', help='Host to connect to')
    parser.add_argument('--port', type=int, default=4433, help='Port to connect to')
    parser.add_argument('--namespace', type=str, default="live/test", help='Track Namespace')
    parser.add_argument('--trackname', type=str, default="track", help='Track Name')
    parser.add_argument('--endpoint', type=str, default="moq", help='MOQT WT endpoint')
    parser.add_argument('--use-quic', action='store_true', help='Enable QUIC transport')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quic-debug', action='store_true',  help='Enable quic debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')

    return parser.parse_args()

async def main(host: str, port: int, endpoint: str, namespace: str, track_name: str,
               use_quic: bool, debug: bool, quic_debug: bool):
    log_level = logging.DEBUG if debug else logging.INFO
    set_log_level(log_level)
    logger = get_logger(__name__)

    client = MOQTClient(
        host,
        port,
        endpoint=endpoint,
        use_quic=use_quic,
        debug=debug,
        quic_debug=quic_debug,
        keylog_filename=args.keylogfile,
    )
    logger.info(f"MOQT app: subscribe session connecting: {client}")
    try:
        async with client.connect() as session:
            try:
                response = await session.client_session_init()

                await session.subscribe_namespace(
                    namespace_prefix=namespace,
                    parameters={ParamType.AUTH_TOKEN: b"auth-token-123"},
                    wait_response=True
                )

                await session.subscribe(
                    namespace=namespace,
                    track_name=track_name,
                    parameters={
                        ParamType.MAX_CACHE_DURATION: 100,
                        ParamType.AUTH_TOKEN: b"auth-token-123",
                        ParamType.DELIVERY_TIMEOUT: 10,
                    },
                    wait_response=True
                )

                # process subscription - publisher will open stream and send data
                await session.async_closed()
                logger.info(f"MOQT app: exiting client session")

            except MOQTRequestError as e:
                logger.error(f"MOQT app: request error: {e}")
                session.close()
            except MOQTException as e:
                logger.error(f"MOQT app: session exception: {e}")
                session.close(e.error_code, e.reason_phrase)
            except Exception as e:
                logger.error(f"MOQT app: connection failed: {e}")
    except Exception as e:
        logger.error(f"MOQT app: connection failed: {e}")

    logger.info(f"MOQT app: subscribe session closed: {class_name(client)}")

if __name__ == "__main__":
    try:
        args = parse_args()
        asyncio.run(main(
            host=args.host,
            port=args.port,
            endpoint=args.endpoint,
            namespace=args.namespace,
            track_name=args.trackname,
            use_quic=args.use_quic,
            debug=args.debug,
            quic_debug=args.quic_debug,
        ), debug=args.debug)

    except KeyboardInterrupt:
        pass
