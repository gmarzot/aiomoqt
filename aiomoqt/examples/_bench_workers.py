"""Shared multiprocessing worker for bench tools.

A subscriber worker runs one MOQTClient in its own process and posts
two message types to a multiprocessing.Queue:

  {"kind": "stats",  "sub_id": N, "t", "rx_bytes", "rx_objs",
    "lat_mean_ms", "lat_p50_ms", "lat_p99_ms", "loss",
    "total_bytes", "total_objs"}            — every 1 s

  {"kind": "health", "sub_id": N, "state": "subscribed" |
    "subscribe_failed" | "stream_reset" | "closed",
    "detail": "...", "t"}                   — on state transitions

Each worker is GIL-isolated. Parent never shares Python objects with
children except the queue and stop_event. Config is a picklable dict.

Consumers today: aiomoqt.examples.adaptive_bench SubsActuator
(ramps subscriber count dynamically). aiomoqt.examples.multi_sub_bench
is a candidate to port next — its run_subscriber has the same shape
but reports once at end-of-run instead of periodically.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from typing import Any, Dict

from aiomoqt.client import MOQTClient
from aiomoqt.track import SubscribedTrack, TrackState
from aiomoqt.types import FilterType
from aiomoqt.utils.url import parse_relay_url
from aiomoqt.utils.logger import set_log_level


SUBSCRIBE_RETRY_WINDOW_S = 30.0
SUBSCRIBE_EACH_TIMEOUT_S = 5.0
STATS_INTERVAL_S = 1.0
STOP_POLL_S = 0.1


class _RollingStats:
    """5s rolling window, mirrors adaptive_bench.LiveStats semantics.

    Stride-aware loss: when objects are striped across parallel
    subgroups the per-subgroup object_id sequence skips by stride.
    """

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self._events: deque = deque()   # (t, bytes, lat_ms_or_None)
        self._last_seen: Dict = {}       # (group, subgroup) -> last obj_id
        self._stride: Dict = {}
        self._lost = 0
        self._total_bytes = 0
        self._total_objs = 0

    def on_object(self, msg, size_bytes, recv_time_ms):
        t = time.monotonic()
        lat_ms = None
        exts = getattr(msg, 'extensions', None) or {}
        send_ms = exts.get(0x20)
        if send_ms is not None and recv_time_ms is not None \
                and abs(recv_time_ms - send_ms) < 60000:
            lat_ms = recv_time_ms - send_ms
        self._events.append((t, size_bytes, lat_ms))
        self._total_bytes += size_bytes
        self._total_objs += 1
        oid = getattr(msg, 'object_id', None)
        gid = getattr(msg, 'group_id', None)
        sg = getattr(msg, 'subgroup_id', 0) or 0
        if oid is not None and gid is not None:
            key = (gid, sg)
            last = self._last_seen.get(key)
            if last is None:
                self._last_seen[key] = oid
            else:
                stride = self._stride.get(key)
                if stride is None and oid > last:
                    stride = oid - last
                    self._stride[key] = stride
                if stride is not None and stride > 0 and oid > last + stride:
                    self._lost += (oid - last - stride) // stride
                self._last_seen[key] = oid

    def snapshot(self) -> dict:
        t = time.monotonic()
        cutoff = t - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        n = len(self._events)
        if n == 0:
            return dict(t=t, rx_bytes=0, rx_objs=0,
                        lat_mean_ms=0.0, lat_p90_ms=0.0,
                        loss=self._lost,
                        total_bytes=self._total_bytes,
                        total_objs=self._total_objs)
        total_bytes = sum(e[1] for e in self._events)
        lats = sorted(e[2] for e in self._events if e[2] is not None)
        mean = sum(lats) / len(lats) if lats else 0.0
        p90 = lats[min(int(len(lats) * 0.90), len(lats) - 1)] if lats else 0.0
        return dict(t=t, rx_bytes=total_bytes, rx_objs=n,
                    lat_mean_ms=mean, lat_p90_ms=p90,
                    loss=self._lost,
                    total_bytes=self._total_bytes,
                    total_objs=self._total_objs)


def _setup_quiet_logging(logdir, name, debug):
    """Redirect stdout; keep stderr for errors; log files if logdir set.

    stdout gets redirected because periodic print() calls from the sub
    library would interleave with the parent's controller output. stderr
    stays for warnings/tracebacks so silent failures are debuggable.
    """
    devnull = open(os.devnull, 'w')
    sys.stdout = devnull
    if logdir:
        os.makedirs(logdir, exist_ok=True)
        logfile = os.path.join(logdir, f'{name}.log')
        lvl = logging.DEBUG if debug else logging.INFO
        logging.basicConfig(filename=logfile, force=True, level=lvl,
                            format='%(asctime)s %(levelname)s %(message)s')
        set_log_level(lvl)
    else:
        logging.basicConfig(stream=devnull, force=True)
        set_log_level(logging.WARNING)


def _bridge_stop_event(mp_stop_event):
    """Bridge a multiprocessing.Event into an asyncio.Event for this loop.

    mp events don't integrate with asyncio; poll on a short timer.
    """
    ev = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _poll():
        if mp_stop_event.is_set():
            ev.set()
        else:
            loop.call_later(STOP_POLL_S, _poll)

    _poll()
    return ev


def _post(q, msg):
    """Put without blocking on a full queue — drop oldest if we have to."""
    try:
        q.put(msg, block=False)
    except Exception:
        # Queue full or closed. Drop silently; consumer is lagging.
        pass


async def _subscriber_task(config: Dict[str, Any], mp_stop_event,
                           events_queue):
    sub_id = config['sub_id']
    relay = parse_relay_url(config['relay_url'],
                            force_quic=config.get('force_quic', False))
    client = MOQTClient(
        relay.host, relay.port,
        endpoint=relay.endpoint or "",
        use_quic=relay.use_quic,
        verify_tls=not config.get('insecure', False),
        draft_version=config.get('draft'),
    )

    stop_ev = _bridge_stop_event(mp_stop_event)
    stats = _RollingStats()

    def _on_object(msg, size_bytes, recv_time_ms, *_args, **_kw):
        stats.on_object(msg, size_bytes, recv_time_ms)

    try:
        async with client.connect() as session:
            await session.client_session_init()

            # Retry subscribe until publisher's announce propagates through
            # the relay; treat code=4 / does-not-exist / no-such-namespace
            # as "keep trying", any other error as fatal.
            deadline = time.monotonic() + SUBSCRIBE_RETRY_WINDOW_S
            track = None
            while time.monotonic() < deadline and not stop_ev.is_set():
                track = SubscribedTrack(
                    session, config['namespace'],
                    trackname=config['trackname'],
                    draft=config.get('draft'),
                    on_object=_on_object,
                )
                try:
                    ft = FilterType(config.get('sub_filter',
                                               FilterType.LATEST_OBJECT))
                    await track.subscribe(
                        timeout=SUBSCRIBE_EACH_TIMEOUT_S,
                        filter_type=ft,
                    )
                    break
                except Exception as e:
                    m = str(e)
                    if ('code=4' in m or 'does not exist' in m
                            or 'no such namespace' in m):
                        await asyncio.sleep(0.3)
                        continue
                    _post(events_queue, {
                        'kind': 'health', 'sub_id': sub_id,
                        'state': 'subscribe_failed', 'detail': m,
                        't': time.monotonic(),
                    })
                    return
            else:
                _post(events_queue, {
                    'kind': 'health', 'sub_id': sub_id,
                    'state': 'subscribe_failed',
                    'detail': 'timeout waiting for publisher announce',
                    't': time.monotonic(),
                })
                return

            _post(events_queue, {
                'kind': 'health', 'sub_id': sub_id,
                'state': 'subscribed', 't': time.monotonic(),
            })

            async def _stats_loop():
                while not stop_ev.is_set():
                    await asyncio.sleep(STATS_INTERVAL_S)
                    snap = stats.snapshot()
                    snap['kind'] = 'stats'
                    snap['sub_id'] = sub_id
                    _post(events_queue, snap)

            async def _watch_track():
                while not stop_ev.is_set():
                    if getattr(track, 'state', None) == TrackState.CLOSED:
                        completed = getattr(track, 'completed', False)
                        _post(events_queue, {
                            'kind': 'health', 'sub_id': sub_id,
                            'state': ('closed' if completed
                                      else 'stream_reset'),
                            't': time.monotonic(),
                        })
                        return
                    await asyncio.sleep(STOP_POLL_S)

            stats_task = asyncio.create_task(_stats_loop())
            watch_task = asyncio.create_task(_watch_track())
            stop_task = asyncio.create_task(stop_ev.wait())
            try:
                await asyncio.wait(
                    {stats_task, watch_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (stats_task, watch_task, stop_task):
                    t.cancel()
                for t in (stats_task, watch_task, stop_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

            # Final snapshot for totals-at-exit accounting.
            final = stats.snapshot()
            final['kind'] = 'stats'
            final['sub_id'] = sub_id
            _post(events_queue, final)
            _post(events_queue, {
                'kind': 'health', 'sub_id': sub_id,
                'state': 'closed', 't': time.monotonic(),
            })
            session.close()
    except Exception as e:
        _post(events_queue, {
            'kind': 'health', 'sub_id': sub_id,
            'state': 'subscribe_failed',
            'detail': f'connect: {e}',
            't': time.monotonic(),
        })


def sub_worker_entry(config: Dict[str, Any], mp_stop_event, events_queue):
    """Process entrypoint. Spawned via multiprocessing.Process."""
    _setup_quiet_logging(config.get('logdir'),
                         f"sub-{config['sub_id']}",
                         config.get('debug', False))
    try:
        asyncio.run(_subscriber_task(config, mp_stop_event, events_queue))
    except KeyboardInterrupt:
        pass
