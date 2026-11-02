"""Shared multiprocessing worker for bench tools.

A subscriber worker runs one MOQTClient in its own process and posts
two message types to a multiprocessing.Queue:

  {"kind": "stats", "sub_id": N, **TrackStats.snapshot()}   — every 1 s
    (deltas over "dt"; windowed latency from "lat_n" samples)

  {"kind": "health", "sub_id": N, "state": "subscribed" |
    "subscribe_failed" | "stream_reset" | "closed",
    "detail": "...", "t"}                   — on state transitions

Each worker is GIL-isolated. Parent never shares Python objects with
children except the queue and stop_event. Config is a picklable dict.

Consumers: aiomoqt.tools.adaptive_bench (ramps subscriber count
dynamically) and aiomoqt.tools.load_sim (matrix + churn simulation).
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import signal
import sys
import time
from collections import deque
from typing import Any, Dict

from aiomoqt.client import MOQTClient
from aiomoqt.messages.base import MOQTMessage
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.track import SubscribedTrack, TrackState
from aiomoqt.types import FilterType
from aiomoqt.utils.url import parse_relay_url
from aiomoqt.utils.logger import set_log_handler, set_log_level
from aiomoqt.utils.stats import TrackStats


def apply_compat(compat) -> bool:
    """Apply a comma-separated compat string in this worker PROCESS.
    Sets the process-global decode/session tolerance flags; returns
    True when the client should be built with libquicr_compat. Mirrors
    moq_interop_client's --compat keys."""
    keys = {k.strip() for k in (compat or "").split(",") if k.strip()}
    if "all" in keys or "lenient-extensions" in keys:
        MOQTMessage._tolerate_trailing_extensions = True
    if "all" in keys or "missing-request-credit" in keys:
        _MOQTSessionMixin._tolerate_missing_request_credit = True
    return "all" in keys or "libquicr" in keys


SUBSCRIBE_RETRY_WINDOW_S = 30.0
SUBSCRIBE_EACH_TIMEOUT_S = 5.0
STATS_INTERVAL_S = 1.0
STOP_POLL_S = 0.1
SELF_HEAL_BACKOFF_S = 0.5
# A slot's delivery is judged this long after its (re)subscribe, and
# counts while it receives at least DELIVERING_FLOOR of its expected rate.
SLOT_GRACE_S = 3.0
DELIVERING_FLOOR = 0.5

# Spawn context for all bench parents. Linux pins "fork": Python 3.14
# switched the Linux default to forkserver, whose by-name SemLock reopen
# in the child races parent-side queue lifetime (FileNotFoundError in
# synchronize.__setstate__). Bench parents are pure orchestrators — no
# QUIC/C threads exist pre-fork, so fork is safe. Other platforms keep
# their platform default (macOS: spawn, where fork is the unsafe choice).
if sys.platform == "linux":
    MP_CTX = multiprocessing.get_context("fork")
else:
    MP_CTX = multiprocessing.get_context()


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
                            format='%(asctime)s %(levelname)s %(name)s: %(message)s')
        # aiomoqt loggers do not propagate; give them the file handler too.
        set_log_handler(logging.getLogger().handlers[0])
        set_log_level(lvl)
    else:
        lvl = logging.DEBUG if debug else logging.WARNING
        logging.basicConfig(stream=sys.stderr, force=True, level=lvl,
                            format=f'[{name}] %(asctime)s %(levelname)s %(message)s')
        set_log_level(lvl)


