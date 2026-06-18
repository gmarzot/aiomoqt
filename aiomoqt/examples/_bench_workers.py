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
SELF_HEAL_BACKOFF_S = 0.5


class _RollingStats:
    """5s rolling window, mirrors adaptive_bench.LiveStats semantics.

    Stride-aware loss: when objects are striped across parallel
    subgroups the per-subgroup object_id sequence skips by stride.
    """

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self._events: deque = deque()   # (t, bytes, lat_ms_or_None)
        # Latency sampling stride — adaptively raised by snapshot()
        # so the percentile sort stays bounded; a full 5 s window at
        # 100K+ obj/s is 500K+ entries and sorting it stalls the
        # consumer loop ~100 ms, queueing arrivals it then measures.
        self._lat_stride: int = 1
        self._n_events: int = 0
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
        # RFC 3550 jitter: smooth |D| where D = inter-arrival skew
        # vs source pacing. Updated per-object in on_object().
        self._jitter_ms = 0.0
        self._last_recv_us: int = 0
        self._last_send_us: int = 0

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
                if self._last_recv_us and self._last_send_us:
                    d_us = abs((recv_time_us - self._last_recv_us)
                               - (send_us - self._last_send_us))
                    self._jitter_ms += (d_us / 1000.0
                                        - self._jitter_ms) / 16.0
                self._last_recv_us = recv_time_us
                self._last_send_us = send_us
        self._n_events += 1
        if self._n_events % self._lat_stride == 0:
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
        # Adaptive thinning: bound the percentile sort so the snapshot
        # cannot stall the consumer loop at high object rates.
        if len(self._events) > 24576:
            self._events = deque(list(self._events)[::2])
            self._lat_stride *= 2
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
                    jitter_ms=self._jitter_ms,
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
        keylog_filename=config.get('keylogfile'),
        keep_alive_interval=config.get('keep_alive_interval'),
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


def _try_install_uvloop() -> None:
    """Best-effort uvloop install in this worker process. Workers are
    separate interpreters so the parent's policy doesn't carry over."""
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass


def sub_worker_entry(config: Dict[str, Any], mp_stop_event, events_queue):
    """Process entrypoint. Spawned via multiprocessing.Process."""
    _setup_quiet_logging(config.get('logdir'),
                         f"sub-{config['sub_id']}",
                         config.get('debug', False))
    if not config.get('no_uvloop', False):
        _try_install_uvloop()
    # AIOMOQT_PROFILE_SUB=<path> wraps this worker's main loop in
    # cProfile. Writes <path>.<sub_id> .prof on exit so multi-sub runs
    # don't clobber each other. Effectively free when the env is unset.
    import os as _os
    _profile_path = _os.environ.get('AIOMOQT_PROFILE_SUB')
    try:
        if _profile_path:
            import cProfile
            prof = cProfile.Profile()
            prof.enable()
            try:
                asyncio.run(_subscriber_task(config, mp_stop_event, events_queue))
            finally:
                prof.disable()
                prof.dump_stats(f"{_profile_path}.{config['sub_id']}.prof")
        else:
            asyncio.run(_subscriber_task(config, mp_stop_event, events_queue))
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Batched subscriber worker — one process hosts K subscriptions, each
# self-healing (reopened on close/reset). Reports ONE aggregate
# batch_stats per interval. Lets adaptive_bench drive thousands of subs
# without one OS process per sub, while keeping the relay's view at N
# connections.
# ---------------------------------------------------------------------------

