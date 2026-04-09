#!/usr/bin/env python3

import asyncio
import argparse
import logging

from aiomoqt.types import ParamType, MOQTException, MOQTRequestError
from aiomoqt.client import MOQTClient
from aiomoqt.track import SubscribedTrack
from aiomoqt.utils.logger import *


def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Client')
    parser.add_argument('--host', type=str, default='localhost', help='Host to connect to')
    parser.add_argument('--port', type=int, default=443, help='Port to connect to')
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
    parser.add_argument('--libquicr-compat', action='store_true', help='Use libquicr filter encoding (LAPS)')
    parser.add_argument('-t', '--duration', type=int, default=120, help='Duration in seconds (default: 120)')

    return parser.parse_args()

import time


class SimpleStats:
    """Lightweight stats for sub_example with latency tracking."""
    TIMESTAMP_EXT = 0x20  # MOQT_TIMESTAMP_EXT

    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self.start = 0.0
        self.last_report = 0.0
        self.iv_objects = 0
        self.iv_bytes = 0
        self.iv_groups = set()
        self.iv_latencies = []
        self.total_objects = 0
        self.total_bytes = 0
        self.total_groups = set()
        self.all_latencies = []
        self._header_printed = False

    def _print_header(self):
        if self._header_printed:
            return
        self._header_printed = True
        print(f"\n  {'Interval':<12}{'Groups':<18}"
              f"{'Objects':<22}{'Bitrate':<14}{'Latency'}")
        print("  " + "─" * 72)

    def on_object(self, msg, size_bytes, recv_time_ms,
                  group_id=None, subgroup_id=None):
        now = time.monotonic()
        if self.start == 0:
            self.start = now
            self.last_report = now

        self.iv_objects += 1
        self.iv_bytes += size_bytes
        if group_id is not None:
            self.iv_groups.add(group_id)
            self.total_groups.add(group_id)
        self.total_objects += 1
        self.total_bytes += size_bytes

        # Latency from timestamp extension
        send_ms = (msg.extensions.get(self.TIMESTAMP_EXT)
                   if msg.extensions else None)
        if send_ms is not None:
            latency = recv_time_ms - send_ms
            if abs(latency) < 60000:
                self.iv_latencies.append(latency)
                self.all_latencies.append(latency)

        if now - self.last_report >= self.interval:
            self._print_header()
            dt = now - self.last_report
            elapsed = now - self.start
            obj_s = self.iv_objects / dt
            grps = len(self.total_groups)
            grp_s = len(self.iv_groups) / dt
            mbps = (self.iv_bytes * 8) / (dt * 1e6)
            iv = f"{elapsed - dt:.0f}-{elapsed:.0f}s"
            grp_col = f"{grps} ({grp_s:.1f}/s)"
            obj_col = f"{self.total_objects:,} ({obj_s:.1f}/s)"
            lat = ""
            if self.iv_latencies:
                avg = sum(self.iv_latencies) / len(self.iv_latencies)
                lat = f"{avg:.0f} ms"
            print(f"  {iv:<12}{grp_col:<18}"
                  f"{obj_col:<22}{mbps:.2f} Mbps"
                  f"{'':>4}{lat}")
            self.iv_objects = 0
            self.iv_bytes = 0
            self.iv_groups = set()
            self.iv_latencies = []
            self.last_report = now

    def summary(self):
        if self.start == 0:
            print("  No data received.")
            return
        dur = time.monotonic() - self.start
        if dur <= 0:
            return
        obj_s = self.total_objects / dur
        grps = len(self.total_groups)
        mbps = (self.total_bytes * 8) / (dur * 1e6)
        lat_s = ""
        if self.all_latencies:
            avg = sum(self.all_latencies) / len(self.all_latencies)
            p50 = sorted(self.all_latencies)[len(self.all_latencies) // 2]
            lat_s = f", latency avg={avg:.0f}ms p50={p50:.0f}ms"
        print(f"\n  Total: {self.total_objects:,} objects, "
              f"{grps:,} groups, "
              f"{obj_s:.1f} obj/s, "
              f"{mbps:.2f} Mbps{lat_s} ({dur:.1f}s)")


async def main(host: str, port: int, endpoint: str, namespace: str, track_name: str,
               use_quic: bool, debug: bool, quic_debug: bool, insecure: bool = False,
               auth_token: str = None, draft: int = None, libquicr_compat: bool = False,
               duration: int = 120):
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
        libquicr_compat=libquicr_compat,
        debug=debug,
        quic_debug=quic_debug,
        keylog_filename=args.keylogfile,
    )
    logger.info(f"MOQT app: subscribe session connecting: {client}")
    try:
        async with client.connect() as session:
            try:
                await session.client_session_init()

                track = SubscribedTrack(
                    session,
                    namespace=namespace,
                    trackname=track_name,
                    draft=draft,
                    on_object=stats.on_object,
                )
                await track.subscribe()
                logger.info(f"MOQT app: subscribed to {track.fqtn}")

                await track.wait_closed(timeout=duration)
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
            libquicr_compat=args.libquicr_compat,
            duration=args.duration,
        ), debug=args.debug)

    except KeyboardInterrupt:
        pass
