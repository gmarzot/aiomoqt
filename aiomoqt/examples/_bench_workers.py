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
        # Per-snapshot deltas — snapshot() returns bytes/objs/loss
        # since the last call so the consumer can sum across its own
        # window without double-counting our rolling window.
        self._last_total_bytes = 0
        self._last_total_objs = 0
        self._last_lost = 0

    def on_object(self, msg, size_bytes, recv_time_us):
        """Timestamps on the wire are us; lat is float ms."""
        t = time.monotonic()
        lat_ms = None
        exts = getattr(msg, 'extensions', None) or {}
        send_us = exts.get(0x20)
        if send_us is not None and recv_time_us is not None:
            raw_us = recv_time_us - send_us
            # Reject negatives + absurd values from deframer garbage;
            # accept up to 10 minutes (real under-load latency).
            if -1_000_000 <= raw_us <= 600_000_000:
                lat_ms = raw_us / 1000.0
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
        """Per-snapshot delta. rx_bytes / rx_objs / iv_lost cover the
        time since the last snapshot — the consumer can sum them over
        its own window without double-counting our rolling window.
        Latency stats stay windowed (need samples for percentiles)."""
        t = time.monotonic()
        cutoff = t - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        # Latency from the rolling window
        lats = sorted(e[2] for e in self._events if e[2] is not None)
        mean = sum(lats) / len(lats) if lats else 0.0
        p90 = lats[min(int(len(lats) * 0.90), len(lats) - 1)] if lats else 0.0
        # Bytes / objs / loss as DELTAS since last call
        iv_bytes = self._total_bytes - self._last_total_bytes
        iv_objs = self._total_objs - self._last_total_objs
        iv_lost = self._lost - self._last_lost
        self._last_total_bytes = self._total_bytes
        self._last_total_objs = self._total_objs
        self._last_lost = self._lost
        return dict(t=t, rx_bytes=iv_bytes, rx_objs=iv_objs,
                    lat_mean_ms=mean, lat_p90_ms=p90,
                    iv_lost=iv_lost, loss=self._lost,
                    total_bytes=self._total_bytes,
                    total_objs=self._total_objs)