async def _slot_supervisor(config, relay, stop_ev, stats, state,
                           initial_delay):
    """Maintain ONE subscription for the batch's lifetime: connect,
    subscribe, receive until the track closes/resets, then reopen after
    a short backoff. Runs until stop_ev. The per-slot _RollingStats is
    passed in and persists across reopens, so a transient drop doesn't
    reset the batch's cumulative totals or latency window.

    state['active'] reflects whether this slot is currently subscribed
    and delivering — the batch stats loop sums it for the active count.
    """
    if initial_delay > 0:
        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=initial_delay)
            return
        except asyncio.TimeoutError:
            pass

    def _on_object(msg, size_bytes, recv_time_ms, *_args, **_kw):
        stats.on_object(msg, size_bytes, recv_time_ms)

    insecure = config.get('insecure', False)
    while not stop_ev.is_set():
        state['active'] = False
        try:
            client = MOQTClient(
                relay.host, relay.port,
                path=relay.path or "",
                use_quic=relay.use_quic,
                verify_tls=not insecure,
                draft_version=config.get('draft'),
                keep_alive_interval=config.get('keep_alive_interval'),
            )
            async with client.connect() as session:
                await session.client_session_init()
                deadline = time.monotonic() + SUBSCRIBE_RETRY_WINDOW_S
                track = None
                subscribed = False
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
                        subscribed = True
                        break
                    except Exception as e:
                        m = str(e)
                        if ('code=4' in m or 'does not exist' in m
                                or 'no such namespace' in m):
                            await asyncio.sleep(0.3)
                            continue
                        # Other subscribe error: drop the connection and
                        # let the outer loop reopen the whole slot.
                        break
                if subscribed:
                    state['active'] = True
                    while not stop_ev.is_set():
                        if getattr(track, 'state', None) == TrackState.CLOSED:
                            break
                        await asyncio.sleep(STOP_POLL_S)
                    state['active'] = False
                session.close()
        except Exception:
            pass
        if stop_ev.is_set():
            break
        # Self-heal: brief backoff, then reopen this slot. Interruptible
        # so a stop during backoff exits promptly.
        try:
            await asyncio.wait_for(stop_ev.wait(),
                                   timeout=SELF_HEAL_BACKOFF_S)
        except asyncio.TimeoutError:
            pass
    state['active'] = False


async def _subscriber_batch_task(config: Dict[str, Any], mp_stop_event,
                                 events_queue):
    """Host config['batch_size'] subscriptions on one event loop, each
    self-healing via _slot_supervisor. Posts one aggregate batch_stats
    event per STATS_INTERVAL_S:

      {"kind": "batch_stats", "worker_id": W, "batch_size": K,
       "active": <subscribed slots>, "t",
       "rx_bytes", "rx_objs" (deltas since last post),
       "lat_p90_ms", "lat_mean_ms" (worst across slots),
       "jitter_ms", "loss", "total_bytes", "total_objs"}
    """
    worker_id = config['worker_id']
    K = max(1, int(config.get('batch_size', 1)))
    stagger = float(config.get('stagger', 0.0))
    relay = parse_relay_url(config['relay_url'],
                            force_quic=config.get('force_quic', False))
    stop_ev = _bridge_stop_event(mp_stop_event)

    # (stats, state) per slot; stats persist across a slot's reopens.
    slots = [(_RollingStats(), {'active': False}) for _ in range(K)]

    async def _batch_stats_loop():
        while not stop_ev.is_set():
            await asyncio.sleep(STATS_INTERVAL_S)
            agg_bytes = agg_objs = agg_iv_lost = 0
            cum_loss = total_bytes = total_objs = 0
            worst_p90 = worst_mean = 0.0
            jitter_sum = 0.0
            jitter_n = 0
            active = 0
            for stats, state in slots:
                snap = stats.snapshot()
                agg_bytes += snap['rx_bytes']
                agg_objs += snap['rx_objs']
                agg_iv_lost += snap['iv_lost']
                cum_loss += snap['loss']
                total_bytes += snap['total_bytes']
                total_objs += snap['total_objs']
                if snap['lat_p90_ms'] > worst_p90:
                    worst_p90 = snap['lat_p90_ms']
                if snap['lat_mean_ms'] > worst_mean:
                    worst_mean = snap['lat_mean_ms']
                jitter_sum += snap['jitter_ms']
                jitter_n += 1
                if state['active']:
                    active += 1
            _post(events_queue, {
                'kind': 'batch_stats', 'worker_id': worker_id,
                'batch_size': K, 'active': active,
                't': time.monotonic(),
                'rx_bytes': agg_bytes, 'rx_objs': agg_objs,
                'iv_lost': agg_iv_lost, 'loss': cum_loss,
                'lat_p90_ms': worst_p90, 'lat_mean_ms': worst_mean,
                'jitter_ms': jitter_sum / jitter_n if jitter_n else 0.0,
                'total_bytes': total_bytes, 'total_objs': total_objs,
            })

    supervisors = [
        asyncio.create_task(
            _slot_supervisor(config, relay, stop_ev, stats, state,
                             i * stagger))
        for i, (stats, state) in enumerate(slots)
    ]
    stats_task = asyncio.create_task(_batch_stats_loop())
    stop_task = asyncio.create_task(stop_ev.wait())
    try:
        await stop_task
    finally:
        for t in [stats_task, *supervisors]:
            t.cancel()
        for t in [stats_task, *supervisors]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


