#!/usr/bin/env python3
"""aiomoqt-relay: a MoQT relay built on aiomoqt.

Relays raw QUIC and WebTransport sessions at drafts 14, 16, 18, 20 and
22. It runs the public aiomoqt-relay, and moxygen's moqtest conformance
suite and the interop runner test against it. Written in Python, it
carries less traffic than the Rust and C++ relays.

Limits:

  * No authentication, authorization or rate limiting.
  * A per-stream send budget (--max-inflight-bytes) and a send lane per
    subscriber holding at most LANE_BYTES, counting its queued live
    objects and what its streams still hold in the transport: a
    subscriber that falls further behind loses whole groups, their
    streams reset.
  * Per session, at most --max-namespaces-per-session namespace requests
    (PUBLISH_NAMESPACE, SUBSCRIBE_NAMESPACE, SUBSCRIBE_TRACKS) and
    --max-subscriptions-per-session SUBSCRIBEs held; past either, a
    request is refused with EXCESSIVE_LOAD (INTERNAL_ERROR at d14/d16).
  * A bounded recent-object cache per track (CACHE_GROUPS / CACHE_BYTES,
    oldest group dropped) under a relay-wide budget (--cache-bytes),
    which drops the oldest group of the least recently used track, never
    the live group of a subscribed one: enough to answer a FETCH,
    standalone or joining, over what it still holds; a late subscriber
    starts at the live edge. Past CACHE_BUDGET_BYTES + CACHE_BYTES the
    largest live group is dropped and the rest of it goes uncached. A
    replay owed objects the cache no longer holds resets that subgroup
    stream rather than FIN it.
    Cache and lanes charge each object its payload, its properties and
    a per-record overhead (retained_object_bytes).
  * Upstream Forward State is raised for a forwarding subscriber and
    lowered again once none has forwarded for --pause-after seconds;
    a --catalog-probe catalog keeps flowing.
  * No delivery-timeout enforcement.
  * Namespace tables are in memory and global to the process.

Routing model (cross-session, single relay instance):
  - PUBLISH_NAMESPACE: record the announcing session under the
    namespace tuple, respond with the protocol's default RequestOk
    (d16+) / PublishNamespaceOk (d14).
  - SUBSCRIBE: find publishers that announced the namespace or a prefix
    of it (§2.4 Namespace Prefix Matching), subscribe upstream once per
    Full Track Name, answer SUBSCRIBE_OK and fan the objects out to
    every downstream subscriber of that track. With no match, send
    SUBSCRIBE_ERROR / REQUEST_ERROR with TRACK_DOES_NOT_EXIST (d14 code
    0x04, d16+ code 0x10) so a conformance suite sees a spec-correct
    rejection.
  - PUBLISH_NAMESPACE_DONE: drop that session's hold on the namespace.

Run on UDP/4443 with the runner's /certs convention:

  python -m aiomoqt.tools.moq_interop_relay \\
      --port 4443 \\
      --cert /certs/cert.pem --key /certs/priv.key

Transports: WebTransport by default, raw QUIC with `--quic`, or BOTH
on one port with `--dual` (per-connection ALPN dispatch via aiopquic
serve_dispatch). `--quic-port N` remains as the legacy two-listener
arrangement (second raw-QUIC listener sharing the global namespace
table) for runners that expect distinct endpoints.

Stopping: SIGTERM or SIGINT drains. The relay refuses new sessions until
it exits, sends GOAWAY to every session (New Session URI from
--goaway-uri, else empty: reconnect to this address), waits up to
--drain-timeout seconds for them to leave, closes the rest with
GOAWAY_TIMEOUT and exits once the closes have left. A second signal
exits at once.
"""

import argparse
import asyncio
from collections import OrderedDict, deque
import contextlib
import datetime
import faulthandler
import itertools
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
import time
import tomllib
from collections import Counter
from types import SimpleNamespace

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer, _listen_desc
from aiomoqt.types import (
    MOQT_DEFAULT_DRAFTS, D18MessageType, FilterType, GroupOrder, MOQTDraft,
    MOQTMessageType, MOQTRequestError, MOQTStreamStopped, ObjectStatus,
    ParamType,
    RequestErrorCode, SCHEDULING, SessionCloseCode, StreamResetCode,
    SubscribeDoneCode,
    parse_draft_spec, to_stream_priority,
)
from aiomoqt.delivery import retained_object_bytes
from aiomoqt.messages import FetchHeader, SubgroupHeader
from aiomoqt.messages.base import MOQTMessage
from aiomoqt.messages.data import (FETCH_FLAGS_END_TIMED_OUT, FetchObject,
                                   ObjectDatagram, ObjectDatagramStatus)
from aiomoqt.messages.fetch import _is_joining
from aiomoqt.messages.params import (RANGE_FILTERS, U64_MAX, D20ParamType,
                                     D20RequestErrorCode,
                                     LocationFilter, LocationFilterType,
                                     RangeFilter, RangeFilters, fill_range,
                                     merge_range_filters, range_filter_count,
                                     uses_location_filter)
from aiomoqt.messages.publish import Publish, PublishError
from aiomoqt.messages.request import RequestError, RequestOk
from aiomoqt.messages.subscribe import Subscribe, SubscribeOk
from aiomoqt.track import (PublishedTrack, SubscribedTrack, _UNFILTERED,
                           _filter_window, _largest_object_param,
                           _location_window, _subscription_window)
from aiomoqt.context import (canonical_request_error, canonical_stream_reset,
                             is_draft16_or_later,
                             request_error_code)
from aiomoqt.tools import relay_status
from aiomoqt import __version__ as AIOMOQT_VERSION
from aiomoqt.utils.logger import (get_category_logger, get_log_level,
                                  set_log_handler, set_log_level,
                                  step_log_level)
from aiomoqt.utils.buffer import Buffer, BufferReadError
from aiomoqt.utils.cli import add_help, add_logging, apply_logging
from aiomoqt.utils.url import parse_relay_url

# Version confinement. The interop runner injects DRAFT (moq-interop-runner
# PR #95) — or older MOQT_DRAFT — to pin the relay to one draft; the client
# is pinned the same way, so negotiation lands on that draft. When neither is
# set (the open-relay context, where clients offer their full version list),
# advertise the library's default drafts, highest first.
_RELAY_DRAFT_DEFAULT = (
    os.environ.get("DRAFT") or os.environ.get("MOQT_DRAFT")
    or ",".join(str(d) for d in MOQT_DEFAULT_DRAFTS)).strip()

logger = get_category_logger("relay")
control_log = get_category_logger("control")


# Cross-session announcement table: namespace tuple -> the publisher
# sessions currently advertising it. A session may announce a namespace
# more than once (sub-tests re-announce), so each session is held with a
# refcount and drops out when it reaches zero or the session closes.
_announced: dict[tuple, dict] = {}
# (id(session), request_id) -> namespace of each PUBLISH_NAMESPACE still
# held, so a withdrawal naming only the request finds its namespace.
_announce_requests: dict[tuple, tuple] = {}

# Tracks being relayed upstream->downstream, keyed by (namespace, name).
_tracks: dict[tuple, "_RelayedTrack"] = {}
# (namespace, name) -> the upstream SUBSCRIBE outstanding for it.
_establishing: dict[tuple, "_Establishing"] = {}
# (namespace, name) -> Forward State of each SUBSCRIBE held for a publisher.
_held_forwards: dict[tuple, list[bool]] = {}

# Sessions this relay dialled OUT to. An origin does not announce to us,
# so it never lands in _announced; it is tried as a fallback when no
# inbound publisher covers a namespace. Without this the relay is only
# ever a server, and the whole client leg — SETUP, SUBSCRIBE and object
# receive as a client, upstream disconnect — goes untested.
_upstreams: list = []
# Upstream URLs the relay was asked to dial; an empty list means there is
# nothing to wait for.
_upstream_urls: list = []

# Redial delay for a lost upstream. Short: a subscriber arriving while
# the relay has no upstream is answered with an error, so the window
# matters more than the reconnect cost.
FETCH_RELATIVE_JOINING = 0x2
UPSTREAM_REDIAL_S = 0.5
# Longest a terminal waits for upstream streams the PUBLISH_DONE counts.
TERMINAL_GRACE_S = 3.0
# How long a SUBSCRIBE waits for an upstream to (re)appear before the
# relay answers "track does not exist".
UPSTREAM_WAIT_S = 3.0
# d18 SUBSCRIBE parameter (§10.2.6); 0x04 is MAX_CACHE_DURATION before d18.
RENDEZVOUS_TIMEOUT = 0x04
# Longest a SUBSCRIBE is held for a publisher, whatever it asks for.
RENDEZVOUS_MAX_S = 30.0
# Longest a joining FETCH waits for the SUBSCRIBE it names to attach.
JOINING_WAIT_S = 5.0
# A REQUEST_UPDATE to a SUBSCRIBE still pending upstream waits for its
# answer, no longer than the SUBSCRIBE itself can be held.
HELD_UPDATE_MAX_S = RENDEZVOUS_MAX_S + UPSTREAM_WAIT_S
HELD_UPDATE_POLL_S = 0.01
# Longest the relay waits for a PUBLISH after an upstream refuses its
# SUBSCRIBE as a duplicate subscription (§5.1: the PUBLISH crossed it).
PUBLISH_WAIT_S = 1.5
# Longest a fill waits on an upstream FETCH; caps FILL_TIMEOUT (§9.20.5).
FILL_UPSTREAM_S = 10.0
# Longest a SUBSCRIBE waits on an upstream FETCH for the group head the
# upstream subscription did not deliver.
BACKFILL_S = 2.0
# Longest a SUBSCRIBE that resumes a paused upstream waits for its
# REQUEST_OK and the publisher's Largest.
RESUME_REPLY_S = 1.0
# Watchdog: self-probes failed in a row before exiting, the exit status
# (EX_SOFTWARE), and the longest one probe may take.
WATCHDOG_MISSES = 3
WATCHDOG_EXIT = 70
WATCHDOG_PROBE_S = 10.0
# The probe's SETUP implementation prefix; the relay logs its sessions at
# DEBUG. One probe in WATCHDOG_FULL_EVERY relays an object; the rest only
# complete SETUP. Probe results are summarised every WATCHDOG_REPORT_S.
WATCHDOG_IMPL = "aiomoqt-watchdog/"
WATCHDOG_FULL_EVERY = 4
WATCHDOG_REPORT_S = 3600.0
# After a failed probe, how long to let a woken reader drain before
# reading the transport counters again.
WATCHDOG_KICK_S = 0.5
# Dashboard (--status-dir): write period and history kept.
STATUS_INTERVAL_S = 2.0
STATUS_HISTORY_S = 600
# platform.json is mostly history; its page tolerates a slower refresh.
PLATFORM_INTERVAL_S = 5.0
# MSF catalog track name (§5.2.3), and the watchdog's own namespace,
# kept off the dashboard.
CATALOG_TRACK = b"catalog"
WATCHDOG_NS = b"aiomoqt-watchdog"
# DEFAULT_PUBLISHER_GROUP_ORDER (§10.5), decoded into group_order.
DEFAULT_GROUP_ORDER_PROPERTY = 0x22
# --catalog-probe: subscribe to an announced namespace's catalog after each
# delay until one succeeds; interop test namespaces ("moq-test...") are skipped.
CATALOG_PROBE_DELAYS_S = (0.5, 2.0)
CATALOG_PROBE_SKIP = b"moq-test"
_catalog_probe = False
# --pause-after: seconds a pulled track may go with no forwarding
# subscriber before its upstream Forward State drops to 0; 0 = never.
PAUSE_AFTER_S = 60.0
_pause_after = PAUSE_AFTER_S
_pause_sweep = None


async def _await_upstream(timeout: float = None) -> bool:
    """Wait for a dialled upstream to (re)connect."""
    deadline = asyncio.get_running_loop().time() + (
        UPSTREAM_WAIT_S if timeout is None else timeout)
    while not _upstreams:
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _dial_upstream(url: str, draft) -> None:
    """Hold a session open to an upstream origin for the process's life."""
    _upstream_urls.append(url)
    ep = parse_relay_url(url)
    # An upstream with no subscribers sends nothing, and an origin will
    # close a silent connection on its idle timeout: PING keeps it.
    client = MOQTClient(ep.host, ep.port, path=ep.path,
                        use_quic=ep.use_quic, verify_tls=False,
                        supported_drafts=draft, keep_alive_interval=10)
    while True:
        try:
            async with client.connect() as session:
                await session.client_session_init()
                _upstreams.append(session)
                logger.info(f"relay: upstream connected {url} "
                            f"draft={session.negotiated_draft}")
                try:
                    await session.async_closed()
                finally:
                    if session in _upstreams:
                        _upstreams.remove(session)
        except Exception as e:
            logger.info(f"relay: upstream {url} unavailable: {e}")
        logger.info(f"relay: retrying upstream {url} in "
                    f"{UPSTREAM_REDIAL_S}s")
        await asyncio.sleep(UPSTREAM_REDIAL_S)


def _announced_match(ns: tuple) -> list[tuple]:
    """Announced namespaces covering `ns`, longest (most specific) first.

    Namespace Prefix Matching (transport §2.4): fields are compared
    sequentially and each must match exactly; an announcement with the
    same or fewer fields than the request qualifies. So announcing
    (foo) covers a SUBSCRIBE for (foo, bar), while (foobar) does not —
    which is why this compares tuple fields and never a joined string.
    """
    hits = [a for a in _announced
            if len(a) <= len(ns) and ns[:len(a)] == a]
    return sorted(hits, key=len, reverse=True)


def _track_live(track) -> bool:
    """A track is usable while it is still producing and the session
    feeding it is still open.

    Liveness is not session-scoped alone: a track ends while its session
    stays open, and a peer's connection can linger for its whole idle
    timeout after it is done. Acking a finished track to a new
    subscriber delivers nothing.
    """
    if track.finished:
        return False
    if track.pending_publish is not None:
        return _session_live(track.pending_publish[0])
    return _session_live(_upstream_session(track))


def _upstream_session(track):
    """The session objects arrive on: the publisher for a bare PUBLISH,
    or the session the upstream subscription was made over."""
    up = track.upstream
    if up is None:
        return None
    return up if hasattr(up, "_moqt_session_closed") else getattr(
        up, "session", None)


def _session_live(session) -> bool:
    """False once the session has closed. A relay outlives its sessions,
    so anything cached against one has to be re-checked before reuse."""
    if session is None:
        return False
    fut = getattr(session, "_moqt_session_closed", None)
    return not (fut is not None and fut.done())


def _cancelling() -> bool:
    """True when the running task itself is being cancelled. A write to a
    session that closed under it raises CancelledError too, and that
    loses one subscriber, not the fan-out."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _publishers_for(ns: tuple) -> list:
    """Publisher sessions that announced `ns` or a prefix of it."""
    out, seen = [], set()
    for a in _announced_match(ns):
        # Newest announcement first: a publisher that just announced is
        # the current authority, and a predecessor that has gone away may
        # not have been observed as closed yet. Trying it first would
        # stall the subscriber for the whole upstream timeout.
        for sess in reversed(list(_announced[a])):
            if not _session_live(sess):
                _forget_session(sess)
                continue
            if id(sess) not in seen:
                seen.add(id(sess))
                out.append(sess)
    # An origin we dialled advertises nothing to us, so ask it last:
    # _establish_upstream tries candidates in turn and moves on when one
    # does not serve the track.
    for sess in _upstreams:
        if id(sess) not in seen:
            seen.add(id(sess))
            out.append(sess)
    return out


def _fetch_sources(ns: tuple, track_name) -> list:
    """Sessions to FETCH a track from: the one feeding it, which for a
    bare PUBLISH is the only one, then the publishers of `ns`."""
    name = (track_name.encode() if isinstance(track_name, str)
            else bytes(track_name))
    track = _tracks.get((tuple(ns), name))
    owner = _track_owner(track) if track is not None else None
    pubs = _publishers_for(ns)
    if not _session_live(owner) or any(p is owner for p in pubs):
        return pubs
    return [owner, *pubs]


# Recent-object cache bounds, per track: whichever is hit first.
CACHE_GROUPS = 8
CACHE_BYTES = 32 * 1024 * 1024
# Relay-wide cache budget (--cache-bytes).
CACHE_BUDGET_BYTES = 256 * 1024 * 1024

# Bytes a subscriber's send lane holds, queued live objects plus what its
# streams hold in the transport, before it sheds groups.
LANE_BYTES = 4 * 1024 * 1024
# Live offers between reads of a lane's transport backlog.
LANE_TX_PROBE = 8
# Seconds between a subscriber's shed log lines after its first.
SHED_LOG_S = 10.0
# Seconds a lane with work completes nothing before it is logged stalled.
LANE_STALL_S = 10.0
# Requests one session may hold open; see the module docstring.
MAX_NAMESPACES_PER_SESSION = 256
MAX_SUBSCRIPTIONS_PER_SESSION = 1024
# Retry Interval of a cap refusal: milliseconds before a retry, plus one.
CAP_RETRY_MS = 1000
# id(session) -> its SUBSCRIBE handlers still running.
_subscribing: Counter = Counter()
# (id(session), cap) already warned about.
_cap_warned: set = set()

# Tiebreak within a data stream band (--stream-scheduling).
STREAM_SCHEDULING = "fifo"


def _stream_band(session, priority) -> int:
    """Transport band for a downstream data stream of this publisher
    priority."""
    if priority is None or not 0 <= priority <= 255:
        priority = 128
    return to_stream_priority(priority, discipline=getattr(
        session, 'stream_scheduling', STREAM_SCHEDULING))


def _no_tx(_stream_id) -> int:
    return 0


def _stream_budget(session):
    """The per-stream send budget (--max-inflight-bytes)."""
    return getattr(getattr(session, "_session", None),
                   "tx_max_inflight_bytes", None)


def _conn_gate(session):
    """The connection's TX gate on new streams (tx_max_queued_bytes)."""
    quic = getattr(session, "_quic", None)
    gate = getattr(quic, "tx_max_queued_bytes", None)
    if gate is None:
        gate = getattr(getattr(quic, "configuration", None),
                       "tx_max_queued_bytes", None)
    return gate


def _kib(n) -> str:
    if not n:
        return "none"
    return f"{n >> 20} MiB" if n % (1 << 20) == 0 else f"{n >> 10} KiB"


def _check_lanes() -> None:
    now = time.monotonic()
    for lane in list(_lanes):
        lane.check(now)


async def _watch_lanes() -> None:
    """Check every send lane for a stall, a few times per LANE_STALL_S."""
    while True:
        await asyncio.sleep(min(STATUS_INTERVAL_S, LANE_STALL_S / 4))
        try:
            _check_lanes()
        except Exception:
            logger.debug("relay: lane check failed", exc_info=True)


def _tx_used(session):
    """The session's per-stream count of bytes queued in the transport."""
    quic = getattr(session, "_quic", None)
    return getattr(quic, "tx_data_ring_used", None) or _no_tx


# MAX_FILTER_RANGES advertised at d20+ (§9.1.6).
MAX_FILTER_RANGES = 16

# Admits no object: a track the request's TRACK_PROPERTY_FILTERs exclude.
_REJECT_ALL = RangeFilters(((D20ParamType.OBJECTID_FILTER, RangeFilter()),),)


class _Downstream:
    """One downstream subscription to a relayed track."""

    __slots__ = ("session", "alias", "request_id", "forward", "window",
                 "joined_at", "replaying", "location_filter", "group_order",
                 "fills", "range_params", "filters", "lane", "lost",
                 "fence")

    def __init__(self, session, alias, request_id, forward=True,
                 window=_UNFILTERED):
        self.session = session
        self.alias = alias
        self.request_id = request_id
        # Forward State (§5.1): objects flow only while True.
        self.forward = forward
        # Filter window (§5.1.2): (start Location, End Group or None[,
        # last Object of the End Group]).
        self.window = window
        # Joining Location (§5.1): Largest when Forward State became 1.
        self.joined_at = None
        # Waiting for the cache replay queued for it; live objects skip it
        # until then and reach it through the replay.
        self.replaying = False
        # d20+ Location Filter and group order, for fill ranges (§3.4).
        self.location_filter = None
        self.group_order = None
        # Fill fetch stream tasks; cancelling one resets its stream.
        self.fills: set = set()
        # Range Filter parameters in effect (§3.3.2), and their compiled
        # form for this track; None passes every object.
        self.range_params: dict = {}
        self.filters = None
        # Its _SendLane, created with the first object for it.
        self.lane = None
        # Groups whose replay lacked objects it was owed: their streams
        # end in a reset.
        self.lost = ()
        # The track's fence when it joined: its joining FETCH and fills
        # start after that group.
        self.fence = None

    def set_filters(self, params, track_properties) -> None:
        self.range_params = params
        compiled = RangeFilters.from_params(params).for_track(
            track_properties)
        self.filters = (_REJECT_ALL if compiled is None
                        else compiled or None)

    def admits(self, sgid, oid, prio, exts) -> bool:
        """The object passes the Range Filters."""
        return self.filters is None or self.filters.admits(
            sgid, oid, prio, exts)

    def passes(self, group_id, object_id) -> bool:
        """Forward State 1 and inside the filter window."""
        window = self.window
        if not self.forward or (group_id, object_id) < window[0]:
            return False
        end_group = window[1]
        if end_group is None or group_id < end_group:
            return True
        return group_id == end_group and (
            len(window) < 3 or object_id <= window[2])

    def past_end(self, group_id) -> bool:
        end_group = self.window[1]
        return end_group is not None and group_id > end_group

    @property
    def start(self):
        """Start Location; {0, 0} when unfiltered (d18 §5.1.2)."""
        return self.window[0]


class _CacheBudget:
    """Cache bytes across all tracks, held to CACHE_BUDGET_BYTES by
    dropping the oldest group of the least recently used track."""

    def __init__(self) -> None:
        # Tracks holding cached objects, least recently used first.
        self.lru: OrderedDict = OrderedDict()
        self.bytes = 0
        self.evictions = 0
        # Live groups dropped at the hard ceiling.
        self.truncations = 0
        self._overrun = False

    def touch(self, track) -> None:
        """`track`'s cache was written, replayed or fetched from."""
        if track._cache:
            self.lru[track] = None
            self.lru.move_to_end(track)

    def forget(self, track) -> None:
        self.lru.pop(track, None)

    def enforce(self) -> None:
        while self.bytes > CACHE_BUDGET_BYTES:
            victim = next((t for t in self.lru if t._evictable()), None)
            if victim is None and self.bytes > CACHE_BUDGET_BYTES + CACHE_BYTES:
                largest = max(self.lru, key=lambda t: t._cache_bytes,
                              default=None)
                if largest is not None:
                    largest._truncate_live()
                    self.truncations += 1
                    continue
            if victim is None:
                if not self._overrun:
                    self._overrun = True
                    logger.warning(
                        f"relay: cache {self.bytes} bytes over its "
                        f"{CACHE_BUDGET_BYTES}-byte budget: the rest is "
                        f"subscribed tracks' live groups")
                return
            victim._drop_oldest_group()
            self.evictions += 1
        self._overrun = False


_budget = _CacheBudget()

# Send lanes still running, including those draining past their track.
_lanes: set = set()


