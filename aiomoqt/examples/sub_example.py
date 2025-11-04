#!/usr/bin/env python3

import asyncio
import argparse
import logging

from qh3.h3.connection import H3_ALPN
from aiomoqt.types import ParamType, MOQTException
from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTException
from aiomoqt.messages.subscribe import SubscribeError
from aiomoqt.messages.namespace import SubscribeAnnouncesError 
from aiomoqt.utils.logger import *


def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Client')
    parser.add_argument('--host', type=str, default='localhost', help='Host to connect to')
    parser.add_argument('--port', type=int, default=4433, help='Port to connect to')
    parser.add_argument('--namespace', type=str, default="live/test", help='Track Namespace')
    parser.add_argument('--trackname', type=str, default="track", help='Track Name')
    parser.add_argument('--endpoint', type=str, default="moq", help='MOQT WT endpoint')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quic-debug', action='store_true',  help='Enable quic debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
    
    return parser.parse_args()

async def main(host: str, port: int, endpoint: str, namespace: str, track_name: str, debug: bool):
    log_level = logging.DEBUG if debug else logging.INFO
    set_log_level(log_level)
    logger = get_logger(__name__)

    client = MOQTClient(
        host,
        port,
        endpoint=endpoint,
        keylog_filename=args.keylogfile,
        debug=debug
    )
    logger.info(f"MOQT app: subscribe session connecting: {client}")
    try:
        async with client.connect() as session:
            try: 
                response = await session.client_session_init()
                
                response = await session.subscribe_announces(
                    namespace_prefix=namespace,
                    parameters={ParamType.AUTHORIZATION_TOKEN: b"auth-token-123"},
                    wait_response=True
                )
                
                if isinstance(response, SubscribeAnnouncesError):
                    logger.error(f"MOQT app: {response}")
                    raise MOQTException(response.error_code, response.reason)

                response = await session.subscribe(
                    namespace=namespace,
                    track_name=track_name,
                    parameters={
                        ParamType.MAX_CACHE_DURATION: 100,
                        ParamType.AUTHORIZATION_TOKEN: b"auth-token-123",
                        ParamType.DELIVERY_TIMEOUT: 10,
                    },
                    wait_response=True
                )
                if isinstance(response, SubscribeError):
                    logger.error(f"MOQT app: {response}")
                    raise MOQTException(response.error_code, response.reason)

                # process subscription - publisher will open stream and send data
                await session.async_closed()
                logger.info(f"MOQT app: exiting client session")

            except MOQTException as e:
                logger.error(f"MOQT app: session exception: {e}")
                session.close(e.error_code, e.reason_phrase)
                pass
            except Exception as e:
                logger.error(f"MOQT app: connection failed: {e}")
                pass
    except Exception as e:
        logger.error(f"MOQT app: connection failed: {e}")
        pass

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
            debug=args.debug
        ), debug=args.debug)
    
    except KeyboardInterrupt:
        pass