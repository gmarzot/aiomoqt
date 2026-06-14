import logging
import argparse

import asyncio

from aiomoqt.server import MOQTServer
from aiomoqt.utils.logger import get_logger, set_log_level


def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Server', add_help=False)
    parser.add_argument('-h', '--host', type=str, default='localhost', help='Host to bind to')
    parser.add_argument('--port', type=int, default=443, help='Port to bind to')
    parser.add_argument('--certificate', type=str, required=True, help='TLS server certificate')
    parser.add_argument('--private-key', type=str, required=True, help='TLS private key')
    parser.add_argument('--path', type=str, default="", help='MOQT WebTransport path (default: "/")')
    parser.add_argument('--retry', action='store_true', help='send a retry for new connections')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quic-debug', action='store_true',  help='Enable quic debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
    parser.add_argument('--cc-algo', type=str, default='bbr',
                        help='Congestion control algorithm '
                             '(bbr | bbr1 | newreno | cubic | dcubic | '
                             'prague | fast). Default: bbr')

    parser.add_argument(
        '-?', '--help', action='help',
        help='Show this help message and exit')
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
        path=args.path,
        debug=args.debug,
        congestion_control_algorithm=args.cc_algo,
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