def _setup_quiet_logging(logdir, name, debug):
    """Redirect stdout; route logs based on (logdir, debug):

      logdir + debug=True  : DEBUG level to <logdir>/<name>.log
      logdir + debug=False : INFO  level to <logdir>/<name>.log
      no logdir + debug    : DEBUG to stderr (visible in parent terminal)
      no logdir + no debug : WARNING to stderr (errors only)

    stdout always goes to /dev/null so periodic print() in subordinate
    libraries doesn't interleave with the parent's controller output.
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
        lvl = logging.DEBUG if debug else logging.WARNING
        logging.basicConfig(stream=sys.stderr, force=True, level=lvl,
                            format=f'[{name}] %(asctime)s %(levelname)s %(message)s')
        set_log_level(lvl)


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
        path=relay.path or "",
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


# ---------------------------------------------------------------------------
# Publisher worker — for adaptive_bench BW mode + (eventually) loopback bench
# ---------------------------------------------------------------------------

async def _publisher_task(config: Dict[str, Any], mp_stop_event,
                          rate_queue, events_queue):
    """Connect to relay, publish a track, drive paced object generation
    at a rate controllable from the parent via rate_queue.

    rate_queue messages: {'rate_ops': float}  (objects/sec aggregate)
    events_queue stats: {'kind':'pub_stats', 't', 'tx_bytes', 'tx_objs',
                          'cumulative_bytes', 'cumulative_objs'} every 1s.
    """
    from aiomoqt.types import MOQTMessageType
    from aiomoqt.track import PublishedTrack

    relay = parse_relay_url(config['relay_url'],
                            force_quic=config.get('force_quic', False))
    client = MOQTClient(
        relay.host, relay.port,
        path=relay.path or "",
        use_quic=relay.use_quic,
        verify_tls=not config.get('insecure', False),
        draft_version=config.get('draft'),
    )

    stop_ev = _bridge_stop_event(mp_stop_event)
    iv_bytes = 0
    iv_objs = 0
    total_bytes = 0
    total_objs = 0

    try:
        async with client.connect() as session:
            await session.client_session_init()

            track = PublishedTrack(
                session,
                namespace=config['namespace'],
                trackname=config['trackname'],
                object_size=config['object_size'],
                group_size=config.get('group_size', 10000),
                num_subgroups=config.get('num_subgroups', 1),
                rate=config.get('initial_rate_ops', 0.0),
            )
            track._stats_header_printed = True
            track._quiet = True

            await track.publish(
                announce_namespace=config.get('pub_ns', False)
                                   or config.get('pub_both', False),
                publish_track=not config.get('pub_ns', False)
                              or config.get('pub_both', False),
            )

            _post(events_queue, {
                'kind': 'pub_health', 'state': 'published',
                't': time.monotonic(),
            })

            async def _rate_listener():
                """Drain rate_queue without blocking; mutate track.rate."""
                while not stop_ev.is_set():
                    try:
                        msg = rate_queue.get_nowait()
                        if isinstance(msg, dict) and 'rate_ops' in msg:
                            track.rate = float(msg['rate_ops'])
                    except Exception:
                        await asyncio.sleep(0.05)
                        continue

            async def _stats_loop():
                """Per-second pub stats. tx_bytes counts bytes the
                publisher has *queued* via send_stream_data; wire_bytes
                counts what picoquic has actually placed on the wire.
                The two diverge during slow-start and any time the
                publisher's queue grows beyond the cwnd."""
                nonlocal iv_bytes, iv_objs, total_bytes, total_objs
                last_total_bytes = 0
                last_total_objs = 0
                last_wire_bytes = 0
                quic = getattr(session, '_quic', None)
                while not stop_ev.is_set():
                    await asyncio.sleep(STATS_INTERVAL_S)
                    cur_bytes = getattr(track, '_total_bytes', 0)
                    cur_objs = getattr(track, '_total_sent', 0)
                    iv_bytes = cur_bytes - last_total_bytes
                    iv_objs = cur_objs - last_total_objs
                    last_total_bytes = cur_bytes
                    last_total_objs = cur_objs
                    total_bytes = cur_bytes
                    total_objs = cur_objs
                    # Wire-bytes from picoquic's internal counter. The
                    # cnx pointer is only valid while the cnx is alive;
                    # swallow anything thrown by the FFI to keep the
                    # stats loop running through teardown.
                    cur_wire = 0
                    try:
                        if quic is not None:
                            cur_wire = quic.bytes_sent
                    except (Exception, SystemError):
                        cur_wire = last_wire_bytes
                    iv_wire = max(0, cur_wire - last_wire_bytes)
                    last_wire_bytes = cur_wire
                    _post(events_queue, {
                        'kind': 'pub_stats',
                        't': time.monotonic(),
                        'tx_bytes': iv_bytes,
                        'tx_objs': iv_objs,
                        'wire_bytes': iv_wire,
                        'cumulative_bytes': total_bytes,
                        'cumulative_objs': total_objs,
                        'cumulative_wire_bytes': cur_wire,
                    })

            # Generation is launched by track.publish()'s handler
            # chain (_on_subscribe → _start_generating → track.generate)
            # when the subscriber arrives. The worker just needs to
            # stay alive — wait on rate / stats / stop tasks; the gen
            # task lives inside the session's _tasks set.
            rate_task = asyncio.create_task(_rate_listener())
            stats_task = asyncio.create_task(_stats_loop())
            stop_task = asyncio.create_task(stop_ev.wait())
            try:
                await asyncio.wait(
                    {rate_task, stats_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (rate_task, stats_task, stop_task):
                    t.cancel()
                for t in (rate_task, stats_task, stop_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

            _post(events_queue, {
                'kind': 'pub_health', 'state': 'closed',
                't': time.monotonic(),
            })
            session.close()
    except Exception as e:
        _post(events_queue, {
            'kind': 'pub_health', 'state': 'failed',
            'detail': f'publisher error: {e}',
            't': time.monotonic(),
        })


def pub_worker_entry(config: Dict[str, Any], mp_stop_event,
                     rate_queue, events_queue):
    """Process entrypoint. Spawned via multiprocessing.Process."""
    _setup_quiet_logging(config.get('logdir'), 'pub',
                         config.get('debug', False))
    try:
        asyncio.run(_publisher_task(config, mp_stop_event,
                                     rate_queue, events_queue))
    except KeyboardInterrupt:
        pass
