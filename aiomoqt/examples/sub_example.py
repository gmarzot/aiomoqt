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
    parser.add_argument('--insecure', action='store_true', help='Skip TLS certificate verification')
    parser.add_argument('--auth-token', type=str, default=None, help='Auth token')
    parser.add_argument('--draft', type=int, default=None, help='MoQT draft version (e.g. 14, 16)')

    return parser.parse_args()

import time


class SimpleStats:
    """Lightweight stats for sub_example."""
    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self.start = 0.0
        self.last_report = 0.0
        self.iv_objects = 0
        self.iv_bytes = 0
        self.total_objects = 0
        self.total_bytes = 0

    def on_object(self, msg, size_bytes, recv_time_ms, group_id=None, subgroup_id=None):
        now = time.monotonic()
        if self.start == 0:
            self.start = now
            self.last_report = now
            print(f"{'Interval':>10}  {'Obj':>7}  {'Rate':>8}  {'Thput':>9}")
            print("─" * 42)
        self.iv_objects += 1
        self.iv_bytes += size_bytes
        self.total_objects += 1
        self.total_bytes += size_bytes
        if now - self.last_report >= self.interval:
            dt = now - self.last_report
            elapsed = now - self.start
            rate = self.iv_objects / dt
            mbps = (self.iv_bytes * 8) / (dt * 1e6)
            iv = f"{elapsed - dt:.0f}-{elapsed:.0f}s"
            print(f"{iv:>10}  {self.iv_objects:>7}  {rate:>6.1f}/s  {mbps:>7.2f}Mb")
            self.iv_objects = 0
            self.iv_bytes = 0
            self.last_report = now

    def summary(self):
        if self.start == 0:
            print("  No data received.")
            return
        dur = time.monotonic() - self.start
        if dur <= 0:
            return
        rate = self.total_objects / dur
        mbps = (self.total_bytes * 8) / (dur * 1e6)
        print(f"\n  Total: {self.total_objects:,} objects, {mbps:.2f} Mbps, {rate:.1f} obj/s ({dur:.1f}s)")


async def main(host: str, port: int, endpoint: str, namespace: str, track_name: str,
               use_quic: bool, debug: bool, quic_debug: bool, insecure: bool = False,
               auth_token: str = None, draft: int = None):
    log_level = logging.DEBUG if debug else logging.INFO
    set_log_level(log_level)
    logger = get_logger(__name__)

    stats = SimpleStats()
    client = MOQTClient(
        host,
        port,
        endpoint=endpoint,
        use_quic=use_quic,
        verify_tls=not insecure,
        draft_version=draft,
        debug=debug,
        quic_debug=quic_debug,
        keylog_filename=args.keylogfile,
    )
    logger.info(f"MOQT app: subscribe session connecting: {client}")
    try:
        async with client.connect() as session:
            session.on_object_received = stats.on_object
            try:
                response = await session.client_session_init()

                params = {ParamType.AUTH_TOKEN: auth_token.encode()} if auth_token else {}
                await session.subscribe_namespace(
                    namespace_prefix=namespace,
                    parameters=params,
                    wait_response=True
                )

                await session.subscribe(
                    namespace=namespace,
                    track_name=track_name,
                    parameters=params,
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

    stats.summary()
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
            insecure=args.insecure,
            auth_token=args.auth_token,
            draft=args.draft,
        ), debug=args.debug)

    except KeyboardInterrupt:
        pass
