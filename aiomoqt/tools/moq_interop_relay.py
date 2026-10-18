#!/usr/bin/env python3
"""ERSATZ-RELAY — a stand-in MoQT relay, not a production relay.

It genuinely relays (moxygen's moqtest conformance suite gates our CI
against it), but it exists for exactly two purposes:

  1. exercise aiomoqt's server-role API surface end to end, and
  2. stand as the SUT for conformance and interop harnesses.

Deliberately absent, and staying absent:

  * NO authentication, authorization, or rate limiting
  * NO load handling, bounded queues, or production hardening
  * A bounded recent-object cache only (CACHE_GROUPS / CACHE_BYTES,
    oldest dropped) — enough to answer a FETCH, standalone or joining,
    over what it still holds; a late subscriber starts at the live edge
  * Upstream Forward State is raised for a forwarding subscriber, never
    lowered again
  * NO delivery-timeout enforcement
  * Namespace tables are in-memory and global to the process

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
      --bind 0.0.0.0 --port 4443 \\
      --cert /certs/cert.pem --key /certs/priv.key

Transports: WebTransport by default, raw QUIC with `--quic`, or BOTH
on one port with `--dual` (per-connection ALPN dispatch via aiopquic
serve_dispatch). `--quic-port N` remains as the legacy two-listener
arrangement (second raw-QUIC listener sharing the global namespace
table) for runners that expect distinct endpoints.
"""

import argparse
import asyncio
from collections import deque
import contextlib
import datetime
import faulthandler
import itertools
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
from collections import Counter

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import (
    MOQT_DEFAULT_DRAFTS, D18MessageType, FilterType, GroupOrder, MOQTDraft,
    MOQTMessageType, MOQTRequestError, ObjectStatus, ParamType,
    RequestErrorCode, StreamResetCode, SubscribeDoneCode,
    parse_draft_spec,
)
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.base import MOQTMessage
from aiomoqt.messages.data import (FetchObject, ObjectDatagram,
                                   ObjectDatagramStatus)
from aiomoqt.messages.fetch import _is_joining
from aiomoqt.messages.publish import PublishError
from aiomoqt.messages.request import RequestError, RequestOk
from aiomoqt.messages.subscribe import Subscribe
from aiomoqt.track import (PublishedTrack, SubscribedTrack, _UNFILTERED,
                           _filter_window, _largest_object_param,
                           _reported_largest)
from aiomoqt.context import (canonical_request_error, canonical_stream_reset,
                             is_draft16_or_later,
                             request_error_code)
from aiomoqt.tools import relay_status
from aiomoqt.utils.buffer import Buffer
from aiomoqt.utils.logger import set_log_handler, set_log_level, get_logger
from aiomoqt.utils.url import parse_relay_url

# Version confinement. The interop runner injects DRAFT (moq-interop-runner
# PR #95) — or older MOQT_DRAFT — to pin the relay to one draft; the client
# is pinned the same way, so negotiation lands on that draft. When neither is
# set (the open-relay context, where clients offer their full version list),
# advertise the library's default drafts, highest first.
_RELAY_DRAFT_DEFAULT = (
    os.environ.get("DRAFT") or os.environ.get("MOQT_DRAFT")
    or ",".join(str(d) for d in MOQT_DEFAULT_DRAFTS)).strip()

logger = get_logger(__name__)


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
# Longest the relay waits for a PUBLISH after an upstream refuses its
# SUBSCRIBE as a duplicate subscription (§5.1: the PUBLISH crossed it).
PUBLISH_WAIT_S = 1.5
# Watchdog: self-probes failed in a row before exiting, the exit status
# (EX_SOFTWARE), and the longest one probe may take.
WATCHDOG_MISSES = 3
WATCHDOG_EXIT = 70
WATCHDOG_PROBE_S = 10.0
# After a failed probe, how long to let a woken reader drain before
# reading the transport counters again.
WATCHDOG_KICK_S = 0.5
# Dashboard (--status-dir): write period, history kept, the longest a
# catalog probe may take, and the wait before re-probing a namespace
# that showed no MSF catalog.
STATUS_INTERVAL_S = 2.0
STATUS_HISTORY_S = 600
CATALOG_PROBE_S = 5.0
CATALOG_RETRY_S = 30.0
# MSF catalog track name (§5.2.3), and the watchdog's own namespace,
# kept off the dashboard.
CATALOG_TRACK = b"catalog"
WATCHDOG_NS = b"aiomoqt-watchdog"


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


# Recent-object cache bounds, per track: whichever is hit first.
CACHE_GROUPS = 8
CACHE_BYTES = 32 * 1024 * 1024


class _Downstream:
    """One downstream subscription to a relayed track."""

    __slots__ = ("session", "alias", "request_id", "forward", "window",
                 "joined_at", "replaying")

    def __init__(self, session, alias, request_id, forward=True,
                 window=_UNFILTERED):
        self.session = session
        self.alias = alias
        self.request_id = request_id
        # Forward State (§5.1): objects flow only while True.
        self.forward = forward
        # Filter window (§5.1.2): (start Location, End Group or None).
        self.window = window
        # Joining Location (§5.1): Largest when Forward State became 1.
        self.joined_at = None
        # Waiting for the cache replay queued for it; live objects skip it
        # until then and reach it through the replay.
        self.replaying = False

    def passes(self, group_id, object_id) -> bool:
        """Forward State 1 and inside the filter window."""
        start, end_group = self.window
        return (self.forward and (group_id, object_id) >= start
                and (end_group is None or group_id <= end_group))

    def past_end(self, group_id) -> bool:
        end_group = self.window[1]
        return end_group is not None and group_id > end_group