class _SendLane:
    """One subscriber's objects behind its own FIFO and writer task.

    The track hands each object to every lane without waiting, so a
    subscriber held up by its transport holds up only itself. Past
    LANE_BYTES of live objects the lane sheds whole groups: their open
    subgroup streams are reset, never FINed short, and delivery resumes
    at a newer group.
    """

    def __init__(self, track, d):
        self.track = track
        self.d = d
        # (gid, size, live, sgid, oid, payload, exts, prio, status, shape,
        # encoded), or ("CUT",) / ("CLOSE",) / ("FINAL", code, None, reason).
        self.items: deque = deque()
        self.bytes = 0           # live object bytes queued
        self.replay_bytes = 0    # cache replay bytes queued, not bounded
        self.tx_bytes = 0        # held by the transport, as last read
        self._probe_gid = None
        self._probe_n = 0
        self.objects = 0
        self.floor = None        # objects of groups below it are shed
        self.shed = 0            # objects shed
        self.sheds = 0           # shed episodes
        self._logged_at = None
        self._logged_sheds = 0
        self.shedding = False
        self.final = False       # PUBLISH_DONE queued: takes nothing more
        self.busy = False        # a write is in progress
        self.opening = False     # that write opens a subgroup stream
        self.parked = None       # what that write awaits: header or object
        self.sid = None          # and on which stream
        self.done = 0            # items completed
        self.stalls = 0          # stall episodes
        self._seen = 0
        self._moved_at = time.monotonic()
        self._stalled = False
        self.stopped = False
        self._cut = False
        self._closing = False
        self._wake = asyncio.Event()
        _lanes.add(self)
        track._lanes.add(self)
        self.task = asyncio.ensure_future(self._run())

    @property
    def idle(self) -> bool:
        return not self.items and not self.busy

    def _put(self, item) -> None:
        self.items.append(item)
        self._wake.set()

    def _count(self, item, sign) -> None:
        if item[4] is None:
            return
        self.objects += sign
        if item[2]:
            self.bytes += sign * item[1]
        else:
            self.replay_bytes += sign * item[1]

    def offer(self, gid, live, sgid, oid, payload, exts, prio, status,
              shape, encoded=None, cost=None) -> None:
        """Queue an object or stream end for group `gid`; `live` objects
        count against LANE_BYTES, a cache replay does not. `cost` is the
        object's retained_object_bytes, when already known."""
        if self.stopped or self.final:
            return
        if self.floor is not None and gid < self.floor:
            if oid is not None:
                self.shed += 1
            return
        if oid is None:
            cost = 0
        elif cost is None:
            cost = retained_object_bytes(payload, exts)
        item = (gid, cost, live, sgid, oid, payload, exts, prio, status,
                shape, encoded)
        self._count(item, 1)
        self._put(item)
        if live:
            if gid != self._probe_gid or self._probe_n >= LANE_TX_PROBE:
                self._probe_gid, self._probe_n = gid, 0
                self.tx_bytes = self.track._tx_queued(self.d.session,
                                                      self.floor)
            self._probe_n += 1
            if self.bytes + self.tx_bytes > LANE_BYTES:
                self._shed(gid)

    def _drop_below(self, floor) -> None:
        kept = deque()
        for item in self.items:
            if isinstance(item[0], str) or item[0] >= floor:
                kept.append(item)
                continue
            self._count(item, -1)
            if item[4] is not None:
                self.shed += 1
        self.items = kept

    def _shed(self, gid) -> None:
        """Drop the groups queued before `gid`, and `gid` too if it alone
        is over the bound; their open streams are reset first."""
        floor = gid
        self._drop_below(floor)
        session = self.d.session
        self.tx_bytes = self.track._tx_queued(session, floor)
        if self.bytes + self.tx_bytes > LANE_BYTES:
            floor = gid + 1
            self._drop_below(floor)
            self.tx_bytes = self.track._tx_queued(session, floor)
        self.floor = floor if self.floor is None else max(self.floor, floor)
        if self.busy and self.opening:
            # A parked header write waits on this session's transport
            # backlog, which only these resets free.
            self.track._reset_streams(session, self.floor)
        if not self._cut:
            self._cut = True
            self.items.appendleft(("CUT",))
            self._wake.set()
        if not self.shedding:
            self.shedding = True
            self.sheds += 1
            self._log_shed()

    def _log_shed(self) -> None:
        """WARNING on the first shed, then one INFO line per SHED_LOG_S
        with the episodes since the last line."""
        now = time.monotonic()
        if self._logged_at is not None and now - self._logged_at < SHED_LOG_S:
            return
        d = self.d
        who = (f"relay: {self.track.key} subscriber "
               f"{_peer_address(d.session)} request {d.request_id}")
        if self._logged_at is None:
            logger.warning(
                f"{who} over its {LANE_BYTES}-byte send lane: resetting "
                f"groups below {self.floor}")
        else:
            logger.info(
                f"{who} shed {self.sheds - self._logged_sheds} more "
                f"time(s) over its send lane: resetting groups below "
                f"{self.floor}")
        self._logged_at, self._logged_sheds = now, self.sheds

    def close_streams(self) -> None:
        """FIN the subscriber's open streams once what is queued is out."""
        if not self._closing and not self.final and not self.stopped:
            self._closing = True
            self._put(("CLOSE",))

    def finish(self, status_code, reason) -> None:
        """PUBLISH_DONE once what is queued is out; nothing more is taken."""
        if not self.final and not self.stopped:
            self.final = True
            self._put(("FINAL", status_code, None, reason))

    def stop(self) -> bool:
        """End now, dropping what is queued. True if an object was
        dropped or its write abandoned."""
        if self.stopped:
            return False
        self.stopped = True
        lost = self.objects > 0 or self.busy
        self.items.clear()
        self.bytes = self.replay_bytes = self.objects = self.tx_bytes = 0
        self._wake.set()
        if self.task is not asyncio.current_task():
            self.task.cancel()
        self._exit()
        return lost

    def _exit(self) -> None:
        _lanes.discard(self)
        self.track._lanes.discard(self)
        if self._stalled:
            self._stalled = False
            logger.info(f"relay: lane ended stalled after "
                        f"{time.monotonic() - self._moved_at:.0f}s: "
                        f"{self._who()}")

    def stalled_s(self, now) -> float:
        """Seconds stalled in the current episode; 0 while moving."""
        return now - self._moved_at if self._stalled else 0

    def _who(self) -> str:
        session = self.d.session
        info = _sessions.get(id(session))
        key = self.track.key
        addr = _addr_of(session)
        peer = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else addr
        return (f"sub {info['id'] if info else '?'} {peer}"
                f" track {_ns_str(key[0])}/{_name_str(key[1])}")

    def check(self, now) -> None:
        """Track progress: one WARNING when the lane has work and has
        completed nothing for LANE_STALL_S, one INFO when it moves again."""
        if self.done != self._seen or not (self.busy or self.items):
            if self._stalled:
                self._stalled = False
                logger.info(f"relay: lane resumed after "
                            f"{now - self._moved_at:.0f}s: {self._who()}")
            self._seen, self._moved_at = self.done, now
            return
        if self._stalled or now - self._moved_at < LANE_STALL_S:
            return
        self._stalled = True
        self.stalls += 1
        session = self.d.session
        backlog = sum(t._tx_queued(session) for t in list(_tracks.values()))
        ring = _tx_used(session)(self.sid) if self.sid is not None else 0
        logger.warning(
            f"relay: lane stalled {now - self._moved_at:.0f}s: {self._who()}"
            f" parked on {self.parked if self.busy else 'queue'}; "
            f"lane_bytes={self.bytes + self.replay_bytes} "
            f"tx_backlog={backlog} stream {self.sid} ring_used={ring} "
            f"(budget {_kib(_stream_budget(session))}, "
            f"conn gate {_kib(_conn_gate(session))})")

    async def _run(self) -> None:
        try:
            while not self.stopped:
                if not self.items:
                    self.shedding = False
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                if not await self._handle(self.items.popleft()):
                    return
                self.done += 1
        finally:
            self._exit()

    async def _handle(self, item) -> bool:
        """Write one item; False ends the lane."""
        track, d = self.track, self.d
        kind = item[0]
        if isinstance(kind, str):
            try:
                if kind == "CUT":
                    self._cut = False
                    track._reset_streams(d.session, self.floor)
                elif kind == "CLOSE":
                    self._closing = False
                    track._close_streams(d.session)
                else:
                    track._end(d, *item[1:])
                    return False
            except Exception:
                logger.debug(f"relay: {kind} on a send lane failed",
                             exc_info=True)
            return True
        self._count(item, -1)
        if not _session_live(d.session):
            track.drop_session(d.session)
            return False
        (gid, _cost, _live, sgid, oid, payload, exts, prio, status, shape,
         encoded) = item
        self.busy = True
        self.opening = (id(d.session), gid, sgid) not in track._streams
        try:
            await track._forward_one(d.session, d.alias, gid, sgid, oid,
                                     payload, exts, prio, status, shape,
                                     encoded, start=d.start, lost=d.lost,
                                     lane=self)
        except (Exception, asyncio.CancelledError):
            if _cancelling():
                raise
            logger.debug("relay: forward failed, dropping subscriber",
                         exc_info=True)
            track._drops += 1
            track.drop_session(d.session)
            return False
        finally:
            self.busy = False
        if oid is not None:
            track._objects_out += 1
            track._bytes_out += len(payload or b"")
        return True


class _RelayedTrack:
    """One upstream subscription fanned out to N downstream subscribers.

    Transport §"Relays": a Full Track Name is matched exactly against
    existing upstream subscriptions, so a second downstream subscriber
    for the same track joins this fan-out instead of opening a second
    upstream SUBSCRIBE.

    Objects arrive on a synchronous callback. One task per track takes
    them in arrival order, caches and filters them, and hands each to
    every subscriber's _SendLane, which does the writing.
    """

    def __init__(self, key):
        self.key = key
        self.upstream = None
        # Flow B: the publisher's PUBLISH, held until someone subscribes.
        self.pending_publish = None   # (session, Publish msg)
        # Upstream Forward State is 0: answered so in PUBLISH_OK, or
        # lowered by --pause-after; a forwarding subscriber raises it.
        self.paused = False
        # Paused by --pause-after, after taking objects.
        self.lowered = False
        # Largest group before the last resume: cached groups up to it
        # are stale for subscribers joining since.
        self.fence = None
        # The PUBLISH feeding this track, once answered.
        self.publish_msg = None
        # Loop time since which no subscriber has forwarded, else None.
        self.unwatched_since = None
        # Resumed after a pause: the next object sets complete_from.
        self._resync = False
        self.downstream: list[_Downstream] = []
        # Send lanes still running, including those of subscribers that
        # have left `downstream` and are draining to their PUBLISH_DONE.
        self._lanes: set = set()
        self.queue = asyncio.Queue()
        self.task = None
        # A downstream SUBSCRIBE is establishing this track: an upstream
        # terminal that lands first waits in the queue for it.
        self.attaching = False
        # (id(session), group, subgroup) -> (stream_id, SubgroupHeader)
        self._streams = {}
        # id(session) -> {(group, subgroup): stream_id} of the streams
        # opened, kept past FIN until the transport has drained them.
        self._tx_streams: dict = {}
        # id(session) -> subgroup streams opened (PUBLISH_DONE count)
        self._sent_streams = {}
        # (id(session), group, subgroup) whose stream the subscriber
        # stopped: its later objects are skipped, never reopened (§11.3.2).
        self._stopped = set()
        self._finished = False
        # Closed: takes no objects and never restarts its drain.
        self._closed = False
        # Alias of the PUBLISH feeding this track, on the publisher's session.
        self.publish_alias = None
        # Recent objects, oldest first, for standalone FETCH. Bounded by
        # groups and bytes; a relay with no cache can serve no fetch at
        # all, and an unbounded one is a leak.
        self._cache = deque()
        self._cache_bytes = 0
        # Objects cached per group ID; the bound counts groups, as IDs
        # may skip.
        self._groups = {}
        # Live group dropped at the relay's hard ceiling: the rest of it
        # is not cached.
        self._uncached = None
        # Group -> largest Object ID dropped from, or never put in, the
        # cache.
        self._lost = {}
        # (group, subgroup) whose upstream stream ended cleanly; a replay
        # FINs them on the subscriber it serves.
        self._ended = set()
        # (group, subgroup) whose first upstream object has arrived.
        self._opened = set()
        # Alias the upstream gave this track. A publisher may reuse one
        # across successive subscriptions on a session it keeps open, so
        # the registrations under it are released when the track ends.
        self.upstream_alias = None
        # Request id of the PUBLISH feeding this track, so its
        # PUBLISH_DONE is matched to the right track on a session
        # publishing more than one.
        self.upstream_request_id = None
        # Largest Location (§10.2.11): largest reported or received.
        self.largest = None
        # From here on every upstream object of a subgroup reaches the
        # relay (§11.3.2); None when unknown.
        self.complete_from = None
        # Where the upstream filter starts; {0, 0} when unfiltered.
        self.upstream_from = None
        # Groups whose Object 0 arrived from upstream: complete from it.
        self._heads = set()
        # Locations a backfill FETCH cached; the live copy is dropped.
        self._backfilled = set()
        # Track Properties as last received from upstream (§8.4): sent on
        # every reply for the track, replaced whole, never merged.
        self.properties: dict = {}
        # Upstream subgroup streams ended, against the Stream Count its
        # PUBLISH_DONE reports: the terminal waits for the objects.
        self._upstream_ended = 0
        self._pending_done = None
        self._done_timer = None
        # Objects taken from upstream / written downstream, reported at
        # the terminal: a subscriber short of objects is either a relay
        # that never received them or one that never forwarded them.
        self._objects_in = 0
        self._objects_out = 0
        # Dashboard counters: payload bytes, most subscribers at once,
        # FETCHes served, subscribers dropped on a failed write.
        self._bytes_in = 0
        self._bytes_out = 0
        self._peak_subs = 0
        self._fetches = 0
        # FETCH payload bytes served, by source.
        self._fetch_bytes = {"cache": 0, "upstream": 0}
        self._drops = 0
        self._groups_in = 0
        self._last_gid = None
        self.created = time.time()

    @property
    def finished(self) -> bool:
        """True once the track has reached its terminal."""
        return self._finished

    def release_upstream(self) -> None:
        """Drop this track's handlers on the upstream session, so a later
        subscription reusing the alias is not answered by a dead track."""
        session = _upstream_session(self)
        if session is None or self.upstream_alias is None:
            return
        try:
            session.unregister_object_handler(self.upstream_alias)
            session.unregister_stream_end_handler(self.upstream_alias)
        except Exception:
            logger.debug("relay: releasing upstream handlers failed",
                         exc_info=True)
        self.upstream_alias = None

    def close(self) -> None:
        """Release the fan-out: stop the drain and forget its streams."""
        self._closed = True
        self.release_upstream()
        self._release_publish()
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self._release_downstream()
        self._drop_cache()

    def _release_downstream(self) -> None:
        """Forget the subscribers; lanes draining to their PUBLISH_DONE
        keep their streams, the rest stop."""
        for lane in list(self._lanes):
            if not lane.final:
                lane.stop()
        draining = {id(lane.d.session) for lane in self._lanes}
        self.downstream.clear()
        for k in [k for k in self._streams if k[0] not in draining]:
            self._streams.pop(k, None)
        for k in [k for k in self._tx_streams if k not in draining]:
            del self._tx_streams[k]

    def _lane(self, d) -> _SendLane:
        if d.lane is None:
            d.lane = _SendLane(self, d)
        return d.lane

    def _queued(self, d) -> bool:
        """`d`'s lane still holds or is writing something."""
        return d.lane is not None and not d.lane.idle

    def _drop_cache(self) -> None:
        """Empty the cache and return its bytes to the relay budget."""
        _budget.bytes -= self._cache_bytes
        _budget.forget(self)
        self._cache.clear()
        self._cache_bytes = 0
        self._groups.clear()

    def _release_publish(self) -> None:
        """Drop the PUBLISH's handlers on the publisher's session, unless a
        newer track on that session has taken the alias."""
        session = _upstream_session(self)
        alias, self.publish_alias = self.publish_alias, None
        if session is None or alias is None:
            return
        if getattr(session, "_object_handlers", {}).get(alias) != self.on_object:
            return
        session.unregister_object_handler(alias)
        session.unregister_stream_end_handler(alias)

    def finish(self, status_code=0x2, reset_code=None, reason="track ended"):
        """Terminate downstream subscriptions (§11.4.1): FIN any open
        subgroup streams (reset them with `reset_code` when given), then
        PUBLISH_DONE on each subscription's request stream — never let
        session teardown reset them. Once; later objects are dropped."""
        if self._finished:
            return
        self._finished = True
        for d in list(self.downstream):
            if reset_code is None and self._queued(d):
                d.lane.finish(status_code, reason)
            else:
                self._end(d, status_code, reset_code, reason)

    def end_downstream(self, d) -> None:
        """A subscription whose End is behind production (§5.1.2) ends
        with PUBLISH_DONE SUBSCRIPTION_ENDED; from d20 it stays open and
        only its streams close (d22 §3.3.1). Either follows what its lane
        holds."""
        if not d.session._profile.publish_done_subscription_ended:
            if self._queued(d):
                d.lane.close_streams()
                return
            try:
                self._close_streams(d.session)
            except Exception:
                logger.debug("relay: closing a subscriber's streams failed",
                             exc_info=True)
            return
        if d in self.downstream:
            self.downstream.remove(d)
            if self._queued(d):
                d.lane.finish(SubscribeDoneCode.SUBSCRIPTION_ENDED,
                              "subscription ended")
                return
            self._end(d, SubscribeDoneCode.SUBSCRIPTION_ENDED, None,
                      "subscription ended")

    def _close_streams(self, session, reset_code=None) -> None:
        """FIN the session's open streams, or reset them with
        `reset_code`."""
        if reset_code is not None:
            self._tx_streams.pop(id(session), None)
        for k, (sid, _hdr) in list(self._streams.items()):
            if k[0] == id(session):
                if reset_code is None:
                    session.stream_write(sid, b"", end_stream=True)
                else:
                    session.stream_reset(sid, int(reset_code))
                self._streams.pop(k, None)

    def _reset_streams(self, session, below) -> None:
        """Reset the session's streams of groups below `below`: open ones,
        and FINed ones the transport has not yet sent out."""
        sk = id(session)
        tx = self._tx_streams.get(sk) or {}
        used = _tx_used(session)
        for k, sid in list(tx.items()):
            if k[0] < below:
                del tx[k]
                if (sk, *k) not in self._streams and used(sid):
                    session.stream_reset(sid, StreamResetCode.CANCELLED)
        for k, (sid, _hdr) in list(self._streams.items()):
            if k[0] == sk and k[1] < below:
                session.stream_reset(sid, StreamResetCode.CANCELLED)
                self._streams.pop(k, None)

    def _tx_queued(self, session, floor=None) -> int:
        """Bytes the transport still holds on the session's streams of
        this track, groups below `floor` excepted. Drained FINed streams
        are forgotten."""
        tx = self._tx_streams.get(id(session))
        if not tx:
            return 0
        sk = id(session)
        used = _tx_used(session)
        total = 0
        for k, sid in list(tx.items()):
            n = used(sid)
            if not n and (sk, *k) not in self._streams:
                del tx[k]
            elif floor is None or k[0] >= floor:
                total += n
        return total

    def _end(self, d, status_code, reset_code, reason) -> None:
        """FIN (or reset) a subscriber's open streams, then PUBLISH_DONE.
        Objects its lane still held are dropped, so its streams are reset."""
        session = d.session
        if (d.lane is not None and d.lane.stop() and reset_code is None):
            reset_code = StreamResetCode.CANCELLED
        try:
            self._close_streams(session, reset_code)
            session.subscribe_done(
                request_id=d.request_id, status_code=status_code,
                stream_count=self._sent_streams.get(id(session), 0),
                reason=reason)
        except Exception:
            logger.debug("relay: ending a subscription failed",
                         exc_info=True)

    def _enqueue_terminal(self, status, reason, key) -> None:
        """Terminal behind everything already queued."""
        if self._done_timer is not None:
            self._done_timer.cancel()
            self._done_timer = None
        self._pending_done = None
        self.queue.put_nowait((None, None, None, None, None, None,
                               "DONE", (status, reason, key)))

    def note_upstream_done(self, status, reason, key, stream_count) -> None:
        """§10.11: PUBLISH_DONE follows every stream the publisher opened,
        but those streams and the control message race each other on the
        wire. Hold the terminal until this track has seen Stream Count
        streams end, so a subscriber is not cut off mid-track."""
        need = int(stream_count or 0)
        if self._upstream_ended >= need:
            self._enqueue_terminal(status, reason, key)
            return
        self._pending_done = (status, reason, key, need)
        logger.info(f"relay: PUBLISH_DONE {key} held for "
                    f"{need - self._upstream_ended} more stream(s)")
        loop = asyncio.get_running_loop()
        self._done_timer = loop.call_later(
            TERMINAL_GRACE_S, self._force_terminal)

    def _force_terminal(self) -> None:
        if self._pending_done is None:
            return
        status, reason, key, need = self._pending_done
        logger.info(f"relay: PUBLISH_DONE {key} released on grace "
                    f"({self._upstream_ended}/{need} streams)")
        self._enqueue_terminal(status, reason, key)

    def _check_pending_done(self) -> None:
        if self._pending_done is None:
            return
        status, reason, key, need = self._pending_done
        if self._upstream_ended >= need:
            self._enqueue_terminal(status, reason, key)

    def on_stream_end(self, group_id, subgroup_id, clean=True, reset_code=0):
        """Upstream ended a subgroup stream: mirror it. A FIN becomes our
        FIN (the subscriber may infer end-of-group, §11.4.2); a reset
        becomes our reset with the same code, never a FIN. A malformed
        upstream track ends every downstream subscription with
        PUBLISH_DONE MALFORMED_TRACK (§2.4.2)."""
        if not clean:
            reset_code = self._canonical_reset(reset_code)
        if not clean and reset_code == StreamResetCode.MALFORMED_TRACK:
            self.finish(SubscribeDoneCode.MALFORMED_TRACK,
                        reset_code=StreamResetCode.MALFORMED_TRACK,
                        reason="malformed track")
            # Nothing from this track may be served or cached (§2.4.2):
            # drop the fan-out so a later SUBSCRIBE starts clean rather
            # than joining a dead one.
            _tracks.pop(self.key, None)
            self.close()
            return
        self.queue.put_nowait(
            (group_id, subgroup_id or 0, None, None, None, None,
             "END" if clean else "RESET", reset_code))
        if clean:
            self._ended.add((group_id, subgroup_id or 0))
        self._ensure_drain()
        self._upstream_ended += 1
        self._check_pending_done()

    def _canonical_reset(self, wire) -> StreamResetCode:
        """An upstream reset code by meaning, so each subscriber gets it
        encoded for its own draft; unknown codes become INTERNAL_ERROR."""
        up = _upstream_session(self)
        prof = getattr(up, "_profile", None)
        code = wire if prof is None else canonical_stream_reset(wire, prof)
        try:
            return StreamResetCode(code)
        except ValueError:
            return StreamResetCode.INTERNAL_ERROR

    def note_largest(self, location) -> None:
        """§10.2.11: Largest is the largest Location reported or seen."""
        if location is not None and (self.largest is None
                                     or tuple(location) > self.largest):
            self.largest = tuple(location)

    def on_object(self, msg, size, ts, group_id, subgroup_id):
        """Upstream delivery callback (sync) — hand off to the drain."""
        if self._closed:
            return
        self._objects_in += 1
        payload = bytes(msg.payload)
        self._bytes_in += len(payload)
        gid = getattr(msg, "group_id", None)
        gid = gid if gid is not None else group_id
        if gid != self._last_gid:
            self._groups_in += 1
            self._last_gid = gid
        if msg.object_id == 0:
            self._heads.add(gid)
        if self._resync:
            # Objects before this one went unsent while paused.
            self._resync = False
            self.complete_from = (gid, msg.object_id)
        if self.key[1] == CATALOG_TRACK and msg.object_id == 0:
            _note_catalog(self.key[0], payload)
        # On arrival, so a SUBSCRIBE sees it; objects for the subscriber
        # still attaching are its own and count once drained.
        if not self.attaching:
            self.note_largest((gid, msg.object_id))
        # Forward the publisher's priority, never a substitute of our
        # own: a subscriber's scheduling depends on it.
        prio = getattr(msg, "publisher_priority", None)
        # Delivery mode is part of what a subscriber checks: an object
        # the publisher sent as a datagram goes out as one, with its
        # end-of-group bit, not re-framed onto a subgroup stream.
        shape = getattr(msg, "stream_flags", None)
        if isinstance(msg, (ObjectDatagram, ObjectDatagramStatus)):
            shape = ("DGRAM", bool(getattr(msg, "end_of_group", False)))
        elif isinstance(shape, tuple) and shape:
            # FIRST_OBJECT holds for the object that opened the upstream
            # stream only; a downstream stream opened later must not claim it.
            key = (gid, subgroup_id or 0)
            if key in self._opened:
                shape = (False,) + tuple(shape[1:])
            else:
                self._opened.add(key)
        self.queue.put_nowait(
            (gid, subgroup_id or 0, msg.object_id,
             payload, msg.extensions or None,
             128 if prio is None else prio,
             getattr(msg, "status", None),
             shape))
        self._ensure_drain()

    def _ensure_drain(self) -> None:
        """Drain from the first object, subscribers or not; objects for a
        subscriber still attaching wait for it."""
        if self._finished or self._closed or self.attaching:
            return
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._forward_loop())
            self.task.add_done_callback(self._drain_ended)

    def _drain_ended(self, task) -> None:
        """Only the terminal and close() may end the drain; anything else
        leaves the queue growing unread, so it is logged."""
        if self.task is not task:
            return
        err = "cancelled" if task.cancelled() else repr(task.exception())
        logger.error(f"relay: forwarding {self.key} stopped: {err}")

    def add_downstream(self, session, track_alias, request_id=None,
                       forward=True, window=_UNFILTERED,
                       replay=False) -> _Downstream:
        """Join the fan-out. With `replay`, cached objects inside the
        window go to the subscriber first, then live ones."""
        self.attaching = False
        d = _Downstream(session, track_alias, request_id, forward, window)
        d.fence = self.fence
        if forward:
            # A joining FETCH from this subscriber backfills to here.
            d.joined_at = self.largest
        self.downstream.append(d)
        self._peak_subs = max(self._peak_subs, len(self.downstream))
        if replay:
            d.replaying = True
            self.queue.put_nowait((None, None, None, None, None, None,
                                   "REPLAY", d))
        self._ensure_drain()
        return d

    def find(self, session, request_id):
        return next((d for d in self.downstream if d.session is session
                     and d.request_id == request_id), None)

    def drop_session(self, session):
        for lane in [lane for lane in self._lanes
                     if lane.d.session is session]:
            lane.stop()
        for d in self.downstream:
            if d.session is session:
                # d22 §3.4.1: a cancelled subscription resets its fills.
                for task in list(d.fills):
                    task.cancel()
        self.downstream = [d for d in self.downstream
                           if d.session is not session]
        for k in [k for k in self._streams if k[0] == id(session)]:
            self._streams.pop(k, None)
        self._stopped = {k for k in self._stopped if k[0] != id(session)}
        self._sent_streams.pop(id(session), None)
        self._tx_streams.pop(id(session), None)

    async def _forward_loop(self):
        while True:
            gid, sgid, oid, payload, exts, prio, status, shape = \
                await self.queue.get()
            if status == "DONE":
                # Terminal, in queue order: each subscriber's PUBLISH_DONE
                # follows what its lane already holds.
                code, reason, key = shape
                # Replays still queued behind the terminal go first.
                for d in list(self.downstream):
                    if d.replaying:
                        self._replay(d)
                self.finish(status_code=code, reason=reason)
                self.release_upstream()
                self._release_downstream()
                self._drop_cache()
                self.task = None
                _tracks.pop(key, None)
                return
            if self._finished:
                continue
            if status == "REPLAY":
                self._replay(shape)
                continue
            if oid is not None:
                if (gid, oid) in self._backfilled:
                    self._backfilled.discard((gid, oid))
                    continue
                cost = retained_object_bytes(payload, exts)
                self._remember(gid, sgid, oid, payload, exts, prio, status,
                               shape, cost)
            else:
                cost = 0
            encoded = {}
            for d in list(self.downstream):
                if d.replaying:
                    continue
                if not _session_live(d.session):
                    self.drop_session(d.session)
                    continue
                out = (oid, payload, exts, status, shape)
                if oid is not None:
                    if d.past_end(gid):
                        self.end_downstream(d)
                        continue
                    if not d.passes(gid, oid):
                        continue
                    if not d.admits(sgid, oid, prio, exts):
                        if status not in (ObjectStatus.END_OF_GROUP,
                                          ObjectStatus.END_OF_TRACK):
                            continue
                        # A filtered-out end marker still ends its stream.
                        out = (None, None, None, "END", None)
                o_oid, o_payload, o_exts, o_status, o_shape = out
                self._lane(d).offer(gid, True, sgid, o_oid, o_payload,
                                    o_exts, prio, o_status, o_shape, encoded,
                                    cost)
                if (oid is not None and d.window[1] == gid and status in (
                        ObjectStatus.END_OF_GROUP, ObjectStatus.END_OF_TRACK)):
                    self.end_downstream(d)

    def _remember(self, gid, sgid, oid, payload, exts, prio, status,
                  shape=None, cost=None) -> None:
        """Keep one object, with its stream shape or datagram mode, for a
        later FETCH or replay, dropping oldest past the bound."""
        if self._uncached is not None:
            if gid == self._uncached:
                self.note_largest((gid, oid))
                self._note_lost(gid, oid)
                return
            if gid > self._uncached:
                self._uncached = None
        self._cache.append((gid, sgid, oid, payload, exts, prio, status,
                            shape))
        size = (retained_object_bytes(payload, exts) if cost is None
                else cost)
        self._cache_bytes += size
        _budget.bytes += size
        self._groups[gid] = self._groups.get(gid, 0) + 1
        self.note_largest((gid, oid))
        _budget.touch(self)
        self._evict()

    def _evict(self) -> None:
        """Drop the oldest objects past CACHE_GROUPS groups or
        CACHE_BYTES, then hold the relay to its budget."""
        while self._cache and (len(self._groups) > CACHE_GROUPS
                               or self._cache_bytes > CACHE_BYTES):
            dropped = self._cache.popleft()
            size = retained_object_bytes(dropped[3], dropped[4])
            self._note_lost(dropped[0], dropped[2])
            self._cache_bytes -= size
            _budget.bytes -= size
            left = self._groups.pop(dropped[0]) - 1
            if left:
                self._groups[dropped[0]] = left
        self._prune()
        _budget.enforce()

    def _evictable(self) -> bool:
        """Holds a group the budget may drop: anything but the newest
        group of a track with a live subscriber."""
        if len(self._groups) > 1:
            return True
        return bool(self._groups) and not any(
            _session_live(d.session) for d in self.downstream)

    def _drop_oldest_group(self) -> None:
        """Drop every cached object of the oldest group, so the cache
        stays a contiguous run of the newest groups."""
        self._drop_group(min(self._groups))

    def _truncate_live(self) -> None:
        """At the relay's hard ceiling: drop the newest cached group and
        cache none of the rest of it. Live delivery is unaffected."""
        gid = max(self._groups)
        logger.warning(
            f"relay: cache {_budget.bytes} bytes over its "
            f"{CACHE_BUDGET_BYTES + CACHE_BYTES}-byte ceiling: dropping "
            f"live group {gid} of {self.key} ({self._cache_bytes} bytes)")
        self._uncached = gid
        self._drop_group(gid)

    def _note_lost(self, gid, oid) -> None:
        if oid > self._lost.get(gid, -1):
            self._lost[gid] = oid

    def _drop_group(self, gid) -> None:
        size = sum(retained_object_bytes(c[3], c[4]) for c in self._cache
                   if c[0] == gid)
        self._note_lost(gid, max(c[2] for c in self._cache if c[0] == gid))
        self._cache = deque(c for c in self._cache if c[0] != gid)
        del self._groups[gid]
        self._cache_bytes -= size
        _budget.bytes -= size
        self._prune()

    def _prune(self) -> None:
        """Forget per-group state below the oldest cached group; an
        empty cache leaves the budget's LRU."""
        floor = (self._cache[0][0] if self._cache
                 else self.largest[0] if self.largest else 0)
        self._lost = {g: o for g, o in self._lost.items() if g >= floor}
        if not self._cache:
            _budget.forget(self)
        else:
            first = self._cache[0][0]
            self._ended = {k for k in self._ended if k[0] >= first}
            self._opened = {k for k in self._opened if k[0] >= first}
            self._heads = {g for g in self._heads if g >= first}
            self._backfilled = {k for k in self._backfilled
                                if k[0] >= first}
            self._stopped = {k for k in self._stopped if k[1] >= first}

    def _replay(self, d) -> None:
        """Queue on `d`'s lane the cached objects inside its window, in
        Location order, then FIN for the subgroups the publisher has
        already ended; live objects follow."""
        d.replaying = False
        if d not in self.downstream:
            return
        d.lost = {g for g, o in self._lost.items() if (g, o) >= d.start}
        objs = sorted((c for c in self._cache if d.passes(c[0], c[2])
                       and d.admits(c[1], c[2], c[5], c[4])),
                      key=lambda c: (c[0], c[1], c[2]))
        if objs:
            _budget.touch(self)
        lane = self._lane(d)
        for gid, sgid, oid, payload, exts, prio, status, shape in objs:
            lane.offer(gid, False, sgid, oid, payload, exts, prio, status,
                       shape)
        for gid, sgid in sorted({(c[0], c[1]) for c in objs}):
            if (gid, sgid) in self._ended:
                lane.offer(gid, False, sgid, None, None, None, None, "END",
                           None)
        logger.info(f"relay: replaying {len(objs)} cached object(s) of "
                    f"{self.key} to request {d.request_id}")

    def cache_covers(self, start) -> bool:
        """True when the cache still holds the start of the range. The
        oldest object is the boundary: anything before it was evicted
        and has to come from the publisher, or the subscriber gets a
        short answer with no way to tell."""
        if not self._cache:
            return False
        first = (self._cache[0][0], self._cache[0][2])
        return start >= first

    def group_end(self, group_id, start):
        """The last cached Location of a group, which is its END_OF_GROUP
        when held; (group, 0) when none is held, and never before
        `start`."""
        oids = [c[2] for c in self._cache if c[0] == group_id]
        return max(start, (group_id, max(oids) if oids else 0))

    def cached_range(self, start, end):
        """Cached objects within [start, end], each (group, object)
        inclusive; `end` None means to the live edge. In Location order,
        across subgroups (§3.2.1)."""
        out = []
        for gid, sgid, oid, payload, exts, prio, status, _ in self._cache:
            if (gid, oid) < start:
                continue
            if end is not None and (gid, oid) > end:
                continue
            out.append((gid, sgid, oid, payload, exts, prio, status))
        out.sort(key=lambda o: (o[0], o[2]))
        if out:
            _budget.touch(self)
        return out

    def _short(self, gid, start) -> bool:
        """A subscription starting at `start` is owed objects of group
        `gid` from before what upstream delivered to this relay."""
        cf = self.complete_from
        return (start is not None and cf is not None
                and max(start, (gid, 0)) < cf and gid not in self._heads)

    async def _forward_one(self, session, alias, gid, sgid, oid,
                           payload, exts, prio, status=None, shape=None,
                           encoded=None, start=None, lost=(), lane=None):
        """Write one object downstream, opening the (group, subgroup)
        stream on first sight. Group/subgroup identity is preserved from
        upstream so the downstream sees the publisher's structure. A
        stream short of objects from `start` is reset, never FINed
        (§11.3.2). `lane` is told what each write awaits."""
        if isinstance(shape, tuple) and shape and shape[0] == "DGRAM":
            prof = session._profile
            is_status = (status or ObjectStatus.NORMAL) != ObjectStatus.NORMAL
            if is_status and not prof.merged_datagram_layout:
                # d14 keeps status datagrams in their own message family.
                dgram = ObjectDatagramStatus(
                    track_alias=alias, group_id=gid, object_id=oid,
                    publisher_priority=prio, extensions=exts, status=status)
            else:
                dgram = ObjectDatagram(
                    track_alias=alias, group_id=gid, object_id=oid,
                    publisher_priority=prio, extensions=exts,
                    payload=payload or b"",
                    end_of_group=bool(shape[1]),
                    status=status or ObjectStatus.NORMAL)
            _send_datagram(session, dgram.serialize(prof=prof), alias, gid, oid)
            return
        skey = (id(session), gid, sgid)
        if skey in self._stopped:
            return
        entry = self._streams.get(skey)
        if entry is None and status in ("END", "RESET"):
            return
        if status == "RESET":
            stream_id, _header = entry
            if not isinstance(shape, StreamResetCode):
                shape = StreamResetCode.INTERNAL_ERROR
            session.stream_reset(stream_id, shape)
            self._streams.pop(skey, None)
            return
        if entry is None:
            if lane is not None:
                lane.parked, lane.sid = "header", None
            stream_id = await session.open_uni_stream()
            if lane is not None:
                lane.sid = stream_id
            session.set_stream_priority(stream_id, _stream_band(session, prio))
            # Mirror the upstream stream's shape: END_OF_GROUP,
            # FIRST_OBJECT and the subgroup-id mode are part of the
            # delivery semantics a subscriber checks, and re-encoding
            # with our own flags loses them.
            first_obj, eog, inherited = shape or (False, False, False)
            header = SubgroupHeader(
                track_alias=alias, group_id=gid, subgroup_id=sgid,
                publisher_priority=prio, extensions_present=True,
                end_of_group=eog, first_object=first_obj,
                prof=session._profile)
            try:
                await session.stream_write_drain(stream_id,
                                                 header.serialize().data)
            except MOQTStreamStopped:
                self._stopped.add(skey)
                return
            except BaseException:
                # Never leave a stream open without its header.
                session.stream_reset(stream_id, StreamResetCode.CANCELLED)
                raise
            entry = (stream_id, header)
            self._streams[skey] = entry
            self._tx_streams.setdefault(id(session), {})[(gid, sgid)] = \
                stream_id
            self._sent_streams[id(session)] = \
                self._sent_streams.get(id(session), 0) + 1
        stream_id, header = entry
        if status in ("END", ObjectStatus.END_OF_GROUP,
                      ObjectStatus.END_OF_TRACK) and (
                          gid in lost or self._short(gid, start)):
            session.stream_reset(stream_id, StreamResetCode.INTERNAL_ERROR)
            self._streams.pop(skey, None)
            return
        if status == "END":
            # Upstream closed the subgroup stream without a marker, so
            # the group end IS the FIN. Writing a marker here would be
            # inventing one the publisher never sent.
            session.stream_write(stream_id, b"", end_stream=True)
            self._streams.pop(skey, None)
            return
        if status in (ObjectStatus.END_OF_GROUP, ObjectStatus.END_OF_TRACK):
            # Upstream sent an explicit marker: forward it, then FIN.
            buf = header.end_group(
                object_id=header.next_object_id if oid is None else oid)
            session.stream_write(stream_id, buf.data, end_stream=True)
            self._streams.pop(skey, None)
            return
        # One encoding per object per stream state, shared by subscribers.
        state = header.encoding_state()
        data = None if encoded is None else encoded.get(state)
        if data is None:
            data = header.next_object_bytes(payload=payload, extensions=exts,
                                            object_id=oid)
            if encoded is not None:
                encoded[state] = data
        else:
            header.advance(oid)
        if lane is not None:
            lane.parked, lane.sid = "object", stream_id
        try:
            await session.stream_write_drain(stream_id, data)
        except MOQTStreamStopped:
            # The session has reset the stream; the subscription goes on.
            self._streams.pop(skey, None)
            self._stopped.add(skey)


