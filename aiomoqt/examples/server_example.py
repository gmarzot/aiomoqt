import logging
import argparse

import asyncio

from aiomoqt.server import MOQTServer
from aiomoqt.utils.cli import add_help
from aiomoqt.utils.logger import get_logger, set_log_level


def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Server', add_help=False)
    parser.add_argument('--bind', dest='host', type=str, default='localhost',
                        help='Bind address (default: localhost)')
    parser.add_argument('-p', '--port', type=int, default=4433,
                        help='Listen port (default: 4433)')
    parser.add_argument('-Q', '--quic', action='store_true',
                        help='Serve raw QUIC')
    parser.add_argument('-W', '--wt', action='store_true',
                        help='Serve H3/WebTransport (default; with -Q, '
                             'both on one port)')
    parser.add_argument('--cert', dest='certificate', type=str, required=True,
                        help='TLS server certificate')
    parser.add_argument('--key', dest='private_key', type=str, required=True,
                        help='TLS private key')
    parser.add_argument('--path', type=str, default="", help='MOQT WebTransport path (default: "/")')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quic-debug', action='store_true',  help='Enable quic debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
    parser.add_argument('--cc-algo', type=str, default='bbr',
                        help='Congestion control algorithm '
                             '(bbr | bbr1 | newreno | cubic | dcubic | '
                             'prague | fast). Default: bbr')

    add_help(parser)
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
        use_quic=args.quic and not args.wt,
        debug=args.debug,
        congestion_control_algorithm=args.cc_algo,
    )

    logger.info(f"MOQT server: starting session: {server}")
    try:
        # run until closed
        if args.quic and args.wt:
            quic_server = await server.serve_dual()
        else:
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