class _RelayedTrack:
    """One upstream subscription fanned out to N downstream subscribers.

    Transport §"Relays": a Full Track Name is matched exactly against
    existing upstream subscriptions, so a second downstream subscriber
    for the same track joins this fan-out instead of opening a second
    upstream SUBSCRIBE.

    Objects arrive on a synchronous callback but forwarding has to open
    streams, which is async — inbound objects go through a queue drained
    by one task per track, which also keeps them in arrival order.
    """

    def __init__(self, key):
        self.key = key
        self.upstream = None
        # Flow B: the publisher's PUBLISH, held until someone subscribes.
        self.pending_publish = None   # (session, Publish msg)
        # Answered with Forward State 0 because nobody could be offered
        # the track yet; raised with REQUEST_UPDATE when one arrives.
        self.parked = None            # (session, Publish msg)
        self.downstream: list[_Downstream] = []
        self.queue = asyncio.Queue()
        self.task = None
        # A downstream SUBSCRIBE is establishing this track: an upstream
        # terminal that lands first waits in the queue for it.
        self.attaching = False
        # (id(session), group, subgroup) -> (stream_id, SubgroupHeader)
        self._streams = {}
        # id(session) -> subgroup streams opened (PUBLISH_DONE count)
        self._sent_streams = {}
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
        self.downstream.clear()
        self._streams.clear()

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
            self._end(d, status_code, reset_code, reason)

    def end_downstream(self, d) -> None:
        """End one subscription whose End Group is behind (§5.1.2)."""
        if d in self.downstream:
            self.downstream.remove(d)
            self._end(d, SubscribeDoneCode.SUBSCRIPTION_ENDED, None,
                      "subscription ended")

    def _end(self, d, status_code, reset_code, reason) -> None:
        """FIN (or reset) a subscriber's open streams, then PUBLISH_DONE."""
        session = d.session
        try:
            for k, (sid, _hdr) in list(self._streams.items()):
                if k[0] == id(session):
                    if reset_code is None:
                        session.stream_write(sid, b"", end_stream=True)
                    else:
                        session.stream_reset(sid, int(reset_code))
                    self._streams.pop(k, None)
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
        self.downstream = [d for d in self.downstream
                           if d.session is not session]
        for k in [k for k in self._streams if k[0] == id(session)]:
            self._streams.pop(k, None)
        self._sent_streams.pop(id(session), None)

    async def _forward_loop(self):
        while True:
            gid, sgid, oid, payload, exts, prio, status, shape = \
                await self.queue.get()
            if status == "DONE":
                # Terminal, in queue order: everything already queued has
                # been written downstream before PUBLISH_DONE goes out.
                code, reason, key = shape
                self.finish(status_code=code, reason=reason)
                self.release_upstream()
                self.downstream.clear()
                self._streams.clear()
                self.task = None
                _tracks.pop(key, None)
                return
            if self._finished:
                continue
            if status == "REPLAY":
                await self._replay(shape)
                continue
            if oid is not None:
                self._remember(gid, sgid, oid, payload, exts, prio, status,
                               shape)
            encoded = {}
            for d in list(self.downstream):
                if d.replaying:
                    continue
                if not _session_live(d.session):
                    self.drop_session(d.session)
                    continue
                if oid is not None:
                    if d.past_end(gid):
                        self.end_downstream(d)
                        continue
                    if not d.passes(gid, oid):
                        continue
                try:
                    self._objects_out += 1
                    self._bytes_out += len(payload or b"")
                    await self._forward_one(
                        d.session, d.alias, gid, sgid, oid, payload, exts,
                        prio, status, shape, encoded)
                except (Exception, asyncio.CancelledError):
                    if _cancelling():
                        raise
                    logger.debug("relay: forward failed, dropping subscriber",
                                 exc_info=True)
                    self._drops += 1
                    self.drop_session(d.session)
                    continue
                if (oid is not None and d.window[1] == gid and status in (
                        ObjectStatus.END_OF_GROUP, ObjectStatus.END_OF_TRACK)):
                    self.end_downstream(d)

    def _remember(self, gid, sgid, oid, payload, exts, prio, status,
                  shape=None) -> None:
        """Keep one object, with its stream shape or datagram mode, for a
        later FETCH or replay, dropping oldest past the bound."""
        self._cache.append((gid, sgid, oid, payload, exts, prio, status,
                            shape))
        self._cache_bytes += len(payload or b"")
        self.note_largest((gid, oid))
        oldest_kept = gid - CACHE_GROUPS + 1
        while self._cache and (self._cache[0][0] < oldest_kept
                               or self._cache_bytes > CACHE_BYTES):
            dropped = self._cache.popleft()
            self._cache_bytes -= len(dropped[3] or b"")
        if self._cache:
            first = self._cache[0][0]
            self._ended = {k for k in self._ended if k[0] >= first}
            self._opened = {k for k in self._opened if k[0] >= first}

    async def _replay(self, d) -> None:
        """The cached objects inside `d`'s window, in Location order,
        then FIN for the subgroups the publisher has already ended."""
        d.replaying = False
        if d not in self.downstream:
            return
        objs = sorted((c for c in self._cache if d.passes(c[0], c[2])),
                      key=lambda c: (c[0], c[1], c[2]))
        try:
            for gid, sgid, oid, payload, exts, prio, status, shape in objs:
                self._objects_out += 1
                self._bytes_out += len(payload or b"")
                await self._forward_one(d.session, d.alias, gid, sgid, oid,
                                        payload, exts, prio, status, shape)
            for gid, sgid in sorted({(c[0], c[1]) for c in objs}):
                if (gid, sgid) in self._ended:
                    await self._forward_one(d.session, d.alias, gid, sgid,
                                            None, None, None, None, "END")
        except (Exception, asyncio.CancelledError):
            if _cancelling():
                raise
            logger.debug("relay: replay failed, dropping subscriber",
                         exc_info=True)
            self._drops += 1
            self.drop_session(d.session)
            return
        logger.info(f"relay: replayed {len(objs)} cached object(s) of "
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

    def cached_range(self, start, end):
        """Cached objects within [start, end], each (group, object)
        inclusive; `end` None means to the live edge. Ascending."""
        out = []
        for gid, sgid, oid, payload, exts, prio, status, _ in self._cache:
            if (gid, oid) < start:
                continue
            if end is not None and (gid, oid) > end:
                continue
            out.append((gid, sgid, oid, payload, exts, prio, status))
        return out

    async def _forward_one(self, session, alias, gid, sgid, oid,
                           payload, exts, prio, status=None, shape=None,
                           encoded=None):
        """Write one object downstream, opening the (group, subgroup)
        stream on first sight. Group/subgroup identity is preserved from
        upstream so the downstream sees the publisher's structure."""
        if (isinstance(shape, tuple) and shape and shape[0] == "DGRAM"
                and not _no_datagrams.get(id(session))):
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
            try:
                session.send_dgram_message(dgram.serialize(prof=prof))
                return
            except NotImplementedError:
                # WebTransport datagram TX is not wired in aiopquic yet;
                # deliver on a subgroup stream rather than drop objects.
                _no_datagrams[id(session)] = True
                logger.warning("relay: no datagram TX on this session, "
                               "forwarding objects on streams")
        skey = (id(session), gid, sgid)
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
            stream_id = await session.open_uni_stream()
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
            session.stream_write(stream_id, header.serialize().data)
            entry = (stream_id, header)
            self._streams[skey] = entry
            self._sent_streams[id(session)] = \
                self._sent_streams.get(id(session), 0) + 1
        stream_id, header = entry
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
        await session.stream_write_drain(stream_id, data)


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
    _ns_subs[:] = [e for e in _ns_subs if e[0] is not session]
    _no_datagrams.pop(id(session), None)
    for k in [k for k in _announce_requests if k[0] == id(session)]:
        del _announce_requests[k]
    for ns in list(_announced):
        if _announced[ns].pop(session, None) is not None and \
                not _announced[ns]:
            del _announced[ns]
            # This session's own prefix subscriptions were dropped just
            # above, so this reaches the observers that remain.
            _retract_namespace(ns)
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


async def _on_publish_namespace(session, msg):
    """Record the announcing session, ack with default OK path."""
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
    # Reuse the protocol's built-in OK helper. It emits RequestOk on
    # d16+ and PublishNamespaceOk on d14, matching peer expectation.
    session.publish_namepace_ok(msg)


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
    """The first subscriber's start for a shared upstream SUBSCRIBE, with
    no End Group: later subscribers may want more. An unfiltered one stays
    unfiltered (filter_type None; see _filter_for)."""
    ftype = getattr(sub, "filter_type", None)
    if sub is None or ftype in (FilterType.LATEST_OBJECT,
                                FilterType.NEXT_GROUP_START):
        return {"filter_type": ftype or FilterType.LATEST_OBJECT}
    if ftype in (FilterType.ABSOLUTE_START, FilterType.ABSOLUTE_RANGE):
        return {"filter_type": FilterType.ABSOLUTE_START,
                "start_group": sub.start_group or 0,
                "start_object": sub.start_object or 0}
    return {"filter_type": None}


def _filter_for(pub, kwargs: dict) -> dict:
    """From d16 an omitted filter is unfiltered (§10.2.9); d14 has no
    omission, so it carries AbsoluteStart {0, 0}."""
    if "filter_type" in kwargs and kwargs["filter_type"] is None and not (
            is_draft16_or_later(pub.negotiated_draft)):
        return {"filter_type": FilterType.ABSOLUTE_START,
                "start_group": 0, "start_object": 0}
    return kwargs


def _parked_publish(key):
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
    if rid is None:
        return
    try:
        session.unsubscribe(rid)
    except Exception:
        logger.debug("relay: cancelling the upstream SUBSCRIBE failed",
                     exc_info=True)


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
        try:
            await upstream.subscribe(timeout=10.0,
                                     **_filter_for(pub, entry.filter))
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
        parked = _parked_publish(key)
        if parked is not None:
            # A PUBLISH from another session landed meanwhile and owns
            # the track: never overwrite it.
            _cancel_upstream_subscribe(pub, ns, track_name)
            if upstream.track_alias is not None:
                pub.unregister_object_handler(upstream.track_alias)
            return parked, None
        track.upstream = upstream
        track.upstream_alias = upstream.track_alias
        # §10.2.11: Largest includes what the upstream SUBSCRIBE_OK reports.
        track.note_largest(upstream.largest)
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
    # §9.4: a bare PUBLISH is the only notice d18 prefix subscribers get
    # that this namespace exists.
    _announce_namespace(ns)
    logger.info(f"relay: publish ns={ns} track={msg.track_name} "
                f"alias={msg.track_alias}")
    offered = False
    for sub_session, prefix, _rid in list(_track_subs):
        if _prefix_covers(prefix, ns):
            offered = True
            asyncio.create_task(
                _offer_track(sub_session, track, key))
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
        forward = _forwarding(track)
    session._track_aliases[msg.track_alias] = msg.request_id
    session.register_object_handler(msg.track_alias, track.on_object)
    session.register_stream_end_handler(msg.track_alias, track.on_stream_end)
    track.publish_alias = msg.track_alias
    if getattr(msg, "content_exists", 0):
        track.note_largest((msg.largest_group_id, msg.largest_object_id))
    # From d20 PUBLISH_OK carries only EXPIRES and Forward State follows
    # in a REQUEST_UPDATE; publish_ok() sends the draft's shape.
    session.publish_ok(msg, forward=int(forward), priority=128,
                       group_order=GroupOrder.ASCENDING,
                       filter_type=FilterType.LATEST_OBJECT)
    logger.info(f"relay: PUBLISH_OK forward={int(forward)} "
                f"alias={msg.track_alias}"
                + ("" if forward else " — parked until a subscriber forwards"))
    track.upstream = session
    track.upstream_request_id = msg.request_id
    track.parked = None if forward else (session, msg)
    track.pending_publish = None


def _refuse_publish(session, msg, reason) -> None:
    """DUPLICATE_SUBSCRIPTION for a PUBLISH (NOT_SUPPORTED from d20,
    PUBLISH_ERROR at d14)."""
    prof = session._profile
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


def _resume_parked_publish(track) -> None:
    """Raise a parked publisher's Forward State once a subscriber
    forwards."""
    if track.parked is None or not _forwarding(track):
        return
    session, msg = track.parked
    track.parked = None
    try:
        session.request_update(msg.request_id, forward=1,
                               **_publish_ok_location(msg))
        logger.info(f"relay: REQUEST_UPDATE forward=1 "
                    f"alias={msg.track_alias}")
    except Exception:
        logger.debug("relay: raising the parked forward state failed",
                     exc_info=True)


def _largest_kwargs(track) -> dict:
    """SUBSCRIBE_OK Largest Location (§10.2.11); none when nothing has
    been published."""
    if track.largest is None:
        return {}
    return {"content_exists": 1,
            "largest_group_id": track.largest[0],
            "largest_object_id": track.largest[1]}


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


def _attach_subscriber(session, msg, track) -> bool:
    """SUBSCRIBE_OK and join the fan-out with the SUBSCRIBE's Forward
    State and filter window, against Largest now (§5.1.2). False when
    the End Group is already behind, answered INVALID_RANGE."""
    window = _filter_window(msg.filter_type, msg.start_group,
                            msg.start_object, msg.end_group, track.largest)
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
    ok = session.subscribe_ok(request_msg=msg, **_largest_kwargs(track))
    # FORWARD omitted means 1 (§10.2.12).
    forward = msg.forward is None or bool(msg.forward)
    # An explicit Start at or before Largest is served from the cache
    # first (§5.1.2: the Start MAY be less than the Largest Object).
    replay = (msg.filter_type in (FilterType.ABSOLUTE_START,
                                  FilterType.ABSOLUTE_RANGE)
              and track.largest is not None and window[0] <= track.largest)
    track.add_downstream(session, ok.track_alias, msg.request_id,
                         forward=forward, window=window, replay=replay)
    session.register_request_cancel_handler(
        msg.request_id, lambda rid, t=track, s=session: t.drop_session(s))
    return True


async def _on_request_update(session, msg):
    """A subscriber changes its Forward State or filter: REQUEST_UPDATE
    from d16 (§10.9), SUBSCRIBE_UPDATE at d14 (Forward State only).
    Updates to anything but a relayed subscription get the session's
    default reply."""
    d16 = is_draft16_or_later(session.negotiated_draft)
    rid = (msg.existing_request_id if d16
           else msg.subscription_request_id)
    found = _joined_subscription(session, rid)
    if found is None:
        if d16:
            await session._handle_request_update(msg)
        else:
            await session._handle_subscribe_update(msg)
        return
    key, track, d = found
    if d16:
        forward = (msg.parameters or {}).get(ParamType.FORWARD)
        ftype = _reply_filter(session, msg)
        if ftype[0] is not None:
            d.window = _filter_window(*ftype, track.largest)
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
        session._send_reply(msg.request_id,
                            RequestOk(request_id=msg.request_id,
                                      parameters=params))
    _resume_parked_publish(track)


async def _on_subscribe(session, msg):
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
    if track is not None and (track.pending_publish or track.upstream):
        if _attach_subscriber(session, msg, track):
            _answer_publish(track)
            _resume_parked_publish(track)
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
            if _attach_subscriber(session, msg, track):
                _answer_publish(track)
                _resume_parked_publish(track)
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


def parse_args():
    parser = argparse.ArgumentParser(
        description="MoQT interop-runner relay (control-plane only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--bind", type=str, default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
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
    # cubic: about 65% more fan-out than bbr on one worker, whose sends
    # are syscall-bound; bbr's per-peer pacing defeats send batching.
    parser.add_argument("--cc-algo", type=str, default="cubic",
                        help="Congestion control (cubic | bbr | bbr1 | c4 | "
                             "newreno | dcubic | prague | fast). Default: "
                             "cubic")
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
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    return parser.parse_args()


# SUBSCRIBE_TRACKS subscribers: (session, prefix_tuple, request_id).
_track_subs: list = []
# (session, prefix, request_id) for d18 SUBSCRIBE_NAMESPACE subscribers.
_ns_subs: list = []


def _prefix_covers(prefix, ns) -> bool:
    """§2.4 field-wise prefix match (empty prefix matches everything)."""
    return ns[:len(prefix)] == prefix


async def _offer_track(session, track, key):
    """§9.5: send a PUBLISH for a held/served track to a
    SUBSCRIBE_TRACKS subscriber; on PUBLISH_OK wire it into the fan-out
    with the Forward State and filter it chose."""
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
        forward=1, largest=track.largest)
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
    window = _filter_window(*_reply_filter(session, reply), track.largest)
    # Cached objects inside the subscriber's window go first.
    track.add_downstream(session, pub_msg.track_alias, pub_msg.request_id,
                         forward=bool(forward), window=window,
                         replay=track.largest is not None)
    session.register_request_cancel_handler(
        pub_msg.request_id,
        lambda rid, t=track, s=session: t.drop_session(s))
    _answer_publish(track)
    _resume_parked_publish(track)
    logger.info(f"relay: PUBLISH offer accepted for {key} "
                f"alias={pub_msg.track_alias} forward={int(bool(forward))} "
                f"(fanout={len(track.downstream)})")


def _reply_filter(session, reply) -> tuple:
    """(type, start group, start object, end group) a PUBLISH_OK or
    REQUEST_UPDATE carries: fields at d14, a parameter from d16."""
    if getattr(reply, "filter_type", None) is not None:
        return (reply.filter_type, reply.start_group, reply.start_object,
                reply.end_group)
    raw = (getattr(reply, "parameters", None) or {}).get(
        ParamType.SUBSCRIPTION_FILTER)
    if raw is None:
        return (None, None, None, None)
    return MOQTMessage._decode_filter(raw, prof=session._profile)


# id(session) -> True once a datagram send proved unsupported there.
_no_datagrams: dict = {}

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


async def _fetch_upstream(ns, track_name, msg):
    """Ask each publisher of `ns` for the requested range. Returns its
    FETCH_OK, the objects and None, or (None, None, the last upstream
    refusal or None) when nobody serves it —
    the relay keeps only recent objects, so a historical range has to
    come from the origin."""
    name = (track_name.decode() if isinstance(track_name, bytes)
            else track_name)
    refusal = None
    for pub in _publishers_for(ns):
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
            ok = await pub._await_response(req.request_id, timeout=10.0)
            await pub.await_fetch_done(req.request_id, timeout=10.0)
        except (MOQTRequestError, asyncio.TimeoutError) as e:
            logger.info(f"relay: upstream FETCH declined on {ns}: {e}")
            _fetch_sinks.pop(key, None)
            if isinstance(e, MOQTRequestError):
                refusal = _upstream_refusal(e, pub._profile, fetch=True)
            continue
        objs = _fetch_sinks.pop(key, [])
        logger.info(f"relay: upstream FETCH {ns}/{name} -> "
                    f"{len(objs)} object(s) end_of_track="
                    f"{getattr(ok, 'end_of_track', 0)}")
        return ok, objs, None
    return None, None, refusal


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
        msg.start_group, msg.start_object = start_group, 0
        msg.end_group, msg.end_object = anchor[0], anchor[1] + 1
        ns, track_name = key
        logger.info(f"relay: joining FETCH {key} groups {start_group}.."
                    f"{anchor[0]} (anchor {anchor})")
    track = _tracks.get((ns, track_name))
    start = (msg.start_group or 0, msg.start_object or 0)
    end = (None if msg.end_group is None
           else (msg.end_group, msg.end_object
                 if msg.end_object is not None else (1 << 62)))
    have_cache = (track is not None and _track_live(track)
                  and track.cache_covers(start))
    objs = track.cached_range(start, end) if have_cache else []
    source = "cache"
    end_of_track = 0
    if objs and objs[-1][6] == ObjectStatus.END_OF_TRACK:
        end_of_track = 1
    if not objs:
        # Only recent objects are held, so a historical range comes from
        # the publisher — which is also the only path for a track this
        # relay has never subscribed to.
        up_ok, upstream, refusal = await _fetch_upstream(ns, track_name, msg)
        if upstream is None and refusal is not None:
            code, reason = refusal
            logger.info(f"relay: FETCH ns={ns} track={track_name} "
                        f"-> {code.name} (upstream: {reason})")
            session.fetch_error(request_id=msg.request_id, error_code=code,
                                reason=reason)
            return
        if upstream is None:
            logger.info(f"relay: FETCH ns={ns} track={track_name} "
                        f"-> DOES_NOT_EXIST")
            session.fetch_error(
                request_id=msg.request_id,
                error_code=RequestErrorCode.DOES_NOT_EXIST,
                reason="track does not exist")
            return
        if not upstream:
            session.fetch_error(
                request_id=msg.request_id,
                error_code=RequestErrorCode.INVALID_RANGE,
                reason="requested range unavailable")
            return
        objs = [(o.group_id, o.subgroup_id, o.object_id, o.payload,
                 o.extensions, o.publisher_priority, o.status)
                for o in upstream]
        end_of_track = int(getattr(up_ok, 'end_of_track', 0) or 0)
        source = "upstream"
    if track is not None:
        track._fetches += 1
    if track_name == CATALOG_TRACK:
        for o in objs:
            if o[2] == 0:
                _note_catalog(ns, o[3])
    order = msg.group_order or GroupOrder.ASCENDING
    last_g, last_o = objs[-1][0], objs[-1][2]
    # §10.13: FETCH_OK End Location is the last Object PLUS 1, and End Of
    # Track says the range reached the track's end — which only the
    # publisher knows, so it is carried over from its FETCH_OK.
    session.fetch_ok(request_id=msg.request_id,
                     end_of_track=end_of_track,
                     largest_group_id=last_g, largest_object_id=last_o + 1,
                     group_order=int(order))
    if int(order) == int(GroupOrder.DESCENDING):
        objs = sorted(objs, key=lambda o: (-o[0], o[2]))
    await session.serve_fetch(
        msg.request_id, _counted_fetch(objs, track, source),
        group_order=int(order))
    logger.info(f"relay: FETCH ns={ns} track={track_name} served "
                f"{len(objs)} object(s) {start}..({last_g}.{last_o})")


async def _on_subscribe_namespace(session, msg):
    """Namespace discovery (§9.4). d18 answers NAMESPACE per namespace
    under the prefix — the subscriber then asks each one for its tracks
    with SUBSCRIBE_TRACKS. d14/d16 have no NAMESPACE message: the same
    prefix subscription is answered with a PUBLISH per matching track,
    present and future, which is what those drafts expect."""
    prefix = _ns_tuple(msg.namespace_prefix)
    _watch_session(session, prefix)
    session.subscribe_namespace_ok(
        msg, stream_id=session._bidi_streams.get(msg.request_id))

    def _drop(rid, s=session, r=msg.request_id):
        _ns_subs[:] = [e for e in _ns_subs
                       if not (e[0] is s and e[2] == r)]
        _track_subs[:] = [e for e in _track_subs
                          if not (e[0] is s and e[2] == r)]
    session.register_request_cancel_handler(msg.request_id, _drop)

    if session._profile.two_level_discovery:
        _ns_subs.append((session, prefix, msg.request_id))
        for ns in _known_namespaces(prefix):
            _offer_namespace(session, msg.request_id, prefix, ns)
        logger.info(f"relay: subscribe-namespace prefix={prefix} "
                    f"(d18 discovery, subs={len(_ns_subs)})")
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
    """Tell d18 prefix subscribers about a namespace that just appeared."""
    for session, prefix, rid in list(_ns_subs):
        if _prefix_covers(prefix, ns) and _session_live(session):
            _offer_namespace(session, rid, prefix, ns)


def _retract_namespace(ns) -> None:
    """Tell d18 prefix subscribers a namespace has gone (§10.8).

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


async def _on_subscribe_tracks(session, msg):
    """§9.5: a SUBSCRIBE_TRACKS subscriber gets a PUBLISH for every
    matching track, present and future."""
    prefix = _ns_tuple(msg.namespace_prefix)
    _watch_session(session, prefix)
    session.subscribe_tracks_ok(msg)

    def _drop(rid, s=session, r=msg.request_id):
        _track_subs[:] = [e for e in _track_subs
                          if not (e[0] is s and e[2] == r)]
    _track_subs.append((session, prefix, msg.request_id))
    session.register_request_cancel_handler(msg.request_id, _drop)
    logger.info(f"relay: subscribe-tracks prefix={prefix} "
                f"(subs={len(_track_subs)})")
    for key, track in list(_tracks.items()):
        if _prefix_covers(prefix, key[0]) and (
                track.pending_publish or track.upstream):
            asyncio.create_task(_offer_track(session, track, key))


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
    ok = RequestOk(request_id=msg.request_id, parameters=params)
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
        **transport,
    )
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


async def _self_probe(host, port, use_quic, draft, timeout) -> bool:
    """Relay one object from a publisher session to a subscriber session,
    both on loopback, under a namespace of the probe's own."""
    ns = f"aiomoqt-watchdog/{os.getpid()}-{next(_probe_seq)}"
    got = asyncio.get_running_loop().create_future()

    def _on_object(*_):
        if not got.done():
            got.set_result(True)

    async def _round_trip():
        async with contextlib.AsyncExitStack() as stack:
            sessions = []
            for _ in range(2):
                client = MOQTClient(host, port, path="/", use_quic=use_quic,
                                    verify_tls=False, supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                sessions.append(s)
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
    "h, p, q, d, t = sys.argv[1:]\n"
    "ok = asyncio.run(r._self_probe(h, int(p), q == '1', int(d), float(t)))\n"
    "sys.exit(0 if ok else 1)\n"
)


async def _probe_in_child(host, port, use_quic, draft, timeout) -> bool:
    """Run one self-probe in a child process, so a crash in the probe's
    client transport cannot take the relay down."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _PROBE_CHILD, host, str(port),
        "1" if use_quic else "0", str(draft), str(timeout),
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
    while True:
        await asyncio.sleep(period)
        if await _probe_in_child(host, port, use_quic, newest,
                                 min(period, WATCHDOG_PROBE_S)):
            misses = 0
            continue
        misses += 1
        await _diagnose_probe_miss(handles)
        if misses >= WATCHDOG_MISSES:
            logger.error(f"relay: watchdog: {misses} self-probes failed "
                         f"in a row, exiting")
            logging.shutdown()
            os._exit(WATCHDOG_EXIT)


# namespace -> [MSF format or None, loop time of the last probe, probe task]
_catalogs: dict[tuple, list] = {}


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
        _catalogs.setdefault(ns, [None, None, None])[0] = fmt


def _status_largest(reply):
    """Largest a TRACK_STATUS reply reports: fields at d14, LARGEST_OBJECT
    from d16 (a varint pair at d16, a Location from d18)."""
    largest = _reported_largest(reply)
    if largest is not None:
        return largest
    raw = (getattr(reply, "parameters", None) or {}).get(
        ParamType.LARGEST_OBJECT)
    if isinstance(raw, (tuple, list)):
        return tuple(raw)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        buf = Buffer(data=bytes(raw))
        return (buf.pull_uint_var(), buf.pull_uint_var())
    return None


def _catalog_sources(ns) -> list:
    """Sessions that can answer for `ns`'s catalog: the one that
    PUBLISHed it, then the namespace's publishers."""
    out = []
    track = _tracks.get((ns, CATALOG_TRACK))
    if track is not None:
        owner = _upstream_session(track) or (
            track.pending_publish[0] if track.pending_publish else None)
        if _session_live(owner):
            out.append(owner)
    return out + [p for p in _publishers_for(ns) if p not in out]


async def _probe_catalog(ns) -> None:
    """Ask each source for its newest catalog group, TRACK_STATUS for the
    Largest then a FETCH of that group, until one yields an MSF format."""
    for pub in _catalog_sources(ns):
        got = []
        try:
            reply = await asyncio.wait_for(
                pub.track_status(ns, CATALOG_TRACK, wait_response=True),
                CATALOG_PROBE_S)
            largest = _status_largest(reply)
            if largest is None:
                continue
            ok = await asyncio.wait_for(
                pub.fetch(namespace=ns, track_name=CATALOG_TRACK,
                          start_group=largest[0], end_group=largest[0],
                          wait_response=True,
                          on_object=lambda obj, *_: got.append(obj)),
                CATALOG_PROBE_S)
            await pub.await_fetch_done(ok.request_id, timeout=CATALOG_PROBE_S)
        except Exception as e:
            logger.debug(f"relay: catalog probe of {ns} failed: {e!r}")
            continue
        for obj in got:
            if obj.object_id == 0:
                _note_catalog(ns, obj.payload)
        if _catalogs.get(ns, (None,))[0] is not None:
            return


def _status_namespaces() -> set:
    """Namespaces on the dashboard: announced, or carrying a track."""
    return {ns for ns in list(_announced) + [k[0] for k in list(_tracks)]
            if not _internal_ns(ns)}


def _probe_catalogs(now) -> None:
    """Probe each namespace with no MSF format yet, at most once per
    CATALOG_RETRY_S, and forget namespaces that have gone."""
    names = _status_namespaces()
    for ns in [n for n in _catalogs if n not in names]:
        task = _catalogs[ns][2]
        if task is None or task.done():
            del _catalogs[ns]
    for ns in names:
        entry = _catalogs.setdefault(ns, [None, None, None])
        fmt, last, task = entry
        if (fmt is not None or (task is not None and not task.done())
                or (last is not None and now - last < CATALOG_RETRY_S)):
            continue
        entry[1] = now
        entry[2] = asyncio.create_task(_probe_catalog(ns))


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
    if track.parked is not None:
        return "parked"
    return "live" if flowing else "idle"


_HISTORY_KEYS = ("t", "ingress_bps", "egress_bps", "media_in_bps",
                 "media_out_bps", "sessions", "subscribers", "subscriptions",
                 "tracks",
                 "loss_pct", "rtt_p50_ms", "lag_p99_ms", "objects_in_per_s",
                 "groups_per_s")
_PLATFORM_KEYS = ("t", "cpu", "cpu_user", "cpu_nice", "cpu_system",
                  "cpu_iowait", "cpu_irq", "cpu_steal", "proc_cpu",
                  "proc_user", "proc_system", "proc_rss", "mem_used",
                  "mem_buffers", "mem_cached", "rx_bps", "tx_bps",
                  "ingress_bps", "egress_bps", "udp_in", "udp_out",
                  "udp_errors", "sock_drops", "sock_rx_queue",
                  "disk_read_Bps", "disk_write_Bps", "psi_cpu", "psi_io",
                  "psi_memory", "ctxt_per_s", "lag_p99_ms")
# Tracks with their own history; later ones go without until one ends.
TRACK_HISTORY_MAX = 24
# Subscribers listed per track.
TRACK_SUBSCRIBERS_MAX = 50


def _cache_efficiency():
    """Share of FETCH payload bytes served from the cache; None before any."""
    cache, upstream = _fetch_bytes["cache"], _fetch_bytes["upstream"]
    total = cache + upstream
    return round(cache / total, 4) if total else None


class _Status:
    """The dashboard's status.json and platform.json, rewritten every
    STATUS_INTERVAL_S."""

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
        while True:
            try:
                now = loop.time()
                _probe_catalogs(now)
                relay_status.write_json_atomic(
                    self.path, self.snapshot(now, time.time()))
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
            entry = _catalogs.get(ns)
            spaces[ns] = {
                "namespace": _ns_str(ns), "announced": bool(holders),
                "origin": (f"{_transport(holders[-1])} d{_draft(holders[-1])}"
                           if holders else None),
                "origin_addr": (relay_status.display_addr(_addr_of(holders[-1]))
                                if holders else None),
                "tracks": 0, "subscribers": len(ns_viewers.get(ns, ())),
                "subscriptions": 0, "in_bps": 0, "out_bps": 0,
                "bytes_out": 0, "format": entry[0] if entry else None,
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
            # Loss counts on delivery only: picoquic reports packets lost on
            # a session the relay mostly receives on.
            if id(s) in subs:
                sent, lost = sent + d[2], lost + d[3]
            rtt = pq.get("rtt") / 1000 if pq.get("rtt") else None
            if rtt is not None:
                rtts.append(rtt)
            role = "+".join(r for r, ids in (("pub", pubs), ("sub", subs))
                            if id(s) in ids)
            sessions.append({
                "id": info["id"], "transport": _transport(s), "draft": _draft(s),
                "addr": relay_status.display_addr(_addr_of(s)),
                "role": role or None, "since": info["since"],
                "rtt_ms": round(rtt, 1) if rtt is not None else None,
                "tx_bps": bps(d[0]) if last is not None else None,
                "rx_bps": bps(d[1]) if last is not None else None,
                "loss_pct": (round(100 * d[3] / d[2], 2) if d[2] else None),
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
            # Share of egress not received upstream ("CDN efficiency").
            "efficiency": (max(0.0, 1 - media_in / media_out)
                           if media_out else None),
            "delivered_bytes": self._delivered, "drops": self._drops,
            "fetches": sum(r["fetches"] for _, r in tracks),
            "fetch_cache_bytes": _fetch_bytes["cache"],
            "fetch_upstream_bytes": _fetch_bytes["upstream"],
            "cache_efficiency": _cache_efficiency(),
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
            "subscribers": totals["subscribers"],
            "subscriptions": totals["subscriptions"], "tracks": len(tracks),
            "loss_pct": totals["loss_pct"], "rtt_p50_ms": totals["rtt_p50_ms"],
            "lag_p99_ms": lag["p99"],
            "objects_in_per_s": totals["objects_in_per_s"],
            "groups_per_s": totals["groups_per_s"],
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
        track absent for the whole window is dropped."""
        length = self.history.length
        filled = len(self.history.series["t"])
        now = {row["id"]: row for _, row in tracks}
        for tid, row in now.items():
            if (tid not in self.track_history
                    and len(self.track_history) < TRACK_HISTORY_MAX):
                self.track_history[tid] = {
                    k: deque([None] * (filled - 1), maxlen=length)
                    for k in ("subscribers", "in_bps", "out_bps")}
        for tid, series in list(self.track_history.items()):
            row = now.get(tid)
            for k, col in series.items():
                col.append(None if row is None else row[k])
            if row is None and all(v is None for v in series["subscribers"]):
                del self.track_history[tid]

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


async def main():
    args = parse_args()
    speaks = sorted(int(d) for d in MOQTDraft)
    wanted = args.draft if isinstance(args.draft, list) else [args.draft]
    if any(d not in speaks for d in wanted):
        sys.exit(f"error: draft {','.join(map(str, wanted))} not supported; "
                 f"aiomoqt speaks {speaks}")
    log_level = logging.DEBUG if args.debug else logging.INFO
    set_log_level(log_level)
    logging.basicConfig(
        level=log_level, stream=sys.stderr,
        format=LOG_FORMAT,
    )
    if args.log_dir:
        _start_log_dir(args.log_dir)
    logger.info(f"relay: start {_versions_line()} pid {os.getpid()}")
    print("aiomoqt ERSATZ-RELAY: an API demonstrator and conformance "
          "SUT — not a production relay (no auth, no hardening).",
          file=sys.stderr)

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

    if args.watchdog > 0:
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.bind,
                                                        args.bind)
        # --quic and --dual serve raw QUIC on --port; otherwise WT.
        _background.add(asyncio.create_task(_watchdog(
            args.watchdog, host, args.port, args.quic or args.dual,
            args.draft, handles)))

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
            "cache": {"groups": CACHE_GROUPS, "bytes": CACHE_BYTES},
        }, handles)
        _background.add(asyncio.create_task(status.run()))

    print(
        "=" * 64
        + "\n EXPERIMENTAL aiomoqt interop relay.\n"
        " NOT a production relay: no auth, no scale handling.\n"
        + "=" * 64,
        file=sys.stderr,
    )
    for _server, port, label in listeners:
        print(f"Listening on {args.bind}:{port} "
              f"({label}, draft-{args.draft})", file=sys.stderr)
    if args.watchdog > 0:
        print(f"Watchdog: self-probe every {args.watchdog:g}s, exit after "
              f"{WATCHDOG_MISSES} misses or a {args.watchdog:g}s stall",
              file=sys.stderr)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        for h in handles:
            h.close()


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