_watched: set = set()
# id(session) -> {"session", "since", "ns"}: when the dashboard first saw
# it and the namespaces it has used.
_sessions: dict[int, dict] = {}
# Short opaque session ids for the dashboard.
_session_ids = itertools.count(1)


def _watch_session(session, ns=None) -> None:
    """Reap everything a session owns the moment it closes.

    Liveness cannot be decided lazily at the next request: a second
    publish/subscribe cycle can arrive before the closed session has been
    observed as closed, and the stale track is then handed to the new
    subscriber, which receives nothing.
    """
    info = _sessions.get(id(session))
    if info is None:
        info = _sessions[id(session)] = {
            "session": session, "since": time.time(), "ns": set(),
            "addr": None, "id": next(_session_ids)}
        _background.add(asyncio.create_task(_log_peer(session, info)))
    if ns is not None:
        info["ns"].add(tuple(ns))
    if session in _watched:
        return
    _watched.add(session)

    async def _reap():
        try:
            await session.async_closed()
        finally:
            _watched.discard(session)
            _sessions.pop(id(session), None)
            _forget_session(session)
            logger.debug("relay: session closed, state released")
            _background.discard(asyncio.current_task())

    _background.add(asyncio.create_task(_reap()))


def _peer_address(session):
    try:
        return session.peer_address
    except Exception:
        return None


def _addr_of(session):
    """The session's (host, port), cached once known; None until then."""
    info = _sessions.get(id(session))
    if info is not None and info.get("addr"):
        return info["addr"]
    addr = _peer_address(session)
    if addr and info is not None:
        info["addr"] = addr
    return addr


async def _log_peer(session, info) -> None:
    """Log a new session's full address once the transport reports it."""
    try:
        for _ in range(50):
            addr = info.get("addr") or _peer_address(session)
            if addr:
                info["addr"] = addr
                host, port = addr[0], addr[1]
                if ":" in host:
                    host = f"[{host}]"
                logger.info(f"relay: session {_transport(session)} "
                            f"d{_draft(session)} from {host}:{port}")
                return
            if not _session_live(session):
                return
            await asyncio.sleep(0.1)
    finally:
        _background.discard(asyncio.current_task())


def _supersede_namespace(ns: tuple, session) -> None:
    """A publisher arriving for a namespace replaces tracks cached against
    a session that has gone.

    Session close is not a usable trigger on its own: a client that
    reconnects promptly announces again before its previous session has
    been observed as closed, and the stale track would be handed to the
    next subscriber, which then receives nothing. The new announcement
    is the event that always arrives in time.
    """
    for key, track in list(_tracks.items()):
        if key[0][:len(ns)] != ns and ns[:len(key[0])] != key[0]:
            continue
        owner = _upstream_session(track) or (
            track.pending_publish[0] if track.pending_publish else None)
        # A live owner is another publisher in the same namespace.
        if (owner is not None and owner is not session
                and not _session_live(owner)):
            logger.info(f"relay: superseding track {key} for a new publisher")
            track.close()
            _tracks.pop(key, None)


def _forget_session(session) -> None:
    """Drop everything a closing session owned: its announcements and
    its downstream subscriptions."""
    _track_subs[:] = [e for e in _track_subs if e[0] is not session]
    for k in [k for k in _track_sub_filters if k[0] == id(session)]:
        del _track_sub_filters[k]
    _ns_subs[:] = [e for e in _ns_subs if e[0] is not session]
    _announce_subs[:] = [e for e in _announce_subs if e[0] is not session]
    _dgram_drops.pop(id(session), None)
    _subscribing.pop(id(session), None)
    _cap_warned.difference_update(
        [k for k in _cap_warned if k[0] == id(session)])
    for k in [k for k in _announce_requests if k[0] == id(session)]:
        del _announce_requests[k]
    for ns in list(_announced):
        if _announced[ns].pop(session, None) is not None and \
                not _announced[ns]:
            del _announced[ns]
            _retract_namespace(ns)
    for lane in [lane for lane in _lanes if lane.d.session is session]:
        lane.track.drop_session(session)
    for key, track in list(_tracks.items()):
        track.drop_session(session)
        if _upstream_session(track) is session or (
                track.pending_publish is not None
                and track.pending_publish[0] is session):
            # Upstream is gone: give each subscriber a clean terminal
            # before dropping the fan-out.
            track.finish()
            track.close()
            _tracks.pop(key, None)


def _ns_tuple(namespace):
    """Normalize a namespace value (str / list / tuple of bytes-or-str)
    into a tuple of bytes for use as a dict key."""
    if isinstance(namespace, str):
        return tuple(s.encode() for s in namespace.split("/") if s)
    if isinstance(namespace, (list, tuple)):
        return tuple(
            s.encode() if isinstance(s, str) else bytes(s)
            for s in namespace
        )
    return tuple()


def _session_namespaces(session) -> int:
    """Namespace requests the session holds."""
    held = sum(1 for sid, _ in _announce_requests if sid == id(session))
    subs = {e[2] for table in (_ns_subs, _track_subs, _announce_subs)
            for e in table if e[0] is session}
    return held + len(subs)


def _session_subscriptions(session) -> int:
    """SUBSCRIBEs the session has in flight or attached to a track."""
    return _subscribing[id(session)] + sum(
        1 for t in _tracks.values() for d in t.downstream
        if d.session is session)


def _refused_over_cap(session, msg, what: str, cap: int, held: int) -> bool:
    """Refuse `msg` with EXCESSIVE_LOAD when the session holds `cap`
    of `what`; one WARNING per session and cap."""
    if held < cap:
        return False
    if (id(session), what) not in _cap_warned:
        _cap_warned.add((id(session), what))
        logger.warning(f"relay: session {_addr_of(session)} holds {held} "
                       f"{what} (cap {cap}): refusing more with "
                       f"EXCESSIVE_LOAD")
    session._refuse_peer_request(
        msg, RequestErrorCode.EXCESSIVE_LOAD,
        f"too many {what} on this session", retry_interval=CAP_RETRY_MS + 1)
    return True


def _namespaces_full(session, msg) -> bool:
    return _refused_over_cap(session, msg, "namespaces",
                             MAX_NAMESPACES_PER_SESSION,
                             _session_namespaces(session))


async def _on_publish_namespace(session, msg):
    """Record the announcing session, ack with default OK path."""
    if _namespaces_full(session, msg):
        return
    ns = _ns_tuple(msg.namespace)
    _watch_session(session, ns)
    _supersede_namespace(ns, session)
    holders = _announced.setdefault(ns, {})
    holders[session] = holders.get(session, 0) + 1
    _announce_requests[(id(session), msg.request_id)] = ns
    # d18 has no PUBLISH_NAMESPACE_DONE: cancelling the request withdraws
    # the namespace (§3.3.2).
    session.register_request_cancel_handler(
        msg.request_id,
        lambda rid, s=session: _withdraw_namespace(s, request_id=rid))
    logger.info(f"relay: announce ns={ns} -> {len(holders)} publisher(s)")
    _announce_namespace(ns)
    _forward_announcements(ns, session)
    # Reuse the protocol's built-in OK helper. It emits RequestOk on
    # d16+ and PublishNamespaceOk on d14, matching peer expectation.
    session.publish_namespace_ok(msg)
    if _catalog_probe and _probe_wanted(ns):
        _spawn(_probe_catalog(ns))


async def _on_publish_namespace_done(session, msg):
    """d14 names the namespace, d16 the PUBLISH_NAMESPACE request."""
    ns = _ns_tuple(msg.namespace) if msg.namespace else None
    _withdraw_namespace(session, request_id=msg.request_id, ns=ns)


def _withdraw_namespace(session, request_id=None, ns=None) -> None:
    """Drop the hold one PUBLISH_NAMESPACE has on its namespace, at most
    once however it is withdrawn; the last hold retracts the namespace."""
    if request_id is None:
        request_id = next((rid for (sid, rid), held
                           in _announce_requests.items()
                           if sid == id(session) and held == ns), None)
    ns = _announce_requests.pop((id(session), request_id), None)
    holders = _announced.get(ns) if ns is not None else None
    if not holders or session not in holders:
        logger.info(f"relay: withdrawal of unknown namespace "
                    f"request_id={request_id}")
        return
    holders[session] -= 1
    if holders[session] <= 0:
        del holders[session]
    if not holders:
        del _announced[ns]
        _retract_namespace(ns)
    logger.info(f"relay: namespace_done ns={ns} -> "
                f"{len(_announced.get(ns, {}))} publisher(s)")


# Mandatory Track Properties (§3.7, from d18); this relay understands none.
_MANDATORY_PROPERTIES = range(0x4000, 0x8000)
_MANDATORY_REFUSAL = (RequestErrorCode.UNSUPPORTED_EXTENSION,
                      "unsupported mandatory track property")


def _track_properties(session, msg) -> dict:
    """The Track Properties a PUBLISH, SUBSCRIBE_OK or FETCH_OK carries.
    The codec lifts DEFAULT_PUBLISHER_GROUP_ORDER into group_order; a
    Descending one goes back in (omitted means Ascending)."""
    if msg is None or not session._profile.track_properties:
        return {}
    props = dict(getattr(msg, "track_extensions", None) or {})
    if (isinstance(msg, (Publish, SubscribeOk))
            and msg.group_order == GroupOrder.DESCENDING):
        props[DEFAULT_GROUP_ORDER_PROPERTY] = int(GroupOrder.DESCENDING)
    return props


def _mandatory(session, props) -> bool:
    """`props` holds a Mandatory Track Property (§3.7): the track is
    neither processed nor forwarded."""
    return ((session.negotiated_draft or 0) >= 18
            and any(t in _MANDATORY_PROPERTIES for t in props))


def _upstream_refusal(err, profile, fetch=False) -> tuple:
    """An upstream REQUEST_ERROR as (RequestErrorCode, reason), for the
    downstream reply to encode in its own draft."""
    code = canonical_request_error(err.error_code, profile, fetch=fetch)
    if not isinstance(code, RequestErrorCode):
        code = RequestErrorCode.INTERNAL_ERROR
    return code, err.reason or "upstream error"


class _Establishing:
    """One upstream SUBSCRIBE in flight for a Full Track Name; every
    subscriber arriving meanwhile awaits `fut` for (fan-out, refusal)."""

    def __init__(self, key):
        self.key = key
        self.fut = asyncio.get_running_loop().create_future()
        # The publisher session the SUBSCRIBE is outstanding on.
        self.pub = None
        # Forward State of each waiting subscriber.
        self.forwards: list[bool] = []
        # subscribe() filter kwargs for the upstream SUBSCRIBE.
        self.filter: dict = {}
        self.task = None

    def resolve(self, result) -> None:
        if _establishing.get(self.key) is self:
            del _establishing[self.key]
        if not self.fut.done():
            self.fut.set_result(result)


