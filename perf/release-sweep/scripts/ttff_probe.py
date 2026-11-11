#!/usr/bin/env python3
"""Late-join probe: TTFF and catch-up burst for one subscriber.

Phase 1e needs two numbers a plain sub_bench run cannot give:

  ttff_ms      time from the SUBSCRIBE request leaving this process to the
               first object delivered to the application
  catch-up     what the relay pushes before the joiner is live: bytes and
               distinct groups handed over in the first second after the
               first object, plus the group id the join landed on

The point is a *late join*: the harness starts this process at t=+30 s into a
publisher that is already running, so "first object" is the relay's answer to
a join against an existing cache, not a cold start.

Everything else is deliberately identical to `moq-sub-bench` (same
BenchReporter / TrackStats accounting, same flags), so the summary block this
prints parses with the same `collect.parse_file()` and the point's throughput
and latency stay comparable with the media points. The extra numbers go out
as a single `TTFF-JSON: {...}` line, which collect.py ignores.

Run under the arm venv, e.g.:

  python -P -m perf.release_sweep.scripts.ttff_probe moqt://10.14.25.173:9768 \
      -N aiomoqt -T track --draft 16 -k -t 90 -i 5 --json-out ttff.json
"""
import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import time

from aiomoqt.client import MOQTClient
from aiomoqt.track import SubscribedTrack
from aiomoqt.types import MOQTException, MOQTRequestError
from aiomoqt.utils import wait_cond_timeout
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.url import parse_relay_url
from aiomoqt.tools.sub_bench import BenchReporter


def parse_args():
    parser = _cli.make_parser(
        'aiomoqt late-join probe — TTFF + catch-up burst for one subscriber')
    _cli.add_endpoint(parser)
    _cli.add_identity(parser)
    _cli.add_run(parser, duration=0)
    _cli.add_interval(parser)
    _cli.add_session(parser)
    _cli.add_keepalive(parser)
    _cli.add_compat(parser)
    _cli.add_timestamp_checking(parser)
    parser.add_argument('--json-out', default=None,
                        help='write the TTFF record here as JSON')
    _cli.add_help(parser)
    return parser.parse_args()


def emit(record, path):
    line = "TTFF-JSON: " + json.dumps(record, sort_keys=True)
    print(line, flush=True)
    if path:
        try:
            with open(path, "w") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
                fh.write("\n")
        except OSError as exc:  # never lose the console copy
            print(f"  json-out failed: {exc}", flush=True)


async def run(args):
    set_log_level(logging.DEBUG if args.debug else logging.WARNING)
    relay = parse_relay_url(args.url)
    stats = BenchReporter(report_interval=args.interval,
                          minimal=args.no_stats)

    rec = {
        "url": args.url,
        "namespace": args.namespace,
        "track": args.trackname,
        "subscribe_sent_unix": None,
        "first_object_unix": None,
        "ttff_ms": None,
        "first_group": None,
        "first_object_id": None,
        "first_object_bytes": None,
        "burst_bytes_1s": None,
        "burst_groups_1s": None,
        "objects": 0,
        "bytes": 0,
        "groups": [],
        "error": None,
    }
    t_sub = None
    t_first = None
    burst = {"bytes": 0, "groups": set()}

    def on_object(msg, size_bytes, recv_time_us, group_id=None,
                  subgroup_id=None):
        nonlocal t_first
        now = time.monotonic()
        if t_first is None:
            t_first = now
            rec["first_object_unix"] = round(time.time(), 6)
            rec["ttff_ms"] = round((now - t_sub) * 1000.0, 2)
            rec["first_group"] = group_id
            rec["first_object_id"] = getattr(msg, "object_id", None)
            rec["first_object_bytes"] = size_bytes
        elif now - t_first <= 1.0:
            burst["bytes"] += size_bytes
            if group_id is not None:
                burst["groups"].add(group_id)
        rec["objects"] += 1
        rec["bytes"] += size_bytes
        if group_id is not None and group_id not in rec["groups"]:
            rec["groups"].append(group_id)
        stats.on_object(msg, size_bytes, recv_time_us, group_id, subgroup_id)

    client = MOQTClient(
        relay.host, relay.port,
        path=relay.path,
        use_quic=relay.use_quic,
        verify_tls=not args.insecure,
        supported_drafts=args.draft,
        debug=args.debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=args.cc_algo,
        keep_alive_interval=args.keepalive,
    )

    # The harness SIGINTs at the end of the window; print the record anyway so
    # a rep that is cut short still yields its TTFF.
    def _sigint(_sig, _frm):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint)

    failed = True
    try:
        async with contextlib.AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(args.duration or None):
                    session = await stack.enter_async_context(client.connect())
            except TimeoutError:
                raise ConnectionError(f"no session within {args.duration}s")
            try:
                async with asyncio.timeout(args.duration or None):
                    await session.client_session_init()
                    track = SubscribedTrack(
                        session,
                        namespace=args.namespace,
                        trackname=args.trackname,
                        on_object=on_object,
                        timestamp_errors=args.timestamp_errors,
                    )
                    stats.stats.timing = track.timing
                    t_sub = time.monotonic()
                    rec["subscribe_sent_unix"] = round(time.time(), 6)
                    await track.subscribe()
                stats.start()
                if not await wait_cond_timeout(track.wait_closed(),
                                               timeout=args.duration):
                    track.completed = True
                failed = False
            except MOQTRequestError as e:
                rec["error"] = f"request: {e}"
                session.close()
            except MOQTException as e:
                rec["error"] = f"moqt: {e}"
                session.close(e.error_code, e.reason_phrase)
            except TimeoutError:
                rec["error"] = f"no subscription within {args.duration}s"
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"connect: {type(e).__name__}: {e}"
    except KeyboardInterrupt:
        rec["error"] = rec["error"] or "interrupted"

    burst_start = t_first
    if burst_start is not None:
        rec["burst_bytes_1s"] = burst["bytes"]
        rec["burst_groups_1s"] = len(burst["groups"])
    rec["stream_seconds"] = (
        round(time.monotonic() - t_sub, 2) if t_sub else None)

    stats.print_summary()
    emit(rec, args.json_out)
    return 0 if rec["objects"] else 1


def cli():
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\n  Interrupted.")


if __name__ == "__main__":
    cli()