def sub_batch_worker_entry(config: Dict[str, Any], mp_stop_event,
                           events_queue):
    """Process entrypoint for a batched subscriber worker."""
    _setup_quiet_logging(config.get('logdir'),
                         f"subw-{config['worker_id']}",
                         config.get('debug', False))
    if not config.get('no_uvloop', False):
        _try_install_uvloop()
    try:
        asyncio.run(_subscriber_batch_task(config, mp_stop_event,
                                           events_queue))
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
        keylog_filename=config.get('keylogfile'),
        keep_alive_interval=config.get('keep_alive_interval'),
    )

    stop_ev = _bridge_stop_event(mp_stop_event)
    iv_bytes = 0
    iv_objs = 0
    total_bytes = 0
    total_objs = 0

    try:
        async with client.connect() as session:
            await session.client_session_init()

            _num_sg = max(1, config.get('num_subgroups', 1))
            track = PublishedTrack(
                session,
                namespace=config['namespace'],
                trackname=config['trackname'],
                object_size=config['object_size'],
                group_size=config.get('group_size', 10000),
                num_subgroups=_num_sg,
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
                """Drain rate_queue without blocking; mutate track.rate.
                Both rate_ops on the wire and PublishedTrack.rate are
                AGGREGATE objects/sec (the track divides by
                num_subgroups in its send loop) — assign verbatim.
                """
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
                    # Per-stream TX ring depth (bytes queued in the
                    # aiopquic SPSC ring waiting for picoquic to drain).
                    # Sum and max across all open streams. Bloat shows
                    # up here when target_tx > picoquic drain rate; if
                    # this stays small but Python memory still grows,
                    # the queueing is upstream of the ring.
                    ring_depth_sum = 0
                    ring_depth_max = 0
                    n_streams = 0
                    try:
                        if quic is not None:
                            for sid in list(quic._stream_ctxs):
                                st = quic.get_stream_buf_stats(sid)
                                if st is None:
                                    continue
                                pushed, popped = st[0], st[1]
                                depth = max(0, pushed - popped)
                                ring_depth_sum += depth
                                if depth > ring_depth_max:
                                    ring_depth_max = depth
                                n_streams += 1
                    except (Exception, SystemError):
                        pass
                    _post(events_queue, {
                        'kind': 'pub_stats',
                        't': time.monotonic(),
                        'tx_bytes': iv_bytes,
                        'tx_objs': iv_objs,
                        'wire_bytes': iv_wire,
                        'cumulative_bytes': total_bytes,
                        'cumulative_objs': total_objs,
                        'cumulative_wire_bytes': cur_wire,
                        'tx_ring_depth_bytes': ring_depth_sum,
                        'tx_ring_depth_max_bytes': ring_depth_max,
                        'tx_ring_streams': n_streams,
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
    if not config.get('no_uvloop', False):
        _try_install_uvloop()
    try:
        asyncio.run(_publisher_task(config, mp_stop_event,
                                     rate_queue, events_queue))
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Loopback server worker — adaptive_bench --mp-loopback. Acts as the
# publisher endpoint for a co-located subscriber proc; talks the same
# rate_queue / pub_stats events_queue protocol as pub_worker_entry, so
# the parent's BWActuator MP path drains both modes the same way.
# ---------------------------------------------------------------------------

async def _loopback_server_task(config: Dict[str, Any], mp_stop_event,
                                  rate_queue, events_queue):
    from aiomoqt.server import MOQTServer
    from aiomoqt.types import MOQTMessageType
    from aiomoqt.track import PublishedTrack

    stop_ev = _bridge_stop_event(mp_stop_event)
    _num_sg = max(1, config.get('num_subgroups', 1))
    initial_rate = config.get('initial_rate_ops', 0.0)
    track_holder: Dict[str, Any] = {'track': None}

    async def _on_subscribe(session, msg):
        track = PublishedTrack(
            session,
            namespace=config['namespace'],
            trackname=config['trackname'],
            object_size=config['object_size'],
            group_size=config.get('group_size', 10000),
            num_subgroups=_num_sg,
            rate=initial_rate,
        )
        track._stats_header_printed = True
        track._quiet = True
        ok = session.subscribe_ok(request_msg=msg)
        track.track_alias = ok.track_alias
        track._generating = True
        track_holder['track'] = track
        try:
            await track.generate(session, ok.track_alias)
        finally:
            track_holder['track'] = None

    async def _rate_listener():
        while not stop_ev.is_set():
            try:
                rmsg = rate_queue.get_nowait()
                track = track_holder['track']
                if (track is not None and isinstance(rmsg, dict)
                        and 'rate_ops' in rmsg):
                    track.rate = float(rmsg['rate_ops'])
            except Exception:
                await asyncio.sleep(0.05)
                continue

    async def _stats_loop():
        last_total_bytes = 0
        last_total_objs = 0
        last_wire_bytes = 0
        while not stop_ev.is_set():
            await asyncio.sleep(STATS_INTERVAL_S)
            track = track_holder['track']
            if track is None:
                continue
            cur_bytes = getattr(track, '_total_bytes', 0)
            cur_objs = getattr(track, '_total_sent', 0)
            iv_bytes = cur_bytes - last_total_bytes
            iv_objs = cur_objs - last_total_objs
            last_total_bytes = cur_bytes
            last_total_objs = cur_objs
            cur_wire = 0
            quic = getattr(track.session, '_quic', None)
            try:
                if quic is not None:
                    cur_wire = quic.bytes_sent
            except (Exception, SystemError):
                cur_wire = last_wire_bytes
            iv_wire = max(0, cur_wire - last_wire_bytes)
            last_wire_bytes = cur_wire
            ring_depth_sum = 0
            ring_depth_max = 0
            n_streams = 0
            try:
                if quic is not None:
                    for sid in list(quic._stream_ctxs):
                        st = quic.get_stream_buf_stats(sid)
                        if st is None:
                            continue
                        pushed, popped = st[0], st[1]
                        depth = max(0, pushed - popped)
                        ring_depth_sum += depth
                        if depth > ring_depth_max:
                            ring_depth_max = depth
                        n_streams += 1
            except (Exception, SystemError):
                pass
            _post(events_queue, {
                'kind': 'pub_stats',
                't': time.monotonic(),
                'tx_bytes': iv_bytes,
                'tx_objs': iv_objs,
                'wire_bytes': iv_wire,
                'cumulative_bytes': cur_bytes,
                'cumulative_objs': cur_objs,
                'cumulative_wire_bytes': cur_wire,
                'tx_ring_depth_bytes': ring_depth_sum,
                'tx_ring_depth_max_bytes': ring_depth_max,
                'tx_ring_streams': n_streams,
            })

    server = MOQTServer(
        host=config.get('host', 'localhost'),
        port=config['port'],
        certificate=config['cert'],
        private_key=config['key'],
        path=config.get('path', ''),
        draft_version=config.get('draft'),
    )
    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    await server.serve()
    _post(events_queue, {
        'kind': 'pub_health', 'state': 'published',
        't': time.monotonic(),
    })

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
        try:
            server.close()
        except Exception:
            pass

    _post(events_queue, {
        'kind': 'pub_health', 'state': 'closed',
        't': time.monotonic(),
    })


def loopback_server_entry(config: Dict[str, Any], mp_stop_event,
                           rate_queue, events_queue):
    """Process entrypoint. Spawned via multiprocessing.Process for
    --mp-loopback mode (adaptive_bench)."""
    _setup_quiet_logging(config.get('logdir'), 'loopback-server',
                         config.get('debug', False))
    if not config.get('no_uvloop', False):
        _try_install_uvloop()
    try:
        asyncio.run(_loopback_server_task(config, mp_stop_event,
                                            rate_queue, events_queue))
    except KeyboardInterrupt:
        pass