async def _establish_upstream(ns, track_name, forward=True, sub=None):
    """Subscribe upstream once per Full Track Name. Returns (fan-out,
    None), or (None, the last upstream refusal or None) when no
    publisher accepts. Concurrent callers share one upstream SUBSCRIBE.

    Transport §"Relays": with no Established upstream subscription for
    the Track, the relay subscribes to each publisher that announced the
    subscription's namespace or a prefix of it.
    """
    key = (ns, track_name)
    entry = _establishing.get(key)
    if entry is None:
        track = _tracks.get(key)
        if track is not None:
            # Reuse only a live upstream. A track cached from an earlier
            # cycle points at a closed session, and attaching a new
            # subscriber to it delivers nothing at all.
            if _track_live(track):
                return track, None
            logger.info(f"relay: dropping stale track {key}")
            track.close()
            _tracks.pop(key, None)
        entry = _Establishing(key)
        entry.filter = _upstream_filter(sub)
        _establishing[key] = entry
        entry.task = asyncio.create_task(_subscribe_upstream(entry))
    entry.forwards.append(bool(forward))
    return await asyncio.shield(entry.fut)


def _upstream_filter(sub) -> dict:
    """The upstream SUBSCRIBE for a track the relay holds nothing of,
    open-ended so later subscribers can join. A filter relative to
    Largest starts at {0, 0} here (§3.1.4), so the upstream is unfiltered
    (see _filter_for); an absolute start is kept. A Relative Start is
    carried for the publisher to resolve."""
    lf = getattr(sub, "location_filter", None)
    if lf is not None and lf.type == LocationFilterType.RELATIVE_START:
        return {"location_filter": lf}
    start = (_UNFILTERED if sub is None
             else _subscription_window(sub, None))[0]
    if start == (0, 0):
        return {"filter_type": None}
    return {"filter_type": FilterType.ABSOLUTE_START,
            "start_group": start[0], "start_object": start[1]}


def _filter_for(pub, kwargs: dict) -> dict:
    """From d16 an omitted filter is unfiltered (§10.2.9); d14 has no
    omission, so it carries AbsoluteStart {0, 0}. Before d20 a relative
    start is sent as Largest Object; _backfill() fetches the rest."""
    if "location_filter" in kwargs and not uses_location_filter(
            pub._profile):
        return {"filter_type": FilterType.LATEST_OBJECT}
    if "filter_type" in kwargs and kwargs["filter_type"] is None and not (
            is_draft16_or_later(pub.negotiated_draft)):
        return {"filter_type": FilterType.ABSOLUTE_START,
                "start_group": 0, "start_object": 0}
    return kwargs


def _delivery_start(kwargs: dict, largest):
    """Where an upstream SUBSCRIBE with these subscribe() kwargs starts
    delivering, given the Largest its SUBSCRIBE_OK reported. Unfiltered
    is taken as the live edge."""
    lf = kwargs.get("location_filter")
    if lf is not None:
        return _location_window(lf, largest)[0]
    return _filter_window(kwargs.get("filter_type")
                          or FilterType.LATEST_OBJECT,
                          kwargs.get("start_group"),
                          kwargs.get("start_object"), None, largest)[0]


async def _backfill(track, msg, largest) -> None:
    """Fetch into the cache what a subscription resolved against
    `largest` is owed from before complete_from (§11.3.2): back to the
    cache's reach when the upstream filter never asked for it (§9.5.1),
    else the head of the group delivery began in. Left short when
    nothing serves it; those streams are then reset."""
    cf = track.complete_from
    if cf is None:
        return
    want = _subscription_window(msg, largest)[0]
    if want < (track.upstream_from or cf):
        # The cache's reach: CACHE_GROUPS groups, by the IDs it has seen
        # below cf, never short of a contiguous span.
        seen = sorted(g for g in track._groups if g < cf[0])
        reach = min([cf[0] - CACHE_GROUPS + 1,
                     *seen[-(CACHE_GROUPS - 1):][:1]])
        start = max(want, (max(0, reach), 0))
    else:
        start = max(want, (cf[0], 0))
    ns, name = track.key
    if (start >= cf or track.cache_covers(start)
            or not _fetch_sources(ns, name)):
        return
    # FETCH End is the last Object plus one; Object 0 is the whole group.
    end = cf if cf[1] else (cf[0] - 1, 0)
    rng = SimpleNamespace(group_order=GroupOrder.ASCENDING,
                          start_group=start[0], start_object=start[1],
                          end_group=end[0], end_object=end[1])
    _, objs, _ = await _fetch_upstream(ns, name, rng, timeout=BACKFILL_S)
    if (objs is None or track.complete_from != cf or track._closed
            or track.finished):
        return
    held = {(c[0], c[2]) for c in track._cache}
    new = [(o.group_id, o.subgroup_id, o.object_id, bytes(o.payload),
            o.extensions or None, o.publisher_priority, o.status, None)
           for o in objs if (o.group_id, o.object_id) not in held]
    track._cache = deque(sorted([*new, *track._cache],
                                key=lambda c: (c[0], c[2])))
    size = sum(retained_object_bytes(c[3], c[4]) for c in new)
    track._cache_bytes += size
    _budget.bytes += size
    for c in new:
        track._groups[c[0]] = track._groups.get(c[0], 0) + 1
    _budget.touch(track)
    track._evict()
    track._backfilled.update((c[0], c[2]) for c in new)
    # Groups before complete_from's came whole: a replay FINs them.
    track._ended.update((c[0], c[1]) for c in new if c[0] < cf[0])
    track.complete_from = start
    track.upstream_from = min(track.upstream_from or cf, start)
    logger.info(f"relay: backfilled {len(new)} object(s) of {track.key} "
                f"{start}..{cf}")


def _probe_wanted(ns) -> bool:
    return (bool(ns) and not _internal_ns(ns)
            and not ns[0].startswith(CATALOG_PROBE_SKIP))


def _probing(track) -> bool:
    """--catalog-probe forwards this catalog with no subscriber."""
    return (_catalog_probe and track.key[1] == CATALOG_TRACK
            and _probe_wanted(track.key[0]))


async def _probe_catalog_head(track) -> None:
    """Backfill the catalog's current group from its head, as a viewer's
    SUBSCRIBE would, when it was published before the relay subscribed."""
    if track.largest is None or track.key[0] in _catalogs:
        return
    g = track.largest[0]
    head = SimpleNamespace(filter_type=FilterType.ABSOLUTE_START,
                           start_group=g, start_object=0, end_group=None)
    await _backfill(track, head, track.largest)
    for c in list(track._cache):
        if c[0] == g and c[2] == 0:
            _note_catalog(track.key[0], c[3])
            return


async def _probe_catalog(ns) -> None:
    """Subscribe to an announced namespace's catalog as a viewer would, so
    the dashboard knows its format before anyone plays it."""
    key = (ns, CATALOG_TRACK)
    for delay in CATALOG_PROBE_DELAYS_S:
        await asyncio.sleep(delay)
        track = _tracks.get(key)
        if (not _announced_match(ns) or ns in _catalogs
                or key in _establishing
                or (track is not None and _track_live(track))):
            return
        track, refusal = await _establish_upstream(ns, CATALOG_TRACK)
        if track is None:
            reason = refusal[1] if refusal else "no publisher served it"
            logger.info(f"relay: catalog probe ns={ns} -> none ({reason})")
            continue
        await _probe_catalog_head(track)
        if not track.downstream:
            track.attaching = False
            track._ensure_drain()
        logger.info(f"relay: catalog probe ns={ns} -> "
                    f"{_catalogs.get(ns) or 'no MSF catalog yet'}")
        return


def _spawn(coro) -> None:
    """Run `coro` in the background, held until it ends."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _live_published(key):
    """The live track a PUBLISH registered for `key`, else None."""
    track = _tracks.get(key)
    if (track is not None and _track_live(track)
            and (track.pending_publish is not None
                 or _is_session(track.upstream))):
        return track
    return None


def _is_session(obj) -> bool:
    return obj is not None and hasattr(obj, "_moqt_session_closed")


def _duplicate_refusal(err, profile) -> bool:
    """The publisher refused our SUBSCRIBE because it PUBLISHes the
    track (§5.1); NOT_SUPPORTED from d20, which drops the code."""
    code = canonical_request_error(err.error_code, profile)
    if profile.param_table >= 20:
        return code == RequestErrorCode.NOT_SUPPORTED
    return code == RequestErrorCode.DUPLICATE_SUBSCRIPTION


def _pending_subscribe_id(session, ns, track_name):
    """Request ID of our newest SUBSCRIBE for the track on `session`."""
    name = (track_name.encode() if isinstance(track_name, str)
            else bytes(track_name))
    for rid in sorted(getattr(session, "_subscriptions", {}), reverse=True):
        msgs = session._subscriptions.get(rid) or ()
        msg = msgs[0] if msgs else None
        if (isinstance(msg, Subscribe) and msg.track_name == name
                and tuple(msg.track_namespace) == tuple(ns)):
            return rid
    return None


def _cancel_upstream_subscribe(session, ns, track_name) -> None:
    rid = _pending_subscribe_id(session, ns, track_name)
    if rid is not None:
        _cancel_request(session, rid)


def _cancel_request(session, rid) -> None:
    """Cancel one of our upstream requests (§3.3.2)."""
    try:
        session.unsubscribe(rid)
    except Exception:
        logger.debug(f"relay: cancelling upstream request {rid} failed",
                     exc_info=True)


def _subscribe_ok_for(session, rid):
    """The SUBSCRIBE_OK `session` received for our request `rid`."""
    return next((m for m in session._subscriptions.get(rid) or ()
                 if isinstance(m, SubscribeOk)), None)


async def _subscribe_upstream(entry) -> None:
    try:
        result = await _subscribe_publishers(entry)
    except Exception:
        logger.exception(f"relay: upstream subscribe for {entry.key} failed")
        result = (None, None)
    entry.resolve(result)


async def _subscribe_publishers(entry):
    key = entry.key
    ns, track_name = key
    refusal = None
    for pub in _publishers_for(ns):
        if entry.fut.done():
            break
        track = _RelayedTrack(key)
        track.attaching = True
        name = (track_name.decode() if isinstance(track_name, bytes)
                else track_name)
        # The tuple, not a "/"-joined string: an element may contain "/".
        upstream = SubscribedTrack(
            pub, ns, name,
            on_object=track.on_object,
            on_done=lambda done, t=track, k=key: _upstream_done(t, k, done))
        entry.pub = pub
        upstream_filter = _filter_for(pub, entry.filter)
        try:
            await upstream.subscribe(timeout=10.0, **upstream_filter)
        except Exception as e:
            if entry.fut.done():
                break
            logger.info(f"relay: upstream subscribe failed on {ns}: {e}")
            if not isinstance(e, MOQTRequestError):
                continue
            refusal = _upstream_refusal(e, pub._profile)
            if _duplicate_refusal(e, pub._profile):
                # Its PUBLISH of the track is on the way; _on_publish
                # resolves the entry when it lands.
                try:
                    await asyncio.wait_for(asyncio.shield(entry.fut),
                                           PUBLISH_WAIT_S)
                except asyncio.TimeoutError:
                    logger.info(f"relay: no PUBLISH of {key} within "
                                f"{PUBLISH_WAIT_S}s of the refusal")
            continue
        if entry.fut.done():
            # A crossing PUBLISH took over; the SUBSCRIBE was cancelled.
            if upstream.track_alias is not None:
                pub.unregister_object_handler(upstream.track_alias)
            break
        rid = getattr(upstream, "request_id", None)
        props = _track_properties(pub, _subscribe_ok_for(pub, rid))
        if _mandatory(pub, props):
            logger.info(f"relay: upstream subscribe {key} -> "
                        f"UNSUPPORTED_EXTENSION {sorted(props)}")
            _cancel_request(pub, rid)
            if upstream.track_alias is not None:
                pub.unregister_object_handler(upstream.track_alias)
            refusal = _MANDATORY_REFUSAL
            continue
        owned = _live_published(key)
        if owned is not None:
            # A PUBLISH from another session landed meanwhile and owns
            # the track: never overwrite it.
            _cancel_upstream_subscribe(pub, ns, track_name)
            if upstream.track_alias is not None:
                pub.unregister_object_handler(upstream.track_alias)
            return owned, None
        track.upstream = upstream
        track.upstream_alias = upstream.track_alias
        track.properties = props
        # §10.2.11: Largest includes what the upstream SUBSCRIBE_OK reports.
        track.note_largest(upstream.largest)
        track.complete_from = _delivery_start(upstream_filter,
                                              upstream.largest)
        unfiltered = ("location_filter" not in upstream_filter
                      and upstream_filter.get("filter_type") is None)
        track.upstream_from = (0, 0) if unfiltered else track.complete_from
        pub.register_stream_end_handler(upstream.track_alias,
                                        track.on_stream_end)
        _tracks[key] = track
        logger.info(f"relay: upstream established ns={ns} track={name} "
                    f"alias={upstream.track_alias}")
        return track, None
    return None, refusal


def _joined_subscription(session, request_id):
    """(key, track, downstream) for this session's subscription
    `request_id`, or None."""
    if request_id is None:
        return None
    for key, track in _tracks.items():
        d = track.find(session, request_id)
        if d is not None:
            return key, track, d
    return None


def _subscribe_pending(session, request_id) -> bool:
    """The peer's SUBSCRIBE `request_id` is admitted and unanswered."""
    held = getattr(session, "_peer_requests", {}).get(request_id)
    return held is not None and held[0] is Subscribe


def _still_wanted(session, request_id) -> bool:
    """False once the subscriber cancelled the request or closed; no
    reply is sent then."""
    if not _session_live(session):
        return False
    if not is_draft16_or_later(session.negotiated_draft):
        return True
    return _subscribe_pending(session, request_id)


async def _await_joined(session, request_id):
    """§10.12.2: hold a joining FETCH until the SUBSCRIBE it names is
    attached. None once that SUBSCRIBE is gone, the session closes, or
    JOINING_WAIT_S passes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JOINING_WAIT_S
    while (_subscribe_pending(session, request_id)
           and _session_live(session) and loop.time() < deadline):
        await asyncio.sleep(0.02)
        found = _joined_subscription(session, request_id)
        if found is not None:
            return found
    return None


def _upstream_done(track, key, done) -> None:
    """Upstream ended the track: FIN downstream streams, PUBLISH_DONE
    each subscriber, and drop the fan-out so a later SUBSCRIBE starts a
    fresh upstream subscription."""
    status = getattr(done, "status_code", SubscribeDoneCode.TRACK_ENDED)
    reason = getattr(done, "reason", "") or "track ended"
    logger.info(f"relay: upstream PUBLISH_DONE {key} status={status} "
                f"streams={getattr(done, 'stream_count', 0)} "
                f"-> {len(track.downstream)} subscriber(s), objects "
                f"in={track._objects_in} out={track._objects_out}, "
                f"upstream streams ended={track._upstream_ended}")
    if track.task is None and not track.attaching:
        # Nothing queued and no subscriber attached.
        track.finish(status_code=status, reason=reason)
        track.close()
        _tracks.pop(key, None)
        return
    track.note_upstream_done(status, reason, key,
                             getattr(done, "stream_count", 0))


def _published_track(session, request_id):
    """(key, track) for the PUBLISH `request_id` on `session`."""
    for key, track in list(_tracks.items()):
        pending = track.pending_publish
        if pending is not None and pending[0] is session and \
                getattr(pending[1], "request_id", None) == request_id:
            return key, track
        if _upstream_session(track) is session and \
                track.upstream_request_id == request_id:
            return key, track
    return None, None


async def _on_publish_done(session, msg):
    """§10.11: a publisher ends a track it PUBLISHed.

    The terminal keys off this rather than off session close, which can
    lag by the peer's whole idle timeout — a track still registered in
    that window is handed to the next subscriber, which gets nothing.
    """
    key, track = _published_track(session, msg.request_id)
    if track is not None:
        status = getattr(msg, "status_code", SubscribeDoneCode.TRACK_ENDED)
        reason = getattr(msg, "reason", "") or "track ended"
        logger.info(f"relay: publisher PUBLISH_DONE {key} status={status} "
                    f"streams={getattr(msg, 'stream_count', 0)} -> "
                    f"{len(track.downstream)} subscriber(s), objects "
                    f"in={track._objects_in} out={track._objects_out}")
        if track.task is None:
            # Nothing queued and no subscriber attached.
            track.finish(status_code=status, reason=reason)
            track.close()
            _tracks.pop(key, None)
        else:
            track.note_upstream_done(status, reason, key,
                                     getattr(msg, "stream_count", 0))
    # The default handler releases the publisher's subgroup streams.
    await session._handle_subscribe_done(msg)


async def _on_publish(session, msg):
    """Flow B: a publisher offers a track with no prior
    PUBLISH_NAMESPACE (transport 0x1D, present in d14/d16/d18 alike).

    The PUBLISH_OK is held while SUBSCRIBE_TRACKS subscribers are offered
    the track; otherwise it is answered with Forward State 0 until a
    subscriber wants objects (§9.5).
    """
    ns = _ns_tuple(msg.track_namespace)
    key = (ns, msg.track_name)
    props = _track_properties(session, msg)
    if _mandatory(session, props):
        logger.info(f"relay: publish {key} -> UNSUPPORTED_EXTENSION "
                    f"{sorted(props)}")
        code, reason = _MANDATORY_REFUSAL
        _refuse_publish(session, msg, reason, code=code)
        return
    track = _tracks.get(key)
    if track is not None:
        owner = _upstream_session(track) or (
            track.pending_publish[0] if track.pending_publish else None)
        if (owner is session and not track.finished
                and not _is_session(track.upstream)
                and track.upstream is not None):
            # §5.1: our SUBSCRIBE already holds this session's
            # subscription to the track.
            logger.info(f"relay: publish {key} -> DUPLICATE_SUBSCRIPTION "
                        f"(upstream subscription established)")
            _refuse_publish(session, msg, "track already subscribed")
            return
        if track.finished or owner is not session:
            # One publisher per Full Track Name. The predecessor's
            # close may not have been observed yet, and reusing its
            # track would drop everything this publisher sends.
            logger.info(f"relay: retiring the track registered for {key}")
            track.finish(reason="superseded by a new publisher")
            track.close()
            _tracks.pop(key, None)
            track = None
    if track is None:
        track = _RelayedTrack(key)
        _tracks[key] = track
    entry = _establishing.get(key)
    crossing = (entry is not None and entry.pub is session
                and not entry.fut.done())
    if crossing:
        # §5.1: this PUBLISH crossed our SUBSCRIBE on the same session;
        # end the SUBSCRIBE and serve the waiters from the PUBLISH.
        logger.info(f"relay: publish {key} crossed our upstream SUBSCRIBE; "
                    f"cancelling it")
        _cancel_upstream_subscribe(session, ns, msg.track_name)
    _watch_session(session, ns)
    track.pending_publish = (session, msg)
    track.properties = props
    # §9.4: a bare PUBLISH is the only notice d18 prefix subscribers get
    # that this namespace exists.
    _announce_namespace(ns)
    logger.info(f"relay: publish ns={ns} track={msg.track_name} "
                f"alias={msg.track_alias}")
    offered = False
    for sub_session, prefix, rid in list(_track_subs):
        if (_prefix_covers(prefix, ns)
                and _track_sub_admits(sub_session, rid, track)):
            offered = True
            asyncio.create_task(
                _offer_track(sub_session, track, key, rid))
    if crossing:
        _answer_publish(track, forward=any(entry.forwards))
        entry.resolve((track, None))
        return
    if track.downstream or not offered:
        # With nobody to offer this track to, no reply is coming from
        # anywhere: answer now rather than hold. A publisher that
        # blocks on PUBLISH_OK before subscribing (§9.5 publish-first)
        # would otherwise deadlock — it cannot subscribe until we
        # answer, and we would not answer until it subscribed.
        _answer_publish(track)
        if _probing(track) and not track.downstream:
            _spawn(_probe_catalog_head(track))


def _forwarding(track) -> bool:
    """A subscriber, attached or held for a publisher, wants objects."""
    return (any(d.forward for d in track.downstream)
            or any(_held_forwards.get(track.key, ())))


def _answer_publish(track, forward=None) -> None:
    """Answer a held PUBLISH and start taking objects: Forward State 1
    when a subscriber forwards (§9.5), else 0 until one does."""
    if track.pending_publish is None or track.upstream is not None:
        return
    session, msg = track.pending_publish
    if forward is None:
        forward = _forwarding(track) or _probing(track)
    session._track_aliases[msg.track_alias] = msg.request_id
    session.register_object_handler(msg.track_alias, track.on_object)
    session.register_stream_end_handler(msg.track_alias, track.on_stream_end)
    track.publish_alias = msg.track_alias
    if getattr(msg, "content_exists", 0):
        track.note_largest((msg.largest_group_id, msg.largest_object_id))
    if forward:
        track.complete_from = _publish_ok_location(msg)["start"]
    # From d20 PUBLISH_OK carries only EXPIRES and Forward State follows
    # in a REQUEST_UPDATE; publish_ok() sends the draft's shape.
    session.publish_ok(msg, forward=int(forward), priority=128,
                       group_order=GroupOrder.ASCENDING,
                       filter_type=FilterType.LATEST_OBJECT)
    logger.info(f"relay: PUBLISH_OK forward={int(forward)} "
                f"alias={msg.track_alias}"
                + ("" if forward else " — paused until a subscriber forwards"))
    track.upstream = session
    track.upstream_request_id = msg.request_id
    track.paused = not forward
    track.publish_msg = msg
    track.pending_publish = None
    _ensure_pause_sweep()


def _refuse_publish(session, msg, reason, code=None) -> None:
    """Refuse a PUBLISH, by default DUPLICATE_SUBSCRIPTION (NOT_SUPPORTED
    from d20); PUBLISH_ERROR at d14."""
    prof = session._profile
    if code is None:
        code = (RequestErrorCode.NOT_SUPPORTED if prof.param_table >= 20
                else RequestErrorCode.DUPLICATE_SUBSCRIPTION)
    if is_draft16_or_later(session.negotiated_draft):
        session.subscribe_error(msg.request_id, error_code=code,
                                reason=reason)
        return
    reply = PublishError(request_id=msg.request_id,
                         error_code=request_error_code(code, prof),
                         reason=reason)
    session.send_control_message(reply)


def _publish_ok_location(msg) -> dict:
    """The d14 SUBSCRIBE_UPDATE fields our PUBLISH_OK set: a Largest
    Object filter (§5.1.2), open-ended, priority 128."""
    start = ((msg.largest_group_id, msg.largest_object_id + 1)
             if getattr(msg, "content_exists", 0) else (0, 0))
    return {"start": start, "end_group": 0, "priority": 128}


def _upstream_request(track):
    """(session, request id, d14 SUBSCRIBE_UPDATE location) of the
    request feeding `track`, or None."""
    up = track.upstream
    if _is_session(up):
        msg = track.publish_msg
        if msg is None:
            return None
        return up, msg.request_id, _publish_ok_location(msg)
    session = getattr(up, "session", None)
    rid = getattr(up, "request_id", None)
    if session is None or rid is None:
        return None
    return session, rid, {}


def _resume_paused(track, wanted=False):
    """Raise a paused upstream's Forward State once a subscriber
    forwards, or is about to (`wanted`). With `wanted`, returns the
    awaitable reply when the upstream draft has one (d16+), else None."""
    if not track.paused or not (wanted or _forwarding(track)):
        return None
    req = _upstream_request(track)
    if req is None:
        return None
    session, rid, location = req
    wait = wanted and not session._profile.update_has_location
    if track.lowered and track.largest is not None:
        track.fence = track.largest[0]
    track.paused = track.lowered = False
    track.unwatched_since = None
    if track._objects_in:
        track.complete_from = None
        track._resync = True
    elif track.publish_msg is not None:
        track.complete_from = location["start"]
    try:
        reply = session.request_update(rid, forward=1, wait_response=wait,
                                       **location)
        logger.info(f"relay: resumed {track.key} upstream: REQUEST_UPDATE "
                    f"forward=1 request {rid}")
    except Exception:
        logger.debug("relay: raising the upstream forward state failed",
                     exc_info=True)
        return None
    return reply if wait else None


# Track key -> task taking the Largest from a resume's REQUEST_OK.
_resuming: dict = {}


async def _resume_for(track, msg) -> None:
    """Resume a paused upstream for a forwarding SUBSCRIBE before it is
    answered, so its reply and joining FETCH resolve against the
    publisher's live Largest rather than the one at the pause or the
    PUBLISH."""
    if not track.paused or (msg.forward is not None and not msg.forward):
        return
    task = _resuming.get(track.key)
    if task is None:
        reply = _resume_paused(track, wanted=True)
        if reply is None:
            return
        task = asyncio.create_task(_take_live_largest(track, reply))
        _resuming[track.key] = task
        task.add_done_callback(
            lambda t, k=track.key: _resuming.pop(k, None)
            if _resuming.get(k) is t else None)
    # Objects the resume brings are held for this subscriber, whose
    # filter resolves against the Largest the publisher reports.
    track.attaching = True
    try:
        await asyncio.shield(task)
    finally:
        track.attaching = False
        track._ensure_drain()


async def _take_live_largest(track, reply) -> None:
    """Note the Largest a resume's REQUEST_OK reports, waiting at most
    RESUME_REPLY_S."""
    try:
        ok = await asyncio.wait_for(reply, RESUME_REPLY_S)
    except TimeoutError:
        logger.warning(f"relay: resume {track.key}: no REQUEST_OK within "
                       f"{RESUME_REPLY_S:g}s; answering with the cached "
                       f"Largest {track.largest}")
        return
    except MOQTRequestError as e:
        logger.info(f"relay: resume {track.key}: upstream refused the "
                    f"update: {e}")
        return
    track.note_largest(_update_largest(ok))


def _update_largest(reply):
    """The LARGEST_OBJECT a REQUEST_OK carries: a Location from d18, a
    length-prefixed pair of varints at d16; None without one."""
    value = (getattr(reply, "parameters", None) or {}).get(
        ParamType.LARGEST_OBJECT)
    if isinstance(value, (tuple, list)):
        return tuple(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        buf = Buffer(data=bytes(value))
        try:
            return (buf.pull_uint_var(), buf.pull_uint_var())
        except BufferReadError:
            return None
    return None


def _pause_upstream(track) -> None:
    """Lower the upstream Forward State of a track nobody forwards."""
    track.unwatched_since = None
    req = _upstream_request(track)
    if req is None or not _session_live(req[0]):
        return
    session, rid, location = req
    try:
        session.request_update(rid, forward=0, **location)
    except Exception:
        logger.debug("relay: lowering the upstream forward state failed",
                     exc_info=True)
        return
    track.paused = True
    track.lowered = True
    logger.info(f"relay: paused {track.key} upstream, no forwarding "
                f"subscriber for {_pause_after:g}s: REQUEST_UPDATE "
                f"forward=0 request {rid}")


def _pausable(track) -> bool:
    """Pulled from upstream with no subscriber forwarding it."""
    if (track.paused or track.finished or track._closed or track.attaching
            or track.pending_publish is not None or track.upstream is None
            or track.key in _establishing or _probing(track)):
        return False
    return not (any(d.forward and _session_live(d.session)
                    for d in track.downstream)
                or any(_held_forwards.get(track.key, ())))


async def _sweep_unwatched() -> None:
    """Pause each track left unwatched for --pause-after seconds."""
    loop = asyncio.get_running_loop()
    while _pause_after > 0:
        await asyncio.sleep(min(max(_pause_after / 4, 0.02), 1.0))
        now = loop.time()
        for track in list(_tracks.values()):
            if not _pausable(track):
                track.unwatched_since = None
            elif track.unwatched_since is None:
                track.unwatched_since = now
            elif now - track.unwatched_since >= _pause_after:
                _pause_upstream(track)


def _ensure_pause_sweep() -> None:
    """Run the sweep on this loop while --pause-after is on."""
    global _pause_sweep
    if _pause_after <= 0:
        return
    loop = asyncio.get_running_loop()
    if (_pause_sweep is None or _pause_sweep.done()
            or _pause_sweep.get_loop() is not loop):
        _pause_sweep = loop.create_task(_sweep_unwatched())


def _largest_kwargs(track) -> dict:
    """SUBSCRIBE_OK Largest Location (§10.2.11); none when nothing has
    been published."""
    if track.largest is None:
        return {}
    return {"content_exists": 1,
            "largest_group_id": track.largest[0],
            "largest_object_id": track.largest[1]}


def _resolve_against(msg, seen, track):
    """The Largest a SUBSCRIBE's filter resolves against: the relay's own
    when it arrived (§3.1.4). A Relative Start arriving with none went
    upstream (_upstream_filter) and resolves against the Largest the
    publisher reported."""
    lf = getattr(msg, "location_filter", None)
    if (seen is None and lf is not None
            and lf.type == LocationFilterType.RELATIVE_START):
        return track.largest
    return seen


def _publisher_known(ns: tuple, track_name) -> bool:
    """A publisher could serve this track now: it PUBLISHed the track,
    announced a covering namespace, or the relay dials an origin."""
    track = _tracks.get((ns, track_name))
    if (track is not None and _track_live(track)
            and (track.pending_publish or track.upstream)):
        return True
    return bool(_publishers_for(ns) or _upstream_urls)


def _rendezvous_s(session, msg) -> float:
    """How long to hold this SUBSCRIBE for a publisher; 0 = answer now."""
    if (session.negotiated_draft or 0) < 18:
        return 0.0
    ms = (msg.parameters or {}).get(RENDEZVOUS_TIMEOUT)
    return min(int(ms) / 1000, RENDEZVOUS_MAX_S) if ms else 0.0


async def _await_publisher(session, ns: tuple, track_name,
                           wait_s: float) -> bool:
    """Hold until a publisher for the track appears. False on expiry or
    once the subscriber's session has closed."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while not _publisher_known(ns, track_name):
        if loop.time() >= deadline or not _session_live(session):
            return False
        await asyncio.sleep(0.05)
    return True