def _ignore_sigint():
    """The parent stops workers by event; Ctrl-C goes to the parent only."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


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


def _transport_options(config: Dict[str, Any]) -> Dict[str, Any]:
    """Forward explicit transport settings, retaining None to disable a budget."""
    return {key: config[key] for key in (
        'congestion_control_algorithm', 'tx_max_queued_bytes',
        'tx_max_inflight_bytes', 'keep_alive_interval',
    ) if key in config}


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
        supported_drafts=config.get('draft'),
        keylog_filename=config.get('keylogfile'),
        libquicr_compat=apply_compat(config.get('compat')),
        **_transport_options(config),
    )

    stop_ev = _bridge_stop_event(mp_stop_event)
    stats = TrackStats()

    def _on_object(msg, size_bytes, recv_time_us, group_id=None,
                   subgroup_id=None, *_args, **_kw):
        stats.on_object(msg, size_bytes, recv_time_us, group_id,
                        subgroup_id)

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
    _ignore_sigint()
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
    a short backoff. Runs until stop_ev. The per-slot TrackStats is
    passed in and persists across reopens, so a transient drop doesn't
    reset the batch's cumulative totals or latency window.

    state['active'] reflects whether this slot is currently subscribed;
    state['since'] is the time of its latest subscribe.
    """
    if initial_delay > 0:
        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=initial_delay)
            return
        except asyncio.TimeoutError:
            pass

    def _on_object(msg, size_bytes, recv_time_us, group_id=None,
                   subgroup_id=None, *_args, **_kw):
        stats.on_object(msg, size_bytes, recv_time_us, group_id,
                        subgroup_id)

    insecure = config.get('insecure', False)
    while not stop_ev.is_set():
        state['active'] = False
        try:
            client = MOQTClient(
                relay.host, relay.port,
                path=relay.path or "",
                use_quic=relay.use_quic,
                verify_tls=not insecure,
                supported_drafts=config.get('draft'),
                libquicr_compat=apply_compat(config.get('compat')),
                **_transport_options(config),
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
                    state['since'] = time.monotonic()
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


def slot_delivering(hist, since: float, now: float, window_s: float,
                    expected_bps: float, grace_s: float = SLOT_GRACE_S) -> bool:
    """Whether a subscribed slot received at least DELIVERING_FLOOR of
    expected_bps (bytes/s) over the last window_s, counted from its latest
    subscribe. hist holds (t, cumulative bytes) samples, oldest first. A
    slot inside its grace is not delivering yet."""
    if now - since < grace_s:
        return False
    start = max(since, now - window_s)
    base = next(((t, b) for t, b in hist if t >= start), None)
    if base is None or not hist:
        return False
    t_last, b_last = hist[-1]
    elapsed = t_last - base[0]
    if elapsed <= 0:
        return False
    return (b_last - base[1]) / elapsed >= DELIVERING_FLOOR * expected_bps


async def _subscriber_batch_task(config: Dict[str, Any], mp_stop_event,
                                 events_queue):
    """Host config['batch_size'] subscriptions on one event loop, each
    self-healing via _slot_supervisor. Posts one aggregate batch_stats
    event per STATS_INTERVAL_S:

      {"kind": "batch_stats", "worker_id": W, "batch_size": K,
       "active": <subscribed slots>, "delivering": <slots at rate>,
       "t", "dt", "rx_bytes", "rx_objs" (deltas over dt),
       "lat_n", "lat_p90_ms", "lat_mean_ms" (worst slot with samples),
       "jitter_ms", "loss", "total_bytes", "total_objs"}

    A slot is delivering per slot_delivering() against config
    'per_sub_mbps' over config 'keep_window' seconds; without
    per_sub_mbps every subscribed slot counts.
    """
    worker_id = config['worker_id']
    K = max(1, int(config.get('batch_size', 1)))
    stagger = float(config.get('stagger', 0.0))
    relay = parse_relay_url(config['relay_url'],
                            force_quic=config.get('force_quic', False))
    stop_ev = _bridge_stop_event(mp_stop_event)

    per_sub_mbps = config.get('per_sub_mbps') or 0.0
    expected_bps = per_sub_mbps * 1e6 / 8
    window_s = float(config.get('keep_window', 3.0))

    # (stats, state) per slot; stats persist across a slot's reopens.
    slots = [(TrackStats(), {'active': False, 'since': 0.0,
                             'hist': deque()})
             for _ in range(K)]

    async def _batch_stats_loop():
        last_t = time.monotonic()
        while not stop_ev.is_set():
            await asyncio.sleep(STATS_INTERVAL_S)
            now = time.monotonic()
            agg_bytes = agg_objs = agg_iv_lost = 0
            cum_loss = total_bytes = total_objs = 0
            worst_p90 = worst_mean = None
            lat_n = 0
            jitter_sum = 0.0
            jitter_n = 0
            active = delivering = 0
            for stats, state in slots:
                snap = stats.snapshot()
                agg_bytes += snap['rx_bytes']
                agg_objs += snap['rx_objs']
                agg_iv_lost += snap['iv_lost']
                cum_loss += snap['loss']
                total_bytes += snap['total_bytes']
                total_objs += snap['total_objs']
                if snap['lat_n']:
                    lat_n += snap['lat_n']
                    p90, mean = snap['lat_p90_ms'], snap['lat_mean_ms']
                    worst_p90 = p90 if worst_p90 is None else max(worst_p90, p90)
                    worst_mean = (mean if worst_mean is None
                                  else max(worst_mean, mean))
                jitter_sum += snap['jitter_ms']
                jitter_n += 1
                hist = state['hist']
                hist.append((now, snap['total_bytes']))
                while hist and hist[0][0] < now - window_s - STATS_INTERVAL_S:
                    hist.popleft()
                if state['active']:
                    active += 1
                    if not expected_bps or slot_delivering(
                            hist, state['since'], now, window_s,
                            expected_bps):
                        delivering += 1
            _post(events_queue, {
                'kind': 'batch_stats', 'worker_id': worker_id,
                'batch_size': K, 'active': active,
                'delivering': delivering,
                't': now, 'dt': now - last_t,
                'rx_bytes': agg_bytes, 'rx_objs': agg_objs,
                'iv_lost': agg_iv_lost, 'loss': cum_loss,
                'lat_n': lat_n,
                'lat_p90_ms': 0.0 if worst_p90 is None else worst_p90,
                'lat_mean_ms': 0.0 if worst_mean is None else worst_mean,
                'jitter_ms': jitter_sum / jitter_n if jitter_n else 0.0,
                'total_bytes': total_bytes, 'total_objs': total_objs,
            })
            last_t = now

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
    _ignore_sigint()
    try:
        asyncio.run(_subscriber_batch_task(config, mp_stop_event,
                                           events_queue))
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Publisher worker — for adaptive_bench BW mode + (eventually) loopback bench
# ---------------------------------------------------------------------------

def bench_track(session, config: Dict[str, Any], *, rate: float,
                num_subgroups: int, **kwargs):
    """The bench publisher's track: a VideoTrack when config names a
    'video' profile ('video_fps', and 'group_size' as the GOP), else
    object_size x num_subgroups at `rate`."""
    from aiomoqt.track import PublishedTrack, VideoTrack
    if config.get('video'):
        return VideoTrack(session, config['namespace'], config['trackname'],
                          resolution=config['video'],
                          fps=config['video_fps'],
                          gop_size=config['group_size'])
    return PublishedTrack(session, namespace=config['namespace'],
                          trackname=config['trackname'],
                          object_size=config['object_size'],
                          group_size=config.get('group_size', 10000),
                          num_subgroups=num_subgroups, rate=rate, **kwargs)


async def _publisher_task(config: Dict[str, Any], mp_stop_event,
                          rate_queue, events_queue):
    """Connect to relay, publish a track, drive paced object generation
    at a rate controllable from the parent via rate_queue.

    rate_queue messages: {'rate_ops': float}  (objects/sec aggregate)
    events_queue stats: {'kind':'pub_stats', 't', 'tx_bytes', 'tx_objs',
                          'cumulative_bytes', 'cumulative_objs'} every 1s.
    """
    relay = parse_relay_url(config['relay_url'],
                            force_quic=config.get('force_quic', False))
    client = MOQTClient(
        relay.host, relay.port,
        path=relay.path or "",
        use_quic=relay.use_quic,
        verify_tls=not config.get('insecure', False),
        supported_drafts=config.get('draft'),
        keylog_filename=config.get('keylogfile'),
        libquicr_compat=apply_compat(config.get('compat')),
        **_transport_options(config),
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
            from aiomoqt.types import ForwardingPreference
            _fwd = (ForwardingPreference.DATAGRAM
                    if config.get('delivery') == 'datagram'
                    else ForwardingPreference.SUBGROUP)
            track = bench_track(
                session, config,
                rate=config.get('initial_rate_ops', 0.0),
                num_subgroups=_num_sg,
                forwarding=_fwd,
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
    _ignore_sigint()
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

    stop_ev = _bridge_stop_event(mp_stop_event)
    _num_sg = max(1, config.get('num_subgroups', 1))
    initial_rate = config.get('initial_rate_ops', 0.0)
    track_holder: Dict[str, Any] = {'track': None}

    async def _on_subscribe(session, msg):
        track = bench_track(session, config, rate=initial_rate,
                            num_subgroups=_num_sg)
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
        supported_drafts=config.get('draft'),
        **_transport_options(config),
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
    _ignore_sigint()
    try:
        asyncio.run(_loopback_server_task(config, mp_stop_event,
                                            rate_queue, events_queue))
    except KeyboardInterrupt:
        pass
