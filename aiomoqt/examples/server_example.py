import time
import logging
import argparse
import datetime

import asyncio
from qh3.h3.connection import H3_ALPN

from qh3.quic.configuration import QuicConfiguration
from aiomoqt.server import MOQTServer
from aiomoqt.utils.logger import get_logger, set_log_level

def parse_args():
    defaults = QuicConfiguration(is_client=False)
    
    parser = argparse.ArgumentParser(description='MOQT WebTransport Server')
    parser.add_argument('--host', type=str, default='localhost', help='Host to bind to')
    parser.add_argument('--port', type=int, default=4433, help='Port to bind to')
    parser.add_argument('--certificate', type=str, required=True, help='TLS server certificate')
    parser.add_argument('--private-key', type=str, required=True, help='TLS private key')
    parser.add_argument('--endpoint', type=str, default="moq", help='MOQT WebTransport endpoint')
    parser.add_argument('--retry', action='store_true', help='send a retry for new connections')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quic-debug', action='store_true',  help='Enable quic debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
    
    return parser.parse_args()

async def main(args):
    log_level = logging.DEBUG if args.debug else logging.INFO
    set_log_level(log_level)
    logger = get_logger(__name__)

    server = MOQTServer(
        host=args.host,
        port=args.port,
        certificate=args.certificate,
        private_key=args.private_key,
        endpoint=args.endpoint,
        debug=args.debug
    )

    logger.info(f"MOQT server: starting session: {server}")
    try:
        # run until closed
        quic_server = await server.serve()

        await server.closed()
            
    except Exception as e:
        logger.error(f"MOQT server: session exception: {e}")
    finally:
        logger.info("MOQT server: shutting down")

if __name__ == "__main__":
    try:
        args = parse_args()
        asyncio.run(main(args), debug=args.debug)
    
    except KeyboardInterrupt:
        pass