def _attach_subscriber(session, msg, track, largest) -> bool:
    """SUBSCRIBE_OK and join the fan-out with the SUBSCRIBE's Forward
    State and filter window, resolved against `largest` (§5.1.2); the
    reply and a joining FETCH take the Largest the relay knows now
    (§10.2.11). False when the End Group is already behind, answered
    INVALID_RANGE."""
    window = _subscription_window(msg, largest)
    if (window[1] is not None and track.largest is not None
            and track.largest[0] > window[1]):
        logger.info(f"relay: subscribe {track.key} -> INVALID_RANGE "
                    f"(end group {window[1]}, largest {track.largest})")
        session.subscribe_error(msg.request_id,
                                error_code=RequestErrorCode.INVALID_RANGE,
                                reason="end group already published")
        track.attaching = False
        track._ensure_drain()
        return False
    _watch_session(session, track.key[0])
    _ensure_pause_sweep()
    ok = session.subscribe_ok(request_msg=msg, **_largest_kwargs(track),
                              track_properties=track.properties)
    # FORWARD omitted means 1 (§10.2.12).
    forward = msg.forward is None or bool(msg.forward)
    # A window starting at or before Largest is served from the cache
    # first (§5.1.2: the Start MAY be less than the Largest Object);
    # unfiltered starts at {0, 0}.
    replay = track.largest is not None and window[0] <= track.largest
    d = track.add_downstream(session, ok.track_alias, msg.request_id,
                             forward=forward, window=window, replay=replay)
    d.location_filter = getattr(msg, "location_filter", None)
    d.group_order = getattr(msg, "group_order", None)
    d.set_filters(merge_range_filters({}, msg.parameters or {}),
                  track.properties)
    session.register_request_cancel_handler(
        msg.request_id, lambda rid, t=track, s=session: t.drop_session(s))
    _start_fill(track, d, msg)
    return True


def _start_fill(track, d, msg) -> None:
    """Open the fill fetch stream a FILL_PARAMETERS asks for (d22 §3.4.1),
    under `msg`'s Request ID: none while paused or for an empty range,
    reset at once for one that ends at or before the fence."""
    fill = (msg.parameters or {}).get(D20ParamType.FILL_PARAMETERS)
    if (fill is None or not d.forward
            or d.session._profile.param_table < 20):
        return
    rng = fill_range(fill, d.location_filter, track.largest)
    if rng is None:
        return
    sid = id(d.session)
    track._sent_streams[sid] = track._sent_streams.get(sid, 0) + 1
    if d.fence is not None and rng[0][0] <= d.fence:
        if rng[1][0] <= d.fence:
            # Non-empty against the reply's Largest: a failure (§3.4.1).
            logger.info(f"relay: fill {track.key} request {msg.request_id} "
                        f"{rng[0]}..{rng[1]} at or before fence "
                        f"{d.fence}, reset")
            task = asyncio.create_task(_fail_fill(d.session, msg.request_id))
            d.fills.add(task)
            task.add_done_callback(d.fills.discard)
            return
        rng = ((d.fence + 1, 0), rng[1])
    # FILL_PARAMETERS overrides the subscription's filters by type (§3.4).
    filters = RangeFilters.from_params(
        merge_range_filters(d.range_params, fill)).for_track(
            track.properties)
    if filters is None:
        filters = _REJECT_ALL
    task = asyncio.create_task(_serve_fill(track, d, msg.request_id, fill,
                                           rng, filters))
    d.fills.add(task)
    task.add_done_callback(d.fills.discard)


async def _serve_fill(track, d, request_id, fill, rng,
                      filters=None) -> None:
    """A fill fetch stream from the cache, else from an upstream FETCH.
    Under FILL_TIMEOUT what neither holds is a Timed-Out gap (§9.20.5);
    without it the fill fails, which is a reset (§3.4.1)."""
    session = d.session
    start, end = rng
    ns, name = track.key
    timeout_ms = fill.get(D20ParamType.FILL_TIMEOUT)
    order = int(fill.get(ParamType.GROUP_ORDER) or d.group_order
                or GroupOrder.ASCENDING)
    objs, source, serving = None, "cache", False
    try:
        if track.cache_covers(start):
            objs = track.cached_range(start, end)
        elif timeout_ms != 0 and _fetch_sources(ns, name):
            wait = FILL_UPSTREAM_S if timeout_ms is None else min(
                timeout_ms / 1000, FILL_UPSTREAM_S)
            rng_msg = SimpleNamespace(
                group_order=order, start_group=start[0],
                start_object=start[1], end_group=end[0],
                end_object=0 if end[1] == U64_MAX else end[1] + 1)
            _, up, _ = await _fetch_upstream(ns, name, rng_msg, timeout=wait)
            if up is not None:
                objs, source = [(o.group_id, o.subgroup_id, o.object_id,
                                 o.payload, o.extensions,
                                 o.publisher_priority, o.status)
                                for o in up], "upstream"
        if objs is None and track.cache_covers(start):
            # Cached while upstream was asked.
            objs = track.cached_range(start, end)
        gap = None
        if objs is None and timeout_ms is not None:
            objs = track.cached_range(start, end)
            g, o = (objs[0][0], objs[0][2]) if objs else (end[0], end[1] + 1)
            gap = FetchObject(group_id=g if o else g - 1,
                              object_id=o - 1 if o else U64_MAX,
                              end_of_range=FETCH_FLAGS_END_TIMED_OUT)
        if objs is None:
            logger.info(f"relay: fill {track.key} request {request_id} "
                        f"{start}..{end} unavailable, reset")
            await _fail_fill(session, request_id)
            return
        objs = _filtered(objs, filters)
        if order == int(GroupOrder.DESCENDING):
            objs = sorted(objs, key=lambda o: (-o[0], o[2]))
        served = _counted_fetch(objs, track, source)
        if gap is not None:
            served = itertools.chain((gap,), served)
        serving = True
        await session.serve_fetch(
            request_id, served, group_order=order,
            stream_priority=_stream_band(session, objs[0][5] if objs else None))
        logger.info(f"relay: fill {track.key} request {request_id} served "
                    f"{len(objs)} object(s) {start}..{end} from {source}")
    except asyncio.CancelledError:
        pass  # serve_fetch reset the stream
    except Exception:
        logger.debug(f"relay: fill for request {request_id} failed",
                     exc_info=True)
        if not serving:
            await _fail_fill(session, request_id)


def _filtered(objs, filters):
    """Fetch tuples (group, subgroup, object, payload, properties,
    priority, status) that pass `filters`."""
    if not filters:
        return objs
    return [o for o in objs if filters.admits(o[1], o[2], o[5], o[4])]


async def _fail_fill(session, request_id) -> None:
    """§3.4.1: a failed fill opens its stream and resets it after the
    FETCH_HEADER, which the reset keeps."""
    try:
        stream_id = await session.open_uni_stream()
        header = FetchHeader(request_id=request_id).serialize(
            prof=session._profile).data
        await session.stream_write_drain(stream_id, header)
        session.stream_reset(stream_id, StreamResetCode.INTERNAL_ERROR,
                             reliable_size=len(header))
    except Exception:
        logger.debug("relay: resetting a fill stream failed", exc_info=True)


async def _on_request_update(session, msg):
    """A subscriber changes its Forward State or filter: REQUEST_UPDATE
    from d16 (§10.9), SUBSCRIBE_UPDATE at d14 (Forward State only).
    Updates to anything but a relayed subscription get the session's
    default reply."""
    d16 = is_draft16_or_later(session.negotiated_draft)
    rid = (msg.existing_request_id if d16
           else msg.subscription_request_id)
    found = _joined_subscription(session, rid)
    held = _held_updates.get((session, rid))
    if d16 and (held is not None
                or (found is None and _subscribe_pending(session, rid))):
        # Answered after the SUBSCRIBE it updates, in arrival order: its
        # reply must not overtake the SUBSCRIBE_OK on the request stream.
        if held is None:
            held = _held_updates[(session, rid)] = []
            _spawn(_release_held_updates(session, rid))
        held.append(msg)
        logger.info(f"relay: update request {msg.request_id} held until "
                    f"SUBSCRIBE {rid} is answered")
        return
    if found is None:
        if d16:
            _update_track_sub_filters(session, rid, msg.parameters or {})
            await session._handle_request_update(msg)
        else:
            await session._handle_subscribe_update(msg)
        return
    _apply_update(session, msg, found, d16)


# (session, SUBSCRIBE request id) -> REQUEST_UPDATEs waiting for it.
_held_updates: dict = {}


async def _release_held_updates(session, rid) -> None:
    """Answer the updates held for SUBSCRIBE `rid` once it is answered:
    applied after its SUBSCRIBE_OK, else given the session's default."""
    deadline = time.monotonic() + HELD_UPDATE_MAX_S
    while (_joined_subscription(session, rid) is None
           and _subscribe_pending(session, rid) and _session_live(session)
           and time.monotonic() < deadline):
        await asyncio.sleep(HELD_UPDATE_POLL_S)
    found = _joined_subscription(session, rid)
    for msg in _held_updates.pop((session, rid), []):
        if found is not None:
            _apply_update(session, msg, found, True)
        else:
            await session._handle_request_update(msg)


def _apply_update(session, msg, found, d16: bool) -> None:
    """Apply an update to a joined downstream subscription and answer it."""
    key, track, d = found
    rid = msg.existing_request_id if d16 else msg.subscription_request_id
    if d16 and not _update_filters(session, msg, track, d):
        return
    if d16:
        forward = (msg.parameters or {}).get(ParamType.FORWARD)
        window = _reply_window(session, msg, track.largest)
        if window is not None:
            d.window = window
        lf = (msg.parameters or {}).get(ParamType.SUBSCRIPTION_FILTER)
        if isinstance(lf, LocationFilter):
            d.location_filter = lf
    else:
        forward = msg.forward
    if forward is not None:
        if forward and not d.forward:
            d.joined_at = track.largest
        d.forward = bool(forward)
    logger.info(f"relay: update {key} request {rid}: "
                f"forward={int(d.forward)} window={d.window}")
    if d16:
        params = ({} if track.largest is None
                  else _largest_object_param(session, track.largest))
        ok = RequestOk(request_id=msg.request_id, parameters=params)
        control_log.info(f"MOQT send: {session._msgstr(ok)}")
        session._send_reply(msg.request_id, ok)
        _start_fill(track, d, msg)
    _resume_paused(track)


def _update_filters(session, msg, track, d) -> bool:
    """Apply a REQUEST_UPDATE's Range Filters to `d` (§3.3.2). Past
    MAX_FILTER_RANGES it fails: INVALID_FILTER, then PUBLISH_DONE
    UPDATE_FAILED (§9.5.1). False when it failed."""
    params = msg.parameters or {}
    if not any(p in RANGE_FILTERS for p in params):
        return True
    merged = merge_range_filters(d.range_params, params)
    fill = params.get(D20ParamType.FILL_PARAMETERS)
    count = range_filter_count(merged) + (
        range_filter_count(merge_range_filters(merged, fill))
        if isinstance(fill, dict) else 0)
    if count > session._max_filter_ranges:
        why = f"more than MAX_FILTER_RANGES {session._max_filter_ranges}"
        logger.info(f"relay: update {track.key} request "
                    f"{msg.existing_request_id} -> INVALID_FILTER ({why})")
        session._send_on_request_stream(msg.request_id, RequestError(
            request_id=msg.request_id,
            error_code=int(D20RequestErrorCode.INVALID_FILTER),
            retry_interval=0, reason=why))
        if d in track.downstream:
            track.downstream.remove(d)
        track._end(d, SubscribeDoneCode.UPDATE_FAILED, None, why)
        return False
    d.set_filters(merged, track.properties)
    return True


async def _on_subscribe(session, msg):
    """Relay a SUBSCRIBE within the session's subscription cap."""
    if _refused_over_cap(session, msg, "subscriptions",
                         MAX_SUBSCRIPTIONS_PER_SESSION,
                         _session_subscriptions(session)):
        return
    _subscribing[id(session)] += 1
    try:
        await _relay_subscribe(session, msg)
    finally:
        _subscribing[id(session)] -= 1
        if _subscribing[id(session)] <= 0:
            del _subscribing[id(session)]


async def _relay_subscribe(session, msg):
    """Relay a SUBSCRIBE: establish upstream, then fan out downstream."""
    ns = _ns_tuple(msg.track_namespace)

    # §10.2.6: hold for a publisher up to RENDEZVOUS_TIMEOUT, then TIMEOUT.
    # Once one appears the SUBSCRIBE proceeds as if it had just arrived.
    wait_s = _rendezvous_s(session, msg)
    if wait_s and not _publisher_known(ns, msg.track_name):
        logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                    f"held up to {wait_s:.3f}s for a publisher")
        key = (ns, msg.track_name)
        wants = msg.forward is None or bool(msg.forward)
        _held_forwards.setdefault(key, []).append(wants)
        try:
            found = await _await_publisher(session, ns, msg.track_name, wait_s)
        finally:
            held = _held_forwards[key]
            held.remove(wants)
            if not held:
                del _held_forwards[key]
        if not found:
            if not _still_wanted(session, msg.request_id):
                return
            logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                        f"-> ERROR (rendezvous timeout)")
            session.subscribe_error(
                msg.request_id, error_code=RequestErrorCode.TIMEOUT,
                reason="no publisher within the rendezvous timeout")
            return

    # Flow B first: a track already offered by PUBLISH is served from
    # that offer, no upstream SUBSCRIBE needed.
    track = _tracks.get((ns, msg.track_name))
    if track is not None and not _track_live(track):
        # Cached from an earlier cycle whose sessions have closed.
        logger.info(f"relay: dropping stale track {(ns, msg.track_name)}")
        track.close()
        _tracks.pop((ns, msg.track_name), None)
        track = None
    if track is not None and track.paused and track.upstream is not None:
        await _resume_for(track, msg)
    # The relay's Largest as it takes the SUBSCRIBE (§3.1.4).
    seen = None if track is None else track.largest
    if track is not None and (track.pending_publish or track.upstream):
        largest = _resolve_against(msg, seen, track)
        await _backfill(track, msg, largest)
        if _attach_subscriber(session, msg, track, largest):
            _answer_publish(track)
            _resume_paused(track)
            logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                        f"-> SUBSCRIBE_OK (published track, fanout="
                        f"{len(track.downstream)})")
        return

    # A dialled origin announces nothing, so an empty announcement table
    # is not proof the track is unavailable. An upstream that has just
    # dropped is redialing, and answering DOES_NOT_EXIST in that window
    # fails every request a back-to-back test suite makes.
    if not _announced_match(ns) and not _upstreams and _upstream_urls:
        await _await_upstream()
    why = "not announced"
    if _announced_match(ns) or _upstreams:
        forward = msg.forward is None or bool(msg.forward)
        track, refusal = await _establish_upstream(ns, msg.track_name,
                                                   forward=forward, sub=msg)
        if not _still_wanted(session, msg.request_id):
            logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                        f"cancelled before its reply")
            return
        if track is not None:
            largest = _resolve_against(msg, seen, track)
            await _backfill(track, msg, largest)
            if _attach_subscriber(session, msg, track, largest):
                _answer_publish(track)
                _resume_paused(track)
                logger.info(f"relay: subscribe ns={ns} "
                            f"track={msg.track_name} -> SUBSCRIBE_OK "
                            f"(fanout={len(track.downstream)})")
            return
        if refusal is not None:
            # The publisher's own answer, in this subscriber's draft.
            code, reason = refusal
            logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                        f"-> {code.name} (upstream: {reason})")
            session.subscribe_error(msg.request_id, error_code=code,
                                    reason=reason)
            return
        why = "no publisher served it"
    logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                f"-> DOES_NOT_EXIST ({why})")
    session.subscribe_error(msg.request_id,
                            error_code=RequestErrorCode.DOES_NOT_EXIST,
                            reason="track does not exist")


def _find_default_cert():
    candidates = [
        '/certs/cert.pem',
        os.path.join(os.path.dirname(__file__),
                     '..', '..', 'certs', 'cert.pem'),
        os.path.expanduser('~/.local/share/moqt/cert.pem'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.realpath(c)
    return None


def _find_default_key(cert_path):
    if not cert_path:
        return None
    for name in ('priv.key', 'key.pem'):
        candidate = os.path.join(os.path.dirname(cert_path), name)
        if os.path.exists(candidate):
            return candidate
    return None


def transport_settings(args) -> dict:
    """MOQTServer transport settings from the relay's flags."""
    return {"congestion_control_algorithm": args.cc_algo,
            "max_connections": args.max_connections,
            "stream_ring_cap": args.stream_ring_cap,
            "tx_max_inflight_bytes": args.max_inflight_bytes}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="aiomoqt-relay: a MoQT relay (raw QUIC and WebTransport)",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", type=str, default=None, metavar="PATH",
                        help="Read flags from a TOML file. Keys are long "
                             "flag names: cache-bytes = 268435456, "
                             "catalog-probe = true, upstream = [\"URL\"]. "
                             "Flags on the command line override it.")
    parser.add_argument("--bind", type=str, default=None,
                        help="Bind address (default: all interfaces, IPv4 and IPv6)")
    parser.add_argument("--port", type=int, default=4443,
                        help="UDP listen port (default: 4443)")
    parser.add_argument("--cert", type=str, default=None,
                        help="TLS cert PEM "
                             "(default: /certs/cert.pem)")
    parser.add_argument("--key", type=str, default=None,
                        help="TLS key PEM "
                             "(default: /certs/priv.key)")
    parser.add_argument("--quic", action="store_true",
                        help="Serve raw QUIC instead of WebTransport")
    parser.add_argument("--dual", action="store_true",
                        help="Serve raw QUIC AND H3/WebTransport on "
                             "--port (single-port ALPN dispatch); "
                             "excludes --quic/--quic-port")
    parser.add_argument("--quic-port", type=int, default=None,
                        help="Also serve raw QUIC on this port via a "
                             "second listener in the same process "
                             "(shares the namespace table) — lets one "
                             "instance back both remote-webtransport "
                             "(--port) and remote-quic (--quic-port)")
    parser.add_argument("--draft", type=parse_draft_spec,
                        default=parse_draft_spec(_RELAY_DRAFT_DEFAULT),
                        help="MoQT draft(s) to serve: a single draft confines "
                             "negotiation to it; a list offers all of them. "
                             "Default from $DRAFT / $MOQT_DRAFT, else the "
                             "library default (MOQT_DEFAULT_DRAFTS).")
    parser.add_argument("--upstream", action="append", default=[],
                        metavar="URL",
                        help="Origin to dial for tracks no inbound "
                             "publisher serves, e.g. "
                             "moqt://host:4433/ or https://host:443/moq. "
                             "Repeat for several. Without it the relay "
                             "only serves publishers that connect to it.")
    parser.add_argument("--watchdog", type=float, default=0, metavar="SEC",
                        help="Exit, for a supervisor to restart, when the "
                             "event loop stalls for SEC seconds or "
                             f"{WATCHDOG_MISSES} self-probes in a row fail "
                             "(a child process relays one object over "
                             "loopback every SEC seconds). 0 = off "
                             "(default).")
    parser.add_argument("--catalog-probe", action="store_true",
                        help="Subscribe to each announced or published "
                             "namespace's catalog track at once, so the "
                             "dashboard shows its MSF format before anyone "
                             "plays it. Skips namespaces starting moq-test.")
    parser.add_argument("--pause-after", type=float, default=PAUSE_AFTER_S,
                        metavar="SEC",
                        help="Pause a track upstream (Forward State 0) once "
                             "no subscriber has forwarded it for SEC "
                             "seconds; the next subscriber resumes it. "
                             "--catalog-probe catalogs keep flowing. "
                             f"0 = never. Default: {PAUSE_AFTER_S:g}")
    parser.add_argument("--goaway-uri", type=str, default="", metavar="URL",
                        help="New Session URI sent in GOAWAY on SIGTERM or "
                             "SIGINT. Default: empty, peers reconnect to "
                             "the same address")
    parser.add_argument("--drain-timeout", type=float, default=5.0,
                        metavar="SEC",
                        help="On SIGTERM or SIGINT, wait up to SEC seconds "
                             "after GOAWAY for sessions to leave before "
                             "exiting. Default: 5")
    parser.add_argument("--control", type=str, default=None, metavar="PATH",
                        help="Serve HTTP on the Unix socket PATH: GET /debug "
                             "reports DEBUG logging, POST /debug toggles it")
    parser.add_argument("--status-dir", type=str, default=None, metavar="DIR",
                        help="Write DIR/status.json every "
                             f"{STATUS_INTERVAL_S:g}s for the dashboard: "
                             "namespaces, tracks, subscribers, bandwidth, "
                             "host metrics. Off by default.")
    parser.add_argument("--log-dir", type=str, default=None, metavar="DIR",
                        help="Also log to DIR/relay.log (rotated, "
                             f"{LOG_FILE_BYTES >> 20} MB x {LOG_FILE_BACKUPS}) "
                             "and send crash and stall dumps to "
                             "DIR/crash.log. Off by default.")
    parser.add_argument("--cc-algo", type=str, default="bbr",
                        help="Congestion control (bbr | bbr1 | c4 | cubic | "
                             "dcubic | fast | newreno | prague). Default: "
                             "bbr")
    parser.add_argument("--max-connections", type=int, default=4096,
                        help="Most connections held at once (aiopquic "
                             ">= 0.5.1). Default: 4096")
    parser.add_argument("--stream-ring-cap", type=int, default=1 << 20,
                        metavar="BYTES",
                        help="Per-stream send buffer, allocated whole for "
                             "each open stream; must exceed the largest "
                             "object. Default: 1 MiB")
    parser.add_argument("--max-inflight-bytes", type=int, default=512 << 10,
                        metavar="BYTES",
                        help="Per-stream send budget, below "
                             "--stream-ring-cap. Default: 512 KiB")
    parser.add_argument("--cache-bytes", type=int,
                        default=CACHE_BUDGET_BYTES, metavar="BYTES",
                        help="Object cache budget across all tracks; past "
                             "it the least recently used tracks lose their "
                             "oldest groups. Default: 256 MiB")
    parser.add_argument("--max-namespaces-per-session", type=int,
                        default=MAX_NAMESPACES_PER_SESSION, metavar="N",
                        help="PUBLISH_NAMESPACE, SUBSCRIBE_NAMESPACE and "
                             "SUBSCRIBE_TRACKS requests one session may "
                             "hold; more are refused with EXCESSIVE_LOAD. "
                             f"Default: {MAX_NAMESPACES_PER_SESSION}")
    parser.add_argument("--max-subscriptions-per-session", type=int,
                        default=MAX_SUBSCRIPTIONS_PER_SESSION, metavar="N",
                        help="SUBSCRIBEs one session may hold; more are "
                             "refused with EXCESSIVE_LOAD. Default: "
                             f"{MAX_SUBSCRIPTIONS_PER_SESSION}")
    parser.add_argument("--stream-scheduling", choices=SCHEDULING,
                        default=STREAM_SCHEDULING,
                        help="Order among data streams of one priority: "
                             "fifo keeps each track's groups in order, "
                             "round_robin shares the band. Default: fifo")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging (= --log-level debug). "
                             "SIGUSR1 steps the level at runtime: INFO, "
                             "DEBUG, TRACE, INFO")
    add_logging(parser)
    add_help(parser)
    return _parse_with_config(parser, argv)


def _parse_with_config(parser, argv=None):
    """Parse `argv`, filling flags it does not give from --config."""
    args = parser.parse_args(argv)
    if args.config is None:
        return args
    try:
        with open(args.config, "rb") as f:
            table = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        parser.error(f"--config {args.config}: {e}")
    given = _given_dests(parser, argv)
    for key, value in table.items():
        action = _config_action(parser, key)
        if action is None:
            parser.error(f"--config {args.config}: unknown key '{key}'")
        try:
            value = _config_value(action, value)
        except (TypeError, ValueError, argparse.ArgumentTypeError) as e:
            parser.error(f"--config {args.config}: {key}: {e}")
        if action.dest not in given:
            setattr(args, action.dest, value)
    return args


def _given_dests(parser, argv) -> set:
    """Dests the command line itself sets."""
    saved = [(a, a.default) for a in parser._actions]
    for a, _ in saved:
        a.default = argparse.SUPPRESS
    try:
        return set(vars(parser.parse_args(argv)))
    finally:
        for a, default in saved:
            a.default = default


def _config_action(parser, key):
    """The action a --config key names: a long flag, not --config or
    --help."""
    for a in parser._actions:
        if (f"--{key}" in a.option_strings and a.dest != "config"
                and not isinstance(a, argparse._HelpAction)):
            return a
    return None


def _config_value(action, value):
    """A TOML value as the action would store it."""
    if isinstance(action, (argparse._StoreTrueAction,
                           argparse._StoreFalseAction,
                           argparse._StoreConstAction)):
        if not isinstance(value, bool):
            raise TypeError(f"expected true or false, got {value!r}")
        return action.const if value else action.default
    if isinstance(action, argparse._AppendAction) or action.nargs in (
            "*", "+") or isinstance(action.nargs, int):
        if not isinstance(value, list):
            raise TypeError(f"expected a list, got {value!r}")
        return [_config_scalar(action, v) for v in value]
    return _config_scalar(action, value)


def _config_scalar(action, value):
    """One TOML scalar through the action's type and choices."""
    kind = action.type
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"expected a {getattr(kind, '__name__', 'string')}, "
                        f"got {value!r}")
    if kind is int:
        if not isinstance(value, int):
            raise TypeError(f"expected an integer, got {value!r}")
    elif kind is float:
        if isinstance(value, str):
            raise TypeError(f"expected a number, got {value!r}")
        value = float(value)
    elif kind is None or kind is str:
        if not isinstance(value, str):
            raise TypeError(f"expected a string, got {value!r}")
    else:
        value = kind(str(value))
    if action.choices is not None and value not in action.choices:
        raise ValueError(f"{value!r} is not one of "
                         f"{', '.join(map(str, action.choices))}")
    return value


# SUBSCRIBE_TRACKS subscribers: (session, prefix_tuple, request_id).
_track_subs: list = []
# (id(session), request_id) -> a SUBSCRIBE_TRACKS's Range Filter
# parameters: TRACK_PROPERTY_FILTER selects the tracks offered, the rest
# filter their objects (§3.6.1).
_track_sub_filters: dict = {}
# (session, prefix, request_id) for SUBSCRIBE_NAMESPACE subscribers sent
# NAMESPACE / NAMESPACE_DONE: d18 on, and d16 when Subscribe Options ask.
_ns_subs: list = []
# (session, prefix, request_id) for d14 SUBSCRIBE_NAMESPACE subscribers,
# forwarded PUBLISH_NAMESPACE / PUBLISH_NAMESPACE_DONE.
_announce_subs: list = []
# d16 SUBSCRIBE_NAMESPACE Subscribe Options.
_SUBSCRIBE_OPTIONS_NAMESPACE = 0x01
_SUBSCRIBE_OPTIONS_BOTH = 0x02


def _prefix_covers(prefix, ns) -> bool:
    """§2.4 field-wise prefix match (empty prefix matches everything)."""
    return ns[:len(prefix)] == prefix


def _track_sub_admits(session, request_id, track) -> bool:
    """The track passes the SUBSCRIBE_TRACKS's TRACK_PROPERTY_FILTERs."""
    params = _track_sub_filters.get((id(session), request_id))
    return not params or RangeFilters.from_params(params).for_track(
        track.properties) is not None


def _update_track_sub_filters(session, request_id, params) -> None:
    """A REQUEST_UPDATE to a SUBSCRIBE_TRACKS: its filters apply to the
    tracks offered from now on."""
    key = (id(session), request_id)
    if key in _track_sub_filters and any(p in RANGE_FILTERS for p in params):
        _track_sub_filters[key] = merge_range_filters(
            _track_sub_filters[key], params)


async def _offer_track(session, track, key, request_id=None):
    """§9.5: send a PUBLISH for a held/served track to a
    SUBSCRIBE_TRACKS subscriber; on PUBLISH_OK wire it into the fan-out
    with the Forward State and filter it chose, under the Range Filters
    of SUBSCRIBE_TRACKS `request_id`."""
    if not _session_live(session):
        _forget_session(session)
        return
    ns, name = key
    # A session that subscribed to a prefix gets every track under it,
    # including one it publishes itself: the subscription is explicit,
    # and moq-test's publish cases use one session for both roles.
    pub_msg = session.publish(
        namespace=ns,
        track_name=(name.decode() if isinstance(name, bytes) else name),
        forward=1, largest=track.largest,
        track_properties=track.properties)
    fut = session._loop.create_future()
    session._pending_requests[pub_msg.request_id] = fut
    try:
        reply = await session._await_response(pub_msg.request_id)
    except MOQTRequestError as e:
        logger.info(f"relay: PUBLISH offer declined for {key}: {e}")
        return
    forward = getattr(reply, 'forward', None)
    if forward is None:
        # §10.2.12: FORWARD omitted from PUBLISH_OK means 1.
        forward = (getattr(reply, 'parameters', None) or {}).get(
            ParamType.FORWARD, 1)
    window = _reply_window(session, reply, track.largest) or _UNFILTERED
    # Cached objects inside the subscriber's window go first.
    d = track.add_downstream(session, pub_msg.track_alias,
                             pub_msg.request_id, forward=bool(forward),
                             window=window, replay=track.largest is not None)
    d.set_filters(_track_sub_filters.get((id(session), request_id), {}),
                  track.properties)
    session.register_request_cancel_handler(
        pub_msg.request_id,
        lambda rid, t=track, s=session: t.drop_session(s))
    _answer_publish(track)
    _resume_paused(track)
    logger.info(f"relay: PUBLISH offer accepted for {key} "
                f"alias={pub_msg.track_alias} forward={int(bool(forward))} "
                f"(fanout={len(track.downstream)})")


def _reply_window(session, reply, largest):
    """The filter window a PUBLISH_OK or REQUEST_UPDATE asks for against
    `largest`, None when it carries no filter: fields at d14, a parameter
    from d16, a Location Filter from d20."""
    if getattr(reply, "filter_type", None) is not None:
        return _filter_window(reply.filter_type, reply.start_group,
                              reply.start_object, reply.end_group, largest)
    raw = (getattr(reply, "parameters", None) or {}).get(
        ParamType.SUBSCRIPTION_FILTER)
    if raw is None:
        return None
    if isinstance(raw, LocationFilter):
        return _location_window(raw, largest)
    return _filter_window(*MOQTMessage._decode_filter(
        raw, prof=session._profile), largest)


# id(session) -> datagram objects dropped for not fitting that session.
_dgram_drops: dict = {}


def _send_datagram(session, buf, alias, gid, oid) -> None:
    """Send one datagram object, or drop it silently when it exceeds the
    session's datagram limit (0 = no datagram TX): an object keeps its
    forwarding preference, never moving to a stream."""
    wire = buf.data
    limit = session.datagram_max_payload()
    why = f"{len(wire)}B > {limit}B"
    if len(wire) <= limit:
        try:
            session.send_dgram_message(buf)
            return
        except (NotImplementedError, ValueError) as e:
            why = f"{len(wire)}B: {e}"
    drops = _dgram_drops.get(id(session), 0) + 1
    _dgram_drops[id(session)] = drops
    if drops == 1:
        logger.warning(f"relay: dropping datagram objects that do not fit "
                       f"this session (alias={alias}, {why})")
    elif logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"relay: datagram object {gid}.{oid} alias={alias} "
                     f"dropped ({why})")


# (id(session), request_id) -> objects collected from an upstream FETCH.
_fetch_sinks: dict = {}


def _install_fetch_sink(pub) -> None:
    """One per-session fetch-object callback, demultiplexed by request
    id, so several upstream fetches can run at once."""
    if getattr(pub, "_relay_fetch_sink", False):
        return
    pub._relay_fetch_sink = True

    def _on(obj, size, ts, request_id):
        sink = _fetch_sinks.get((id(pub), request_id))
        if sink is not None:
            sink.append(obj)

    pub.on_fetch_object = _on


# Relay-wide FETCH payload bytes by source, including tracks with no state.
_fetch_bytes = {"cache": 0, "upstream": 0}


def _counted_fetch(objs, track, source):
    """FetchObjects for serve_fetch(); each payload is counted once the
    next is asked for, i.e. written with the FETCH not cancelled."""
    for gid, sgid, oid, payload, exts, prio, status in objs:
        payload = payload or b""
        yield FetchObject(group_id=gid, subgroup_id=sgid, object_id=oid,
                          publisher_priority=prio, extensions=exts,
                          payload=payload, status=status)
        _fetch_bytes[source] += len(payload)
        if track is not None:
            track._fetch_bytes[source] += len(payload)


async def _fetch_upstream(ns, track_name, msg, timeout=10.0):
    """Ask each source of the track for the requested range. Returns its
    FETCH_OK, the objects and None, or (None, None, the last upstream
    refusal or None) when nobody serves it —
    the relay keeps only recent objects, so a historical range has to
    come from the origin."""
    name = (track_name.decode() if isinstance(track_name, bytes)
            else track_name)
    refusal = None
    for pub in _fetch_sources(ns, track_name):
        _install_fetch_sink(pub)
        req = pub.fetch(
            namespace=ns,
            track_name=name,
            group_order=msg.group_order or GroupOrder.ASCENDING,
            start_group=msg.start_group or 0,
            start_object=msg.start_object or 0,
            end_group=msg.end_group or 0,
            end_object=msg.end_object or 0,
        )
        key = (id(pub), req.request_id)
        _fetch_sinks[key] = []
        try:
            ok = await pub._await_response(req.request_id, timeout=timeout)
            mandatory = _mandatory(pub, _track_properties(pub, ok))
            if not mandatory:
                await pub.await_fetch_done(req.request_id, timeout=timeout)
        except asyncio.CancelledError:
            _fetch_sinks.pop(key, None)
            raise
        except (MOQTRequestError, asyncio.TimeoutError) as e:
            logger.info(f"relay: upstream FETCH declined on {ns}: {e}")
            _fetch_sinks.pop(key, None)
            if isinstance(e, MOQTRequestError):
                refusal = _upstream_refusal(e, pub._profile, fetch=True)
            continue
        if mandatory:
            logger.info(f"relay: upstream FETCH {ns}/{name} -> "
                        f"UNSUPPORTED_EXTENSION")
            _fetch_sinks.pop(key, None)
            _cancel_request(pub, req.request_id)
            refusal = _MANDATORY_REFUSAL
            continue
        objs = _fetch_sinks.pop(key, [])
        logger.info(f"relay: upstream FETCH {ns}/{name} -> "
                    f"{len(objs)} object(s) end_of_track="
                    f"{getattr(ok, 'end_of_track', 0)}")
        return ok, objs, None
    return None, None, refusal


def _fetch_range(msg, largest):
    """(start, end) Locations a FETCH asks for, both inclusive, the end at
    most `largest`: (group, 2^64-1) for a whole End Group below it, None
    for no End with nothing published. d14-d18 End is the last Object
    plus one, Object 0 the whole End Group."""
    if msg.location_filter is not None:
        return msg.location_filter.resolve(largest, fetch=True)
    start = (msg.start_group or 0, msg.start_object or 0)
    if msg.end_group is None:
        end = None
    elif not msg.end_object:
        end = (msg.end_group, U64_MAX)
    else:
        end = (msg.end_group, msg.end_object - 1)
    if largest is not None:
        end = largest if end is None else min(end, largest)
    return start, end


async def _on_fetch(session, msg):
    """FETCH (§10.12) served from the track's recent-object cache, or
    from the publisher when the range predates it. A joining FETCH is
    resolved against its subscription and served the same way."""
    # A joining FETCH names no track: both come from its subscription.
    ns, track_name = _ns_tuple(msg.namespace), msg.track_name
    _watch_session(session, ns or None)
    if _is_joining(msg.fetch_type):
        # §10.12.2: namespace, track and End Location come from the
        # associated subscription, so the backfill is contiguous with it.
        found = _joined_subscription(session, msg.joining_request_id)
        if found is None:
            found = await _await_joined(session, msg.joining_request_id)
        if found is None:
            logger.info(f"relay: joining FETCH for unknown subscription "
                        f"{msg.joining_request_id}")
            session.fetch_error(
                request_id=msg.request_id,
                error_code=RequestErrorCode.INVALID_JOINING_REQUEST_ID,
                reason="no such subscription")
            return
        key, track, d = found
        anchor = d.joined_at
        if not d.forward or anchor is None:
            # §10.12.2: Forward State 1 required; with no Joining Location
            # the subscription covers the track from its start.
            logger.info(f"relay: joining FETCH {key} has no anchor "
                        f"(forward={int(d.forward)})")
            session.fetch_error(
                request_id=msg.request_id,
                error_code=RequestErrorCode.INVALID_RANGE,
                reason="subscription has no largest object")
            return
        start_group = int(msg.joining_start or 0)
        if int(msg.fetch_type) == FETCH_RELATIVE_JOINING:
            start_group = max(0, anchor[0] - start_group)
        if d.fence is not None and start_group <= d.fence:
            start_group = d.fence + 1
            if start_group > anchor[0]:
                logger.info(f"relay: joining FETCH {key} -> INVALID_RANGE "
                            f"(nothing since the resume after group "
                            f"{d.fence})")
                session.fetch_error(
                    request_id=msg.request_id,
                    error_code=RequestErrorCode.INVALID_RANGE,
                    reason="nothing published since the resume")
                return
        msg.start_group, msg.start_object = start_group, 0
        msg.end_group, msg.end_object = anchor[0], anchor[1] + 1
        ns, track_name = key
        logger.info(f"relay: joining FETCH {key} groups {start_group}.."
                    f"{anchor[0]} (anchor {anchor})")
    track = _tracks.get((ns, track_name))
    start, end = _fetch_range(msg, None if track is None else track.largest)
    have_cache = (track is not None and _track_live(track)
                  and track.cache_covers(start))
    objs = track.cached_range(start, end) if have_cache else []
    source = "cache"
    props = track.properties if track is not None else {}
    end_of_track = 0
    if objs and objs[-1][6] == ObjectStatus.END_OF_TRACK:
        end_of_track = 1
    if not objs:
        # Only recent objects are held, so a historical range comes from
        # the publisher — which is also the only path for a track this
        # relay has never subscribed to.
        up_ok, upstream, refusal = await _fetch_upstream(ns, track_name, msg)
        if (not upstream and track is not None and _track_live(track)
                and track.cache_covers(start)):
            # Cached while upstream was asked.
            objs = track.cached_range(start, end)
        if objs:
            end_of_track = int(objs[-1][6] == ObjectStatus.END_OF_TRACK)
        elif upstream is None and refusal is not None:
            code, reason = refusal
            logger.info(f"relay: FETCH ns={ns} track={track_name} "
                        f"-> {code.name} (upstream: {reason})")
            session.fetch_error(request_id=msg.request_id, error_code=code,
                                reason=reason)
            return
        elif upstream is None:
            logger.info(f"relay: FETCH ns={ns} track={track_name} "
                        f"-> DOES_NOT_EXIST")
            session.fetch_error(
                request_id=msg.request_id,
                error_code=RequestErrorCode.DOES_NOT_EXIST,
                reason="track does not exist")
            return
        else:
            objs = [(o.group_id, o.subgroup_id, o.object_id, o.payload,
                     o.extensions, o.publisher_priority, o.status)
                    for o in upstream]
            end_of_track = int(getattr(up_ok, 'end_of_track', 0) or 0)
            props = dict(getattr(up_ok, "track_extensions", None) or {})
            source = "upstream"
    if track is not None:
        track._fetches += 1
    if track_name == CATALOG_TRACK:
        for o in objs:
            if o[2] == 0:
                _note_catalog(ns, o[3])
    order = msg.group_order or GroupOrder.ASCENDING
    # FETCH_OK End Location (d22 §9.12): the requested End, at most
    # Largest; a whole End Group ends at its last Object. From upstream,
    # the last Object served, with the publisher's End Of Track; with
    # none served, the publisher's End.
    # fetch_ok() takes End as the last Object plus one at every draft.
    last = (objs[-1][0], objs[-1][2]) if objs else None
    if objs:
        end_loc = last
        if source == "cache" and end is not None:
            end_loc = (end if end[1] != U64_MAX
                       else track.group_end(end[0], start))
        end_next = (end_loc[0], end_loc[1] + 1)
    else:
        end_next = (up_ok.largest_group_id, up_ok.largest_object_id)
        if not end_next[1] and session._profile.fetch_location_filter:
            # A d20+ End is inclusive: no whole-group form.
            end_next = (end_next[0], 1)
    session.fetch_ok(request_id=msg.request_id,
                     end_of_track=end_of_track,
                     largest_group_id=end_next[0],
                     largest_object_id=end_next[1],
                     group_order=int(order), track_extensions=props)
    # Range Filters drop objects, never the FETCH_OK End (§3.2.2).
    objs = _filtered(objs, RangeFilters.from_params(msg.parameters))
    if int(order) == int(GroupOrder.DESCENDING):
        objs = sorted(objs, key=lambda o: (-o[0], o[2]))
    await session.serve_fetch(
        msg.request_id, _counted_fetch(objs, track, source),
        group_order=int(order),
        stream_priority=_stream_band(session, objs[0][5] if objs else None))
    logger.info(f"relay: FETCH ns={ns} track={track_name} served "
                f"{len(objs)} object(s) {start}..{last} from {source}")


async def _on_subscribe_namespace(session, msg):
    """Namespace discovery (§9.4). d18 answers NAMESPACE per namespace
    under the prefix — the subscriber then asks each one for its tracks
    with SUBSCRIBE_TRACKS. d16 sends NAMESPACE, a PUBLISH per matching
    track, or both, as its Subscribe Options ask. d14 forwards matching
    PUBLISH_NAMESPACEs and a PUBLISH per matching track."""
    if _namespaces_full(session, msg):
        return
    prefix = _ns_tuple(msg.namespace_prefix)
    _watch_session(session, prefix)
    session.subscribe_namespace_ok(
        msg, stream_id=session._bidi_streams.get(msg.request_id))

    def _drop(rid, s=session, r=msg.request_id):
        _ns_subs[:] = [e for e in _ns_subs
                       if not (e[0] is s and e[2] == r)]
        _track_subs[:] = [e for e in _track_subs
                          if not (e[0] is s and e[2] == r)]
        _announce_subs[:] = [e for e in _announce_subs
                             if not (e[0] is s and e[2] == r)]
    session.register_request_cancel_handler(msg.request_id, _drop)

    if session._profile.two_level_discovery:
        _ns_subs.append((session, prefix, msg.request_id))
        for ns in _known_namespaces(prefix):
            _offer_namespace(session, msg.request_id, prefix, ns)
        logger.info(f"relay: subscribe-namespace prefix={prefix} "
                    f"(d18 discovery, subs={len(_ns_subs)})")
        return

    options = msg.subscribe_options
    if not is_draft16_or_later(session.negotiated_draft):
        _announce_subs.append((session, prefix, msg.request_id))
        for ns in list(_announced):
            if _prefix_covers(prefix, ns):
                _forward_announce(session, ns)
    elif options in (_SUBSCRIBE_OPTIONS_NAMESPACE, _SUBSCRIBE_OPTIONS_BOTH):
        _ns_subs.append((session, prefix, msg.request_id))
        for ns in _known_namespaces(prefix):
            _offer_namespace(session, msg.request_id, prefix, ns)
        if options == _SUBSCRIBE_OPTIONS_NAMESPACE:
            logger.info(f"relay: subscribe-namespace prefix={prefix} "
                        f"(d16 NAMESPACE, subs={len(_ns_subs)})")
            return

    _track_subs.append((session, prefix, msg.request_id))
    logger.info(f"relay: subscribe-namespace prefix={prefix} "
                f"(publish fan-out, subs={len(_track_subs)})")
    for key, track in list(_tracks.items()):
        if _prefix_covers(prefix, key[0]) and (
                track.pending_publish or track.upstream):
            asyncio.create_task(_offer_track(session, track, key))


def _known_namespaces(prefix) -> list:
    """Namespaces the relay can serve under `prefix`: announced ones
    plus any track it already relays."""
    out, seen = [], set()
    for ns in list(_announced) + [key[0] for key in _tracks]:
        if ns in seen or not _prefix_covers(prefix, ns):
            continue
        seen.add(ns)
        out.append(ns)
    return out


def _offer_namespace(session, request_id, prefix, ns) -> None:
    """One NAMESPACE reply on the SUBSCRIBE_NAMESPACE request stream:
    the suffix under the subscribed prefix (§10.8)."""
    try:
        session.namespace(ns[len(prefix):], request_id=request_id)
    except Exception:
        logger.debug("relay: NAMESPACE offer failed", exc_info=True)


def _announce_namespace(ns) -> None:
    """Tell NAMESPACE subscribers about a namespace that just appeared."""
    for session, prefix, rid in list(_ns_subs):
        if _prefix_covers(prefix, ns) and _session_live(session):
            _offer_namespace(session, rid, prefix, ns)


def _forward_announcements(ns, publisher) -> None:
    """d14: forward a new PUBLISH_NAMESPACE to namespace subscribers."""
    for session, prefix, _rid in list(_announce_subs):
        if session is not publisher and _prefix_covers(prefix, ns):
            _forward_announce(session, ns)


def _forward_announce(session, ns) -> None:
    """One PUBLISH_NAMESPACE to a d14 namespace subscriber, once."""
    if not _session_live(session) or session.is_announced(ns):
        return
    try:
        session.publish_namespace(namespace=ns)
    except Exception:
        logger.debug("relay: PUBLISH_NAMESPACE forward failed", exc_info=True)


def _retract_namespace(ns) -> None:
    """Tell prefix subscribers a namespace has gone (§10.8): NAMESPACE_DONE
    from d16, PUBLISH_NAMESPACE_DONE at d14.

    The counterpart to _announce_namespace: an observer that was told a
    namespace exists otherwise goes on believing it after the last
    publisher withdraws or its session closes.
    """
    for session, prefix, rid in list(_ns_subs):
        if _prefix_covers(prefix, ns) and _session_live(session):
            try:
                session.namespace_done(ns[len(prefix):], request_id=rid)
            except Exception:
                logger.debug("relay: NAMESPACE_DONE failed", exc_info=True)
    for session, prefix, _rid in list(_announce_subs):
        if (_prefix_covers(prefix, ns) and _session_live(session)
                and session.is_announced(ns)):
            try:
                session.publish_namespace_done(namespace=ns)
            except Exception:
                logger.debug("relay: PUBLISH_NAMESPACE_DONE failed",
                             exc_info=True)


async def _on_subscribe_tracks(session, msg):
    """§9.5: a SUBSCRIBE_TRACKS subscriber gets a PUBLISH for every
    matching track, present and future."""
    if _namespaces_full(session, msg):
        return
    prefix = _ns_tuple(msg.namespace_prefix)
    _watch_session(session, prefix)
    session.subscribe_tracks_ok(msg)

    def _drop(rid, s=session, r=msg.request_id):
        _track_subs[:] = [e for e in _track_subs
                          if not (e[0] is s and e[2] == r)]
        _track_sub_filters.pop((id(s), r), None)
    _track_subs.append((session, prefix, msg.request_id))
    filters = merge_range_filters({}, msg.parameters or {})
    if filters:
        _track_sub_filters[(id(session), msg.request_id)] = filters
    session.register_request_cancel_handler(msg.request_id, _drop)
    logger.info(f"relay: subscribe-tracks prefix={prefix} "
                f"(subs={len(_track_subs)})")
    for key, track in list(_tracks.items()):
        if _prefix_covers(prefix, key[0]) and (
                track.pending_publish or track.upstream):
            if not _track_sub_admits(session, msg.request_id, track):
                logger.info(f"relay: {key} filtered out of subscribe-tracks "
                            f"request {msg.request_id}")
                continue
            asyncio.create_task(
                _offer_track(session, track, key, msg.request_id))


async def _on_track_status(session, msg):
    """§10.14: answer TRACK_STATUS from the relay's own table —
    TRACK_STATUS_OK (REQUEST_OK) for a served track, DOES_NOT_EXIST
    otherwise. The reply rides the request's stream at d18."""
    ns = _ns_tuple(msg.track_namespace)
    _watch_session(session, ns)
    track = _tracks.get((ns, msg.track_name))
    if track is None or not _track_live(track):
        logger.info(f"relay: track-status ns={ns} track={msg.track_name} "
                    f"-> DOES_NOT_EXIST")
        err = RequestError(
            request_id=msg.request_id,
            error_code=request_error_code(RequestErrorCode.DOES_NOT_EXIST,
                                          session._profile),
            retry_interval=0,
            reason="track not served here",
        )
        session._send_reply(msg.request_id, err, fin=True)
        return
    largest = track.largest
    params = ({} if largest is None
              else _largest_object_param(session, largest))
    logger.info(f"relay: track-status ns={ns} track={msg.track_name} "
                f"-> OK largest={largest}")
    ok = RequestOk(request_id=msg.request_id, parameters=params,
                   track_properties=track.properties)
    session._send_reply(msg.request_id, ok, fin=True)


def _answered(handler):
    """Every request gets a terminal reply. A handler that raises would
    otherwise kill the control task and leave the request stream open
    with no answer, and the peer waits out its whole timeout — the shape
    of most conformance failures this relay has had."""
    async def _wrapped(session, msg):
        try:
            await handler(session, msg)
        except Exception:
            logger.exception(f"relay: {type(msg).__name__} handler failed")
            request_id = getattr(msg, "request_id", None)
            if request_id is None:
                return
            try:
                session._send_reply(request_id, RequestError(
                    request_id=request_id,
                    error_code=int(RequestErrorCode.INTERNAL_ERROR),
                    retry_interval=0,
                    reason="relay error"), fin=True)
            except Exception:
                logger.debug("relay: error reply failed", exc_info=True)
    _wrapped.__name__ = getattr(handler, "__name__", "handler")
    return _wrapped


def _build_server(bind, port, cert, key, use_quic, draft, **transport):
    """Construct a MOQTServer with the relay's control-plane handlers.
    `transport` passes MOQTServer transport settings through."""
    server = MOQTServer(
        host=bind, port=port,
        certificate=cert, private_key=key,
        path="/",
        use_quic=use_quic,
        supported_drafts=draft,
        quiet_peers=(WATCHDOG_IMPL,),
        max_filter_ranges=MAX_FILTER_RANGES,
        **transport,
    )
    server.stream_scheduling = STREAM_SCHEDULING
    server.register_handler(
        MOQTMessageType.PUBLISH_NAMESPACE, _answered(_on_publish_namespace))
    server.register_handler(
        MOQTMessageType.PUBLISH_NAMESPACE_DONE, _on_publish_namespace_done)
    server.register_handler(
        MOQTMessageType.SUBSCRIBE, _answered(_on_subscribe))
    # Same code point as d16+ REQUEST_UPDATE.
    server.register_handler(
        MOQTMessageType.SUBSCRIBE_UPDATE, _answered(_on_request_update))
    server.register_handler(
        MOQTMessageType.PUBLISH, _answered(_on_publish))
    server.register_handler(
        MOQTMessageType.PUBLISH_DONE, _on_publish_done)
    server.register_handler(
        MOQTMessageType.TRACK_STATUS, _answered(_on_track_status))
    server.register_handler(
        MOQTMessageType.SUBSCRIBE_NAMESPACE, _answered(_on_subscribe_namespace))
    server.register_handler(
        MOQTMessageType.FETCH, _answered(_on_fetch))
    server.register_handler(
        D18MessageType.SUBSCRIBE_TRACKS, _answered(_on_subscribe_tracks))
    return server


# Tasks kept alive for the life of the process.
_background: set = set()
_probe_seq = itertools.count()


class _ProbeTrack(PublishedTrack):
    """One object, for the watchdog's round trip."""

    async def generate(self, session, track_alias):
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(
            track_alias=track_alias, group_id=0, subgroup_id=0,
            publisher_priority=128, extensions_present=True,
            prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        buf = hdr.next_object(payload=b"watchdog", extensions=None,
                              object_id=0)
        session.stream_write(sid, buf.data, end_stream=True)
        self._largest = (0, 0)


async def _self_probe(host, port, use_quic, draft, timeout,
                      full=True) -> bool:
    """Complete SETUP on one loopback session or, `full`, relay one object
    from a publisher session to a subscriber session under a namespace of
    the probe's own."""
    ns = f"aiomoqt-watchdog/{os.getpid()}-{next(_probe_seq)}"
    impl = f"{WATCHDOG_IMPL}{AIOMOQT_VERSION}"
    got = asyncio.get_running_loop().create_future()

    def _on_object(*_):
        if not got.done():
            got.set_result(True)

    async def _round_trip():
        async with contextlib.AsyncExitStack() as stack:
            sessions = []
            for _ in range(2 if full else 1):
                client = MOQTClient(host, port, path="/", use_quic=use_quic,
                                    verify_tls=False, supported_drafts=draft,
                                    implementation=impl)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                sessions.append(s)
            if not full:
                return
            pub, sub = sessions
            _ProbeTrack(pub, ns, "probe").attach()
            await pub.publish_namespace(namespace=ns, wait_response=True)
            await SubscribedTrack(sub, ns, "probe",
                                  on_object=_on_object).subscribe(
                                      timeout=timeout)
            await got

    try:
        await asyncio.wait_for(_round_trip(), timeout)
        return True
    except Exception as e:
        logger.warning(f"relay: watchdog probe failed: {e!r}")
        return False


# One self-probe; argv: host, port, use_quic (1/0), draft, timeout.
_PROBE_CHILD = (
    "import asyncio, logging, sys\n"
    "from aiomoqt.tools import moq_interop_relay as r\n"
    "logging.disable(logging.INFO)\n"
    "h, p, q, d, t, f = sys.argv[1:]\n"
    "ok = asyncio.run(r._self_probe(h, int(p), q == '1', int(d), float(t),\n"
    "                               f == '1'))\n"
    "sys.exit(0 if ok else 1)\n"
)


async def _probe_in_child(host, port, use_quic, draft, timeout,
                          full=True) -> bool:
    """Run one self-probe in a child process, so a crash in the probe's
    client transport cannot take the relay down."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _PROBE_CHILD, host, str(port),
        "1" if use_quic else "0", str(draft), str(timeout),
        "1" if full else "0",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout + 15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("relay: watchdog probe timed out")
        return False
    if proc.returncode == 0:
        return True
    lines = (err or b"").decode(errors="replace").strip().splitlines()
    logger.warning(f"relay: watchdog probe failed (exit {proc.returncode})"
                   f"{': ' + lines[-1] if lines else ''}")
    return False


async def _heartbeat(stall_s: float) -> None:
    """Keep re-arming faulthandler's timer. It runs on its own C thread,
    so an event loop stuck past `stall_s` — in Python or in native code —
    gets every thread's stack dumped and the process exited."""
    while True:
        faulthandler.dump_traceback_later(
            stall_s, exit=True, file=_crash_file or sys.stderr)
        await asyncio.sleep(min(1.0, stall_s / 4))


# aiopquic counters that show the RX wake state and worker progress.
TRANSPORT_COUNTERS = (
    "rx_notify_pending_now", "rx_event_ring_count_now",
    "rx_overflow_depth_now", "rx_overflow_max_depth", "rx_overflow_pushed",
    "rx_event_drops", "tx_event_ring_count_now", "loop_callbacks",
    "wake_calls", "wake_skipped_coalesced")


def _transport_of(handle):
    """The aiopquic TransportContext behind a serve() handle, else None."""
    transport = getattr(handle, "_transport", None)
    if transport is None:
        transport = getattr(getattr(handle, "_engine", None), "_transport",
                            None)
    return transport


def transport_counters(handles) -> list:
    """TRANSPORT_COUNTERS of each listener's transport."""
    out = []
    for handle in handles:
        try:
            counters = _transport_of(handle).counters
        except Exception:
            continue
        out.append({k: counters.get(k) for k in TRANSPORT_COUNTERS})
    return out


def _kick_rx(handle) -> bool:
    """Wake asyncio's reader for the transport once, as the network thread
    does. Linux only: elsewhere the wake fd's write end is not exposed."""
    transport = _transport_of(handle)
    if transport is None or not sys.platform.startswith("linux"):
        return False
    try:
        os.write(transport.eventfd, (1).to_bytes(8, sys.byteorder))
    except (OSError, AttributeError):
        return False
    return True


async def _diagnose_probe_miss(handles) -> None:
    """Log the transports' RX wake state, wake each reader once, and log
    the state again: queued events draining after the kick mean a lost
    wake; staying queued means no reader is attached."""
    logger.warning(f"relay: watchdog: probe failed; transport "
                   f"{transport_counters(handles)}")
    kicked = [_kick_rx(h) for h in handles]
    await asyncio.sleep(WATCHDOG_KICK_S)
    logger.warning(f"relay: watchdog: woke readers {kicked}; transport "
                   f"{transport_counters(handles)}")


async def _watchdog(period, host, port, use_quic, draft,
                    handles=()) -> None:
    """Exit so the supervisor restarts the relay when it hangs or stops
    relaying: a loop stall past `period`, or WATCHDOG_MISSES self-probes
    in a row, on the newest draft, that deliver no object. Each failed
    probe logs the transports' RX wake state and wakes their readers."""
    _background.add(asyncio.create_task(_heartbeat(period)))
    newest = max(draft) if isinstance(draft, (list, tuple)) else int(draft)
    misses = 0
    ticks = ok = 0
    report_at = time.monotonic() + WATCHDOG_REPORT_S
    while True:
        await asyncio.sleep(period)
        full = ticks % WATCHDOG_FULL_EVERY == 0
        ticks += 1
        passed = await _probe_in_child(host, port, use_quic, newest,
                                       min(period, WATCHDOG_PROBE_S), full)
        ok += passed
        if time.monotonic() >= report_at:
            logger.info(f"relay: watchdog: {ok}/{ticks} probes ok in the "
                        f"last {WATCHDOG_REPORT_S / 3600:g} h")
            ticks = ok = 0
            report_at = time.monotonic() + WATCHDOG_REPORT_S
        if passed:
            misses = 0
            continue
        misses += 1
        await _diagnose_probe_miss(handles)
        if misses >= WATCHDOG_MISSES:
            logger.error(f"relay: watchdog: {misses} self-probes failed "
                         f"in a row, exiting")
            logging.shutdown()
            os._exit(WATCHDOG_EXIT)


# namespace -> MSF format of the last catalog object relayed for it
_catalogs: dict[tuple, str] = {}


def _internal_ns(ns) -> bool:
    """The watchdog's own namespaces."""
    return bool(ns) and ns[0] == WATCHDOG_NS


def _internal_session(info) -> bool:
    """A session that has used only watchdog namespaces."""
    return bool(info["ns"]) and all(_internal_ns(n) for n in info["ns"])


def _note_catalog(ns, payload) -> None:
    """Record the MSF format of a catalog object seen for `ns`."""
    fmt = relay_status.classify_catalog(payload)
    if fmt is not None:
        _catalogs[ns] = fmt


def _status_namespaces() -> set:
    """Namespaces on the dashboard: announced, or carrying a track."""
    return {ns for ns in list(_announced) + [k[0] for k in list(_tracks)]
            if not _internal_ns(ns)}


def _prune_catalogs() -> None:
    """Forget the formats of namespaces that have gone."""
    names = _status_namespaces()
    for ns in [n for n in _catalogs if n not in names]:
        del _catalogs[ns]


def _path_quality(session) -> dict:
    """picoquic path stats for the session's connection; {} when its
    transport has none."""
    try:
        return session._quic.path_quality() or {}
    except Exception:
        return {}


def _ns_str(ns) -> str:
    return "/".join(p.decode(errors="replace") for p in ns)


def _name_str(name) -> str:
    return (name.decode(errors="replace") if isinstance(name, bytes)
            else str(name))


def _transport(session) -> str:
    return "wt" if getattr(session, "_is_wt", False) else "quic"


def _draft(session):
    return getattr(session, "negotiated_draft", None)


def _track_owner(track):
    return _upstream_session(track) or (
        track.pending_publish[0] if track.pending_publish else None)


def _track_state(track, flowing) -> str:
    if track.finished:
        return "ended"
    if track.pending_publish is not None:
        return "held"
    if track.paused:
        return "parked"  # dashboard.js state name
    return "live" if flowing else "idle"


_HISTORY_KEYS = ("t", "ingress_bps", "egress_bps", "media_in_bps",
                 "media_out_bps", "sessions", "publishers", "namespaces",
                 "subscribers", "subscriptions", "tracks",
                 "loss_pct", "rtt_p50_ms", "lag_p99_ms", "objects_in_per_s",
                 "objects_out_per_s", "groups_per_s", "groups_out_per_s",
                 "drops")
_PLATFORM_KEYS = ("t", "cpu", "cpu_user", "cpu_nice", "cpu_system",
                  "cpu_iowait", "cpu_irq", "cpu_steal", "proc_cpu",
                  "proc_user", "proc_system", "proc_rss", "mem_used",
                  "mem_buffers", "mem_cached", "rx_bps", "tx_bps",
                  "ingress_bps", "egress_bps", "udp_in", "udp_out",
                  "udp_errors", "sock_drops", "sock_rx_queue",
                  "disk_read_Bps", "disk_write_Bps", "psi_cpu", "psi_io",
                  "psi_memory", "ctxt_per_s", "lag_p99_ms")
# Tracks with their own history; a present track takes an ended one's slot.
TRACK_HISTORY_MAX = 24
# Subscribers listed per track.
TRACK_SUBSCRIBERS_MAX = 50


def _cache_efficiency():
    """Share of FETCH payload bytes served from the cache; None before any."""
    cache, upstream = _fetch_bytes["cache"], _fetch_bytes["upstream"]
    total = cache + upstream
    return round(cache / total, 4) if total else None


class _Status:
    """The dashboard's status.json, rewritten every STATUS_INTERVAL_S, and
    platform.json, every PLATFORM_INTERVAL_S."""

    def __init__(self, path: str, relay: dict, handles=()) -> None:
        self.path = path
        # serve() handles whose aiopquic counters platform.json carries.
        self.handles = list(handles)
        self.platform_path = os.path.join(os.path.dirname(path),
                                          "platform.json")
        self.relay = relay
        self.host = relay_status.HostSampler(udp_port=relay.get("port"))
        self.lag = relay_status.LoopLag()
        length = int(STATUS_HISTORY_S / STATUS_INTERVAL_S)
        self.history = relay_status.History(_HISTORY_KEYS, length)
        self.platform_history = relay_status.History(_PLATFORM_KEYS, length)
        # track id -> {series: deque}, aligned with history["t"].
        self.track_history: dict[str, dict[str, deque]] = {}
        self.platform: dict | None = None
        # Most subscribers at once: relay-wide, and per namespace while it lasts.
        self._subs_peak = 0
        self._ns_peak: dict[tuple, int] = {}
        # id(track) -> (bytes in, bytes out, drops) at the last sample.
        self._track_prev: dict[int, tuple] = {}
        # id(session) -> (bytes sent, bytes received, packets sent, lost).
        self._quic_prev: dict[int, tuple] = {}
        self._delivered = 0
        self._drops = 0
        self._last = None

    async def run(self) -> None:
        _background.add(asyncio.create_task(self.lag.run()))
        loop = asyncio.get_running_loop()
        platform_due = 0.0
        while True:
            try:
                now = loop.time()
                _prune_catalogs()
                relay_status.write_json_atomic(
                    self.path, self.snapshot(now, time.time()))
                if now >= platform_due:
                    platform_due = now + PLATFORM_INTERVAL_S
                    relay_status.write_json_atomic(self.platform_path,
                                                   self.platform)
            except Exception:
                logger.warning("relay: status write failed", exc_info=True)
            await asyncio.sleep(STATUS_INTERVAL_S)

    def snapshot(self, now: float, wall: float) -> dict:
        """Relay, totals, namespace, track and session tables, host
        metrics and history; rates are over the time since the last
        snapshot, None on the first."""
        dt = None if self._last is None else now - self._last
        self._last = now

        def bps(delta):
            return round(delta * 8 / dt) if dt else None

        def per_s(delta):
            return round(delta / dt, 1) if dt else None

        live = [i for i in list(_sessions.values())
                if _session_live(i["session"]) and not _internal_session(i)]
        pqs = {id(i["session"]): _path_quality(i["session"]) for i in live}

        def rtt_ms(session):
            pq = pqs.get(id(session))
            rtt = (_path_quality(session) if pq is None else pq).get("rtt")
            return rtt / 1000 if rtt else None

        pubs, subs = set(), set()
        ns_viewers: dict[tuple, set] = {}
        tracks, prev = [], {}
        for key, t in list(_tracks.items()):
            if _internal_ns(key[0]):
                continue
            p_in, p_out, p_drops, p_obj, p_grp = self._track_prev.get(
                id(t), (0, 0, 0, 0, 0))
            prev[id(t)] = (t._bytes_in, t._bytes_out, t._drops, t._objects_in,
                           t._groups_in)
            self._delivered += t._bytes_out - p_out
            self._drops += t._drops - p_drops
            owner = _track_owner(t)
            if owner is not None:
                pubs.add(id(owner))
            subs.update(id(d.session) for d in t.downstream)
            ns_viewers.setdefault(key[0], set()).update(
                id(d.session) for d in t.downstream)
            up = rtt_ms(owner) if owner is not None else None
            down = relay_status.percentile(
                [r for r in (rtt_ms(d.session) for d in t.downstream) if r is not None], 50)
            listed = []
            for d in t.downstream[:TRACK_SUBSCRIBERS_MAX]:
                info = _sessions.get(id(d.session))
                r = rtt_ms(d.session)
                listed.append({
                    "session": info["id"] if info else None,
                    "transport": _transport(d.session), "draft": _draft(d.session),
                    "addr": relay_status.display_addr(_addr_of(d.session)),
                    "rtt_ms": None if r is None else round(r, 1),
                    "forward": bool(d.forward),
                    "lane_bytes": (lane.bytes + lane.replay_bytes
                                   if (lane := d.lane) else 0),
                    "lane_objects": lane.objects if lane else 0,
                    "lane_tx_bytes": lane.tx_bytes if lane else 0,
                    "shed_objects": lane.shed if lane else 0,
                    "shed_episodes": lane.sheds if lane else 0,
                    "lane_stalled_s": (int(lane.stalled_s(time.monotonic()))
                                       if lane else 0),
                    "lane_stalls": lane.stalls if lane else 0,
                    "since": info["since"] if info else None})
            tracks.append((key, {
                "id": "\x1f".join((_ns_str(key[0]), _name_str(key[1]))),
                "namespace": _ns_str(key[0]), "name": _name_str(key[1]),
                "subscriber_list": listed,
                "subscribers": len(t.downstream),
                "forwarding": sum(1 for d in t.downstream if d.forward),
                "peak": t._peak_subs,
                "in_bps": bps(t._bytes_in - p_in),
                "out_bps": bps(t._bytes_out - p_out),
                "bytes_in": t._bytes_in, "bytes_out": t._bytes_out,
                "objects_in": t._objects_in, "objects_out": t._objects_out,
                "fetches": t._fetches, "drops": t._drops,
                "fetch_cache_bytes": t._fetch_bytes["cache"],
                "fetch_upstream_bytes": t._fetch_bytes["upstream"],
                "groups_in": t._groups_in,
                "groups_per_s": per_s(t._groups_in - p_grp),
                "objects_per_s": per_s(t._objects_in - p_obj),
                "rtt_up_ms": None if up is None else round(up, 1),
                "rtt_down_ms": None if down is None else round(down, 1),
                # One-way network estimate: half of each RTT; no queueing or buffering.
                "latency_ms": (None if up is None or down is None
                               else round((up + down) / 2, 1)),
                "largest": list(t.largest) if t.largest else None,
                "cache_groups": len({c[0] for c in t._cache}),
                "cache_bytes": t._cache_bytes,
                "state": _track_state(t, t._bytes_in > p_in),
                "origin": (f"{_transport(owner)} d{_draft(owner)}"
                           if owner is not None else None),
                "origin_addr": (relay_status.display_addr(_addr_of(owner))
                                if owner is not None else None),
                "since": t.created,
            }))
        self._track_prev = prev

        spaces = {}
        for ns in _status_namespaces():
            holders = [s for s in _announced.get(ns, {}) if _session_live(s)]
            pubs.update(id(s) for s in holders)
            spaces[ns] = {
                "namespace": _ns_str(ns), "announced": bool(holders),
                "origin": (f"{_transport(holders[-1])} d{_draft(holders[-1])}"
                           if holders else None),
                "origin_addr": (relay_status.display_addr(_addr_of(holders[-1]))
                                if holders else None),
                "tracks": 0, "subscribers": len(ns_viewers.get(ns, ())),
                "subscriptions": 0, "in_bps": 0, "out_bps": 0,
                "bytes_out": 0, "format": _catalogs.get(ns),
            }
        for key, row in tracks:
            sp = spaces.get(key[0])
            if sp is None:
                continue
            sp["tracks"] += 1
            sp["subscriptions"] += row["subscribers"]
            sp["in_bps"] += row["in_bps"] or 0
            sp["out_bps"] += row["out_bps"] or 0
            sp["bytes_out"] += row["bytes_out"]
            if sp["origin"] is None:
                sp["origin"] = row["origin"]
                sp["origin_addr"] = row["origin_addr"]
        self._ns_peak = {ns: max(self._ns_peak.get(ns, 0), sp["subscribers"])
                         for ns, sp in spaces.items()}
        for ns, sp in spaces.items():
            sp["peak"] = self._ns_peak[ns]

        sessions, quic = [], {}
        held_subs = Counter(id(d.session) for t in _tracks.values()
                            for d in t.downstream)
        held_subs.update(_subscribing)
        tx = rx = sent = lost = 0
        rtts = []
        for info in live:
            s = info["session"]
            pq = pqs[id(s)]
            cur = (pq.get("bytes_sent", 0), pq.get("bytes_received", 0),
                   pq.get("sent", 0), pq.get("lost", 0))
            last = self._quic_prev.get(id(s))
            quic[id(s)] = cur
            d = ([max(0, c - p) for c, p in zip(cur, last)]
                 if last is not None else [0, 0, 0, 0])
            tx, rx = tx + d[0], rx + d[1]
            # Loss counts on delivery only: toward a publisher the relay sends
            # mostly ACK-only packets, which picoquic counts lost unacked.
            delivery = id(s) in subs
            if delivery:
                sent, lost = sent + d[2], lost + d[3]
            rtt = round(pq.get("rtt") / 1000, 2) if pq.get("rtt") else None
            if rtt is not None:
                rtts.append(rtt)
            role = "+".join(r for r, ids in (("pub", pubs), ("sub", subs))
                            if id(s) in ids)
            sessions.append({
                "id": info["id"], "transport": _transport(s), "draft": _draft(s),
                "addr": relay_status.display_addr(_addr_of(s)),
                "role": role or None, "since": info["since"],
                "rtt_ms": round(rtt, 1) if rtt is not None else None,
                "rtt_min_ms": (round(pq["rtt_min"] / 1000, 1)
                               if pq.get("rtt_min") else None),
                "tx_bps": bps(d[0]) if last is not None else None,
                "rx_bps": bps(d[1]) if last is not None else None,
                "loss_pct": (round(100 * d[3] / d[2], 2)
                             if delivery and d[2] else None),
                "namespaces": _session_namespaces(s),
                "subscriptions": held_subs[id(s)],
                "dgram_drops": _dgram_drops.get(id(s), 0),
            })
        self._quic_prev = quic

        media_in = sum(r["in_bps"] or 0 for _, r in tracks)
        media_out = sum(r["out_bps"] or 0 for _, r in tracks)
        lag = self.lag.summary()
        totals = {
            "sessions": len(sessions),
            "by_draft": dict(Counter(f"d{s['draft']}" for s in sessions)),
            "by_transport": dict(Counter(s["transport"] for s in sessions)),
            "publishers": sum(1 for s in sessions
                              if (s["role"] or "").startswith("pub")),
            "namespaces": len(spaces), "tracks": len(tracks),
            # Distinct sessions; a session on several tracks counts once.
            "subscribers": len(subs),
            "subscriptions": sum(r["subscribers"] for _, r in tracks),
            "subscribers_peak": None,
            "ingress_bps": bps(rx), "egress_bps": bps(tx),
            "media_in_bps": media_in, "media_out_bps": media_out,
            "objects_in_per_s": (None if dt is None else round(
                sum(r["objects_per_s"] or 0 for _, r in tracks), 1)),
            "groups_per_s": (None if dt is None else round(
                sum(r["groups_per_s"] or 0 for _, r in tracks), 1)),
            # Delivered rates: each track's input rate times its subscribers.
            "objects_out_per_s": (None if dt is None else round(sum(
                (r["objects_per_s"] or 0) * r["subscribers"] for _, r in tracks), 1)),
            "groups_out_per_s": (None if dt is None else round(sum(
                (r["groups_per_s"] or 0) * r["subscribers"] for _, r in tracks), 1)),
            # Share of egress not received upstream ("CDN efficiency").
            "efficiency": (max(0.0, 1 - media_in / media_out)
                           if media_out else None),
            "delivered_bytes": self._delivered, "drops": self._drops,
            "fetches": sum(r["fetches"] for _, r in tracks),
            "fetch_cache_bytes": _fetch_bytes["cache"],
            "fetch_upstream_bytes": _fetch_bytes["upstream"],
            "cache_efficiency": _cache_efficiency(),
            "cache_budget": CACHE_BUDGET_BYTES,
            "cache_bytes": _budget.bytes,
            "cache_evictions": _budget.evictions,
            "cache_truncations": _budget.truncations,
            "loss_pct": round(100 * lost / sent, 2) if sent else 0.0,
            "rtt_p50_ms": relay_status.percentile(rtts, 50),
            "rtt_p99_ms": relay_status.percentile(rtts, 99),
            "loop_lag_ms": lag,
        }
        self._subs_peak = max(self._subs_peak, totals["subscribers"])
        totals["subscribers_peak"] = self._subs_peak
        host = dict(self.host.static, **self.host.sample(now))
        self.history.add({
            "t": round(wall), "ingress_bps": totals["ingress_bps"],
            "egress_bps": totals["egress_bps"], "media_in_bps": media_in,
            "media_out_bps": media_out, "sessions": totals["sessions"],
            "publishers": totals["publishers"],
            "namespaces": totals["namespaces"],
            "subscribers": totals["subscribers"],
            "subscriptions": totals["subscriptions"], "tracks": len(tracks),
            "loss_pct": totals["loss_pct"], "rtt_p50_ms": totals["rtt_p50_ms"],
            "lag_p99_ms": lag["p99"],
            "objects_in_per_s": totals["objects_in_per_s"],
            "objects_out_per_s": totals["objects_out_per_s"],
            "groups_per_s": totals["groups_per_s"],
            "groups_out_per_s": totals["groups_out_per_s"],
            "drops": totals["drops"],
        })
        self._add_track_history(tracks)
        self.platform = self._platform(wall, host, totals, lag)
        return {
            "generated": wall, "interval_s": STATUS_INTERVAL_S,
            "relay": dict(self.relay,
                          uptime_s=round(wall - self.relay["started"])),
            "totals": totals,
            "namespaces": sorted(spaces.values(),
                                 key=lambda r: r["namespace"]),
            "tracks": sorted((r for _, r in tracks),
                             key=lambda r: (r["namespace"], r["name"])),
            "sessions": sorted(sessions, key=lambda r: r["since"]),
            "host": {k: host.get(k) for k in (
                *self.host.static, "cpu", "mem_used", "mem_total",
                "disk_used", "disk_total", "uptime_s")},
            "history": self.history.to_json(),
            "track_history": {tid: {k: list(v) for k, v in series.items()}
                              for tid, series in self.track_history.items()},
        }

    def _add_track_history(self, tracks) -> None:
        """One point per tracked track, None for one absent this tick; a
        track absent for the whole window is dropped. With every slot
        taken, a present track (subscribed ones first) takes the slot of
        the track absent longest."""
        length = self.history.length
        filled = len(self.history.series["t"])
        now = {row["id"]: row for _, row in tracks}
        for tid in sorted(now, key=lambda t: -(now[t]["subscribers"] or 0)):
            if tid in self.track_history:
                continue
            if len(self.track_history) >= TRACK_HISTORY_MAX:
                gone = self._longest_gone(now)
                if gone is None:
                    continue
                del self.track_history[gone]
            self.track_history[tid] = {
                k: deque([None] * (filled - 1), maxlen=length)
                for k in ("subscribers", "in_bps", "out_bps")}
        for tid, series in list(self.track_history.items()):
            row = now.get(tid)
            for k, col in series.items():
                col.append(None if row is None else row[k])
            if row is None and all(v is None for v in series["subscribers"]):
                del self.track_history[tid]

    def _longest_gone(self, now):
        """The tracked track absent from `now` for the most ticks, or None."""
        def absent(tid):
            col = self.track_history[tid]["subscribers"]
            ticks = 0
            for v in reversed(col):
                if v is not None:
                    break
                ticks += 1
            return ticks
        gone = [tid for tid in self.track_history if tid not in now]
        return max(gone, key=absent, default=None)

    def _platform(self, wall: float, host: dict, totals: dict,
                  lag: dict) -> dict:
        """platform.json: host detail for the platform page, with its own
        history."""
        modes = host.get("cpu_modes") or {}
        udp = host.get("udp") or {}
        sock = host.get("udp_socket") or {}
        mem = host.get("mem") or {}
        psi = host.get("psi") or {}
        proc = host.get("proc") or {}
        disks = host.get("disks") or []
        point = {
            "t": round(wall), "cpu": host.get("cpu"),
            "cpu_user": modes.get("user"), "cpu_nice": modes.get("nice"),
            "cpu_system": modes.get("system"),
            "cpu_iowait": modes.get("iowait"),
            "cpu_irq": (None if not modes else
                        round(modes["irq"] + modes["softirq"], 1)),
            "cpu_steal": modes.get("steal"),
            "proc_cpu": host.get("proc_cpu"),
            "proc_user": proc.get("user_pct"),
            "proc_system": proc.get("system_pct"),
            "proc_rss": host.get("proc_rss"),
            "mem_used": mem.get("used"), "mem_buffers": mem.get("buffers"),
            "mem_cached": mem.get("cached"),
            "rx_bps": host.get("rx_bps"), "tx_bps": host.get("tx_bps"),
            "ingress_bps": totals["ingress_bps"],
            "egress_bps": totals["egress_bps"],
            "udp_in": udp.get("InDatagrams"), "udp_out": udp.get("OutDatagrams"),
            "udp_errors": (None if not udp else round(
                udp.get("InErrors", 0) + udp.get("RcvbufErrors", 0)
                + udp.get("SndbufErrors", 0), 1)),
            "sock_drops": sock.get("drops_per_s"),
            "sock_rx_queue": sock.get("rx_queue"),
            "disk_read_Bps": (sum(d["read_Bps"] for d in disks)
                              if disks else None),
            "disk_write_Bps": (sum(d["write_Bps"] for d in disks)
                               if disks else None),
            "psi_cpu": (psi.get("cpu") or {}).get("some"),
            "psi_io": (psi.get("io") or {}).get("some"),
            "psi_memory": (psi.get("memory") or {}).get("some"),
            "ctxt_per_s": host.get("ctxt_per_s"), "lag_p99_ms": lag["p99"],
        }
        self.platform_history.add(point)
        return {
            "generated": wall, "interval_s": STATUS_INTERVAL_S,
            "relay": {"versions": self.relay.get("versions"),
                      "uptime_s": round(wall - self.relay["started"])},
            "host": host,
            "bandwidth": {"ingress_bps": totals["ingress_bps"],
                          "egress_bps": totals["egress_bps"],
                          "loss_pct": totals["loss_pct"]},
            "loop_lag_ms": lag,
            "transport": transport_counters(self.handles),
            "history": self.platform_history.to_json(),
        }


# --log-dir: relay.log rotation, and the root logger's format.
LOG_FILE_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 4
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# faulthandler's target with --log-dir (crash.log); stderr otherwise.
_crash_file = None


class _LogFile(RotatingFileHandler):
    """relay.log; mode 0644 across rotations for a read-only reader."""

    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o644)
        return stream


class _MirrorFormatter(logging.Formatter):
    """Each record as stderr shows it: aiomoqt loggers have their own
    handler and format, everything else goes through the root's."""

    def __init__(self, aiomoqt_formatter):
        super().__init__(LOG_FORMAT)
        self.aiomoqt = aiomoqt_formatter

    def format(self, record):
        if not logging.getLogger(record.name).propagate:
            return self.aiomoqt.format(record)
        return super().format(record)


class _Tee(logging.Handler):
    """Hands each record to several handlers."""

    def __init__(self, *targets):
        super().__init__()
        self.targets = targets

    def handle(self, record):
        for target in self.targets:
            target.handle(record)
        return True

    def emit(self, record):
        pass


def _versions_line() -> str:
    v = relay_status.versions()
    return f"aiomoqt {v['aiomoqt']} aiopquic {v['aiopquic']}"


def _start_log_dir(log_dir: str) -> None:
    """DIR/relay.log: the stderr log, rotated. DIR/crash.log: a start
    line, then faulthandler's crash and stall dumps (it takes one file,
    so they no longer reach stderr)."""
    global _crash_file
    os.makedirs(log_dir, exist_ok=True)
    os.chmod(log_dir, 0o755)
    shown = logger.handlers[0] if logger.handlers else None
    log_file = _LogFile(os.path.join(log_dir, "relay.log"),
                        maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_BACKUPS)
    log_file.setFormatter(_MirrorFormatter(
        shown.formatter if shown is not None and shown.formatter
        else logging.Formatter(LOG_FORMAT)))
    logging.getLogger().addHandler(log_file)
    set_log_handler(_Tee(shown, log_file) if shown is not None else log_file)

    path = os.path.join(log_dir, "crash.log")
    _crash_file = open(path, "a")
    os.chmod(path, 0o644)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    _crash_file.write(f"relay: start {now.isoformat(timespec='seconds')}Z "
                      f"pid {os.getpid()} {_versions_line()}\n")
    _crash_file.flush()
    faulthandler.enable(file=_crash_file, all_threads=True)
    logger.info(f"relay: crash and stall dumps go to {path}")


def _debug_on() -> bool:
    return (get_log_level() or logging.INFO) <= logging.DEBUG


def _step_log_level() -> None:
    """SIGUSR1: aiomoqt logging INFO -> DEBUG -> TRACE -> INFO."""
    level = step_log_level()
    logger.info(f"relay: log level {logging.getLevelName(level)}")


def _toggle_debug() -> None:
    """POST /debug: switch aiomoqt logging between DEBUG and INFO."""
    if _debug_on():
        set_log_level(logging.INFO)
        logger.info("relay: debug logging off")
    else:
        set_log_level(logging.DEBUG)
        logger.info("relay: debug logging on")


async def _on_control(reader, writer) -> None:
    """One HTTP/1.1 request on the control socket."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
        method, path = (head.split(b"\r\n", 1)[0].decode("latin-1")
                        .split(" ")[:2])
        if path != "/debug" or method not in ("GET", "POST"):
            status, body = "404 Not Found", {"error": "not found"}
        else:
            if method == "POST":
                _toggle_debug()
            status = "200 OK"
            body = {"debug": _debug_on()}
        data = json.dumps(body).encode()
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json"
                     f"\r\nContent-Length: {len(data)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + data)
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            asyncio.TimeoutError, ValueError, ConnectionError):
        pass
    finally:
        writer.close()


async def _serve_control(path: str) -> None:
    """The --control socket; world-writable, for a proxy in another
    container. Its parent directory decides who can reach it."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    server = await asyncio.start_unix_server(_on_control, path=path)
    os.chmod(path, 0o666)
    _background.add(server)
    logger.info(f"relay: control socket {path}")


# Seconds between drain passes.
DRAIN_POLL_S = 0.05


def _setup_done(session) -> bool:
    fut = getattr(session, "_moqt_session_setup", None)
    return (fut is not None and fut.done() and not fut.cancelled()
            and fut.result() is True)


async def _wait_closed(sessions, timeout: float) -> None:
    """Wait up to `timeout` for closed sessions' connections to leave."""
    done = [getattr(s, "_cnx_closed", None) or getattr(s, "_closed", None)
            for s in sessions]
    waits = [asyncio.ensure_future(ev.wait()) for ev in done
             if ev is not None]
    if waits:
        _, pending = await asyncio.wait(waits, timeout=timeout)
        for w in pending:
            w.cancel()


async def _drain(servers, uri: str, timeout: float, why: str) -> None:
    """GOAWAY to every set-up session, refusing any other from now on,
    then wait up to `timeout` seconds for them to leave; close the rest
    with GOAWAY_TIMEOUT."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    for srv in servers:
        srv.refuse_sessions("relay shutting down")

    def live():
        return [(srv, s) for srv in servers for s in list(srv._live)
                if _session_live(s)]
    held = {s for _srv, s in live() if _setup_done(s)}
    logger.info(f"relay: {why}: draining {len(held)} sessions, GOAWAY "
                f"{'to ' + uri if uri else 'to this address'}, up to "
                f"{timeout:g}s")
    for s in held:
        try:
            s.drain()
            s.goaway(uri, timeout=int(timeout * 1000))
        except Exception:
            logger.debug("relay: GOAWAY failed", exc_info=True)
    while True:
        left = []
        for srv, s in live():
            if s in held:
                left.append(s)
            else:
                try:
                    srv.refuse(s)
                except Exception:
                    logger.debug("relay: refusing a session failed",
                                 exc_info=True)
        if not left:
            logger.info("relay: drained, all sessions gone")
            return
        if loop.time() >= deadline:
            logger.info(f"relay: drain timed out, {len(left)} sessions "
                        f"left")
            for s in left:
                s.close(SessionCloseCode.GOAWAY_TIMEOUT, "GOAWAY timeout")
            await _wait_closed(left, 1.0)
            return
        await asyncio.sleep(DRAIN_POLL_S)


async def main():
    global _catalog_probe, _pause_after
    args = parse_args()
    _catalog_probe = args.catalog_probe
    if args.pause_after < 0:
        sys.exit("error: --pause-after must be 0 or above")
    _pause_after = args.pause_after
    speaks = sorted(int(d) for d in MOQTDraft)
    wanted = args.draft if isinstance(args.draft, list) else [args.draft]
    if any(d not in speaks for d in wanted):
        sys.exit(f"error: draft {','.join(map(str, wanted))} not supported; "
                 f"aiomoqt speaks {speaks}")
    log_level = apply_logging(args, default=logging.INFO)
    logging.basicConfig(
        level=log_level, stream=sys.stderr,
        format=LOG_FORMAT,
    )
    if args.log_dir:
        _start_log_dir(args.log_dir)
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1,
                                                  _step_log_level)
    if args.control:
        try:
            await _serve_control(args.control)
        except OSError as e:
            sys.exit(f"error: --control {args.control}: {e.strerror or e}")
    logger.info(f"relay: start {_versions_line()} pid {os.getpid()}")

    cert = args.cert or _find_default_cert()
    key = args.key or _find_default_key(cert)
    if not cert or not key:
        print("Error: TLS cert/key required. Use --cert/--key or place "
              "cert.pem+priv.key at /certs/", file=sys.stderr)
        sys.exit(2)

    if args.dual and (args.quic or args.quic_port is not None):
        print("Error: --dual excludes --quic/--quic-port (one port "
              "serves both transports)", file=sys.stderr)
        sys.exit(2)

    if not 0 < args.max_inflight_bytes < args.stream_ring_cap:
        print("Error: --max-inflight-bytes must be above 0 and below "
              "--stream-ring-cap", file=sys.stderr)
        sys.exit(2)
    if args.cache_bytes <= 0:
        print("Error: --cache-bytes must be above 0", file=sys.stderr)
        sys.exit(2)
    if args.drain_timeout < 0:
        print("Error: --drain-timeout must be 0 or more", file=sys.stderr)
        sys.exit(2)
    global CACHE_BUDGET_BYTES, STREAM_SCHEDULING
    CACHE_BUDGET_BYTES = args.cache_bytes
    if (args.max_namespaces_per_session <= 0
            or args.max_subscriptions_per_session <= 0):
        print("Error: --max-namespaces-per-session and "
              "--max-subscriptions-per-session must be above 0",
              file=sys.stderr)
        sys.exit(2)
    global MAX_NAMESPACES_PER_SESSION, MAX_SUBSCRIPTIONS_PER_SESSION
    MAX_NAMESPACES_PER_SESSION = args.max_namespaces_per_session
    MAX_SUBSCRIPTIONS_PER_SESSION = args.max_subscriptions_per_session
    STREAM_SCHEDULING = args.stream_scheduling
    transport = transport_settings(args)

    # Primary listener: WebTransport unless --quic; both with --dual.
    listeners = [(_build_server(args.bind, args.port, cert, key,
                                args.quic, args.draft, **transport),
                  args.port,
                  "dual raw QUIC + H3/WebTransport" if args.dual
                  else ("raw QUIC" if args.quic else "H3/WebTransport"))]

    # Optional second listener: raw QUIC on --quic-port, sharing the
    # global namespace table. One process then backs both a
    # remote-webtransport and a remote-quic interop endpoint.
    if args.quic_port is not None:
        if args.quic_port == args.port:
            print("Error: --quic-port must differ from --port (two UDP "
                  "binds can't share one port; same-port dual-ALPN would "
                  "need aiopquic support)", file=sys.stderr)
            sys.exit(2)
        listeners.append(
            (_build_server(args.bind, args.quic_port, cert, key,
                           True, args.draft, **transport),
             args.quic_port, "raw QUIC"))

    handles = [await (server.serve_dual() if args.dual else server.serve())
               for server, _port, _label in listeners]

    # Dial origins after the listeners are up: an origin may itself be
    # waiting on us, and a failed dial retries rather than aborting.
    upstream_tasks = [asyncio.create_task(_dial_upstream(url, args.draft))
                      for url in args.upstream]
    for url in args.upstream:
        print(f"  upstream origin: {url}")

    _background.add(asyncio.create_task(_watch_lanes()))

    watchdog = None
    if args.watchdog > 0:
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.bind,
                                                        args.bind)
        # --quic and --dual serve raw QUIC on --port; otherwise WT.
        watchdog = asyncio.create_task(_watchdog(
            args.watchdog, host, args.port, args.quic or args.dual,
            args.draft, handles))
        _background.add(watchdog)

    if args.status_dir:
        os.makedirs(args.status_dir, exist_ok=True)
        status = _Status(os.path.join(args.status_dir, "status.json"), {
            "versions": relay_status.versions(),
            "drafts": wanted,
            "port": args.port,
            "quic_port": args.quic_port,
            "transports": (["quic", "wt"] if args.dual or args.quic_port
                           else ["quic"] if args.quic else ["wt"]),
            "started": time.time(),
            "watchdog_s": args.watchdog,
            "cache": {"groups": CACHE_GROUPS, "bytes": CACHE_BYTES,
                      "budget": CACHE_BUDGET_BYTES},
            "per_session": {"namespaces": MAX_NAMESPACES_PER_SESSION,
                            "subscriptions": MAX_SUBSCRIPTIONS_PER_SESSION},
        }, handles)
        _background.add(asyncio.create_task(status.run()))

    print(
        "=" * 64
        + "\n aiomoqt-relay: a MoQT relay in Python.\n"
        " No authentication or rate limiting.\n"
        + "=" * 64,
        file=sys.stderr,
    )
    for _server, port, label in listeners:
        print(f"Listening on {_listen_desc(args.bind)}, UDP port {port} "
              f"({label}, draft-{args.draft})", file=sys.stderr)
    if args.watchdog > 0:
        print(f"Watchdog: self-probe every {args.watchdog:g}s, exit after "
              f"{WATCHDOG_MISSES} misses or a {args.watchdog:g}s stall",
              file=sys.stderr)

    # SIGTERM or SIGINT drains; a second one exits at once.
    stop = asyncio.Event()
    stopped_by = []
    main_task = asyncio.current_task()

    def _on_stop(sig):
        if stop.is_set():
            logger.info(f"relay: {sig.name} again, exiting now")
            main_task.cancel()
            return
        stopped_by.append(sig.name)
        stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, _on_stop, sig)
    servers = [server for server, _port, _label in listeners]
    wait = 1.0
    try:
        await stop.wait()
        if watchdog is not None:
            watchdog.cancel()
        await _drain(servers, args.goaway_uri, args.drain_timeout,
                     stopped_by[0])
    except asyncio.CancelledError:
        wait = 0
    finally:
        await asyncio.gather(*(server.close_sessions(timeout=wait)
                               for server in servers))
        for h in handles:
            h.close()
        logger.info("relay: exit")


def cli():
    """Console entry point (moq-interop-relay)."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
