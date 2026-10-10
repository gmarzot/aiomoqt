"""Connection-scoped owner of an agent's readers and writers.

MoQT priority is a relation over the schedulable objects of one
connection (transport §7.1), so the object expressing "decisions before
reasoning" has to be the object holding the connection. A list of
independent publish specs cannot say they share one.

AgentSession is that owner. It wraps a connected MOQT session, routes
each reader's objects, PUBLISH_DONE and stream ends through the session's
per-request and per-alias tables (never a session-wide handler slot) so
several tracks can share the connection, announces each namespace its
writers share once, and keeps the declared priorities together.

Async only. A loop-owning runtime with a blocking facade wraps this
later; nothing here assumes it is driven from the loop thread beyond
ordinary asyncio rules.

A declared publisher priority reaches the transport scheduler where one
exists (aiopquic >= 0.4.1); `priority_plan()` reports whether it actually
did rather than assuming. Scheduling among equal priorities is a local
policy, not MoQT semantics (§7.2 leaves it implementation-defined), so it
is configured per connection and never carried in a spec.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from ..types import (
    FilterType, GroupOrder, MOQTRequestError, SCHEDULING,
)
from ..utils.logger import get_logger
from .errors import AgentError
from .reader import Reader
from .spec import FetchSpec, PublishSpec, SubscribeSpec, check_supported
from .writer import Writer, build_writer

logger = get_logger(__name__)

_FILTERS = {
    "latest": FilterType.LATEST_OBJECT,
    "next_group": FilterType.NEXT_GROUP_START,
    "group": FilterType.ABSOLUTE_START,
    "range": FilterType.ABSOLUTE_RANGE,
}

_GROUP_ORDERS = {
    "ascending": GroupOrder.ASCENDING,
    "descending": GroupOrder.DESCENDING,
    "publisher_default": GroupOrder.PUBLISHER_DEFAULT,
}


def start_at_to_wire(spec_start) -> Tuple[FilterType, int, int, int]:
    """Map a StartAt onto (filter_type, start_group, start_object, end_group).

    The single place a spec meets a wire enum. Draft churn lands here:
    d19 renames SUBSCRIPTION_FILTER to LOCATION_FILTER and d20 removes
    joining FETCH, neither of which should reach the spec vocabulary.
    """
    filter_type = _FILTERS.get(spec_start.mode)
    if filter_type is None:
        raise AgentError(f"start_at mode {spec_start.mode!r} has no wire form")
    return (filter_type,
            spec_start.group or 0,
            spec_start.object or 0,
            spec_start.end_group or 0)


def fetch_range_to_wire(spec_start) -> Tuple[int, int, int, int]:
    """Map a FetchSpec range onto FETCH's (start_group, start_object,
    end_group, end_object).

    The range takes the whole end group, which session.fetch() spells
    end_object 0; its per-draft encoding is the codec's.
    """
    if spec_start.mode != "range":
        raise AgentError(
            f"fetch: start_at mode {spec_start.mode!r} is not a range")
    return (spec_start.group, spec_start.object or 0,
            spec_start.end_group, 0)


class AgentSession:
    """One connection, its readers, and its writers."""

    def __init__(self, session: Any, *, scheduling: str = "round_robin"):
        if scheduling not in SCHEDULING:
            raise AgentError(
                f"scheduling must be one of {', '.join(SCHEDULING)}, "
                f"got {scheduling!r}")
        self._session = session
        self.scheduling = scheduling
        self._readers: Dict[str, Reader] = {}
        # In-flight fetches by FETCH request id, and the tasks ending them.
        self._fetches: Dict[int, Reader] = {}
        self._fetch_tasks: Dict[int, asyncio.Future] = {}
        self._publishes: Dict[str, PublishSpec] = {}
        self._writers: Dict[str, Writer] = {}
        # Namespace -> the PUBLISH_NAMESPACE sent for this session's writers.
        self._announces: Dict[str, asyncio.Future] = {}
        self._closed = False

    @property
    def session(self) -> Any:
        return self._session

    @property
    def readers(self) -> Dict[str, Reader]:
        return dict(self._readers)

    async def reader(self, spec: SubscribeSpec) -> Reader:
        """Subscribe and return a bounded reader for the track.

        The reader is done once PUBLISH_DONE has arrived and every stream
        it counts has ended, or the session's PUBLISH_DONE_GRACE_S has
        run out; `Reader.ended` says which.
        """
        self._guard()
        check_supported(spec)
        if spec.track.name is None:
            raise AgentError(
                "reader: SubscribeSpec.track.name is required; namespace "
                "discovery is a separate call")
        name = str(spec.track)
        if name in self._readers:
            raise AgentError(f"reader: already subscribed to {name}")

        rdr = Reader(spec, name, grace_s=getattr(
            self._session, "PUBLISH_DONE_GRACE_S", 5.0))
        filter_type, start_group, start_object, end_group = start_at_to_wire(
            spec.start_at)
        ok = await self._session.subscribe(
            namespace=spec.track.namespace,
            track_name=spec.track.name,
            forward=1 if spec.forward else 0,
            filter_type=filter_type,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            priority=spec.priority.subscriber,
            group_order=_GROUP_ORDERS.get(spec.priority.group_order),
            on_object=rdr.on_object,
            on_done=rdr.on_publish_done,
            wait_response=True,
        )
        alias = getattr(ok, "track_alias", None)
        if alias is None:
            raise AgentError(f"reader: no track alias in the reply for {name}")
        rdr.alias = alias
        rdr.request_id = getattr(ok, "request_id", None)
        self._session.register_stream_end_handler(alias, rdr.on_stream_end)
        self._readers[name] = rdr
        return rdr

    async def fetch(self, spec: FetchSpec) -> Reader:
        """FETCH a bounded range and return a reader over its objects.

        Each fetch has its own reader, so several can run at once. It is
        done when the fetch stream ends, or after `spec.timeout_s`;
        `Reader.ended` says which. A refused FETCH raises AgentError.
        """
        self._guard()
        check_supported(spec)
        if spec.track.name is None:
            raise AgentError("fetch: FetchSpec.track.name is required")
        name = str(spec.track)
        rdr = Reader(spec, name)
        start_group, start_object, end_group, end_object = (
            fetch_range_to_wire(spec.start_at))
        try:
            ok = await self._session.fetch(
                namespace=spec.track.namespace,
                track_name=spec.track.name,
                subscriber_priority=spec.priority.subscriber,
                group_order=_GROUP_ORDERS.get(spec.priority.group_order),
                start_group=start_group,
                start_object=start_object,
                end_group=end_group,
                end_object=end_object,
                on_object=rdr.on_fetch_object,
                wait_response=True,
            )
        except MOQTRequestError as exc:
            raise AgentError(f"fetch {name} failed: {exc}") from exc
        rid = ok.request_id
        rdr.request_id = rid
        self._fetches[rid] = rdr
        self._fetch_tasks[rid] = asyncio.ensure_future(
            self._end_fetch(rid, rdr, spec.timeout_s))
        return rdr

    async def _end_fetch(self, rid: int, rdr: Reader, timeout: float) -> None:
        clean = await self._session.await_fetch_done(rid, timeout)
        self._session.unregister_fetch_object_handler(rid)
        self._fetches.pop(rid, None)
        self._fetch_tasks.pop(rid, None)
        rdr.mark_done("complete" if clean else "failed")

    def writer(self, spec: PublishSpec) -> Writer:
        """Register a publication on this connection.

        Builds and registers without going live: an agent usually wants
        every track and their relative priorities declared before any of
        them produces. Call `Writer.start()` to publish.
        """
        self._guard()
        name = str(spec.track)
        if name in self._writers:
            raise AgentError(f"writer: already publishing {name}")
        wtr, _track = build_writer(self._session, spec,
                                   scheduling=self.scheduling,
                                   announcer=self._announce)
        self._writers[name] = wtr
        self._publishes[name] = spec
        return wtr

    async def _announce(self, namespace: str) -> None:
        """PUBLISH_NAMESPACE `namespace` once for all of this session's
        writers under it; a namespace the app announced on the session
        itself is not sent again. A refusal raises AgentError and the
        next start() tries again."""
        fut = self._announces.get(namespace)
        if fut is None:
            if self._session.is_announced(namespace):
                return
            fut = asyncio.ensure_future(self._session.publish_namespace(
                namespace=namespace, wait_response=True))
            self._announces[namespace] = fut
        try:
            await asyncio.shield(fut)
        except Exception as exc:
            if self._announces.get(namespace) is fut:
                del self._announces[namespace]
            raise AgentError(f"announce {namespace} failed: {exc}") from exc

    @property
    def writers(self) -> Dict[str, Writer]:
        return dict(self._writers)

    def priority_plan(self) -> Dict[str, Any]:
        """What this connection has been asked to schedule, and whether
        that is actually applied.

        `enforced` is False until per-stream priority is plumbed through
        aiopquic; reporting it truthfully is the point of this method.
        """
        tracks = [
            {"track": name,
             "publisher": spec.priority.publisher,
             "subscriber": spec.priority.subscriber}
            for name, spec in self._publishes.items()
        ]
        tracks.extend(
            {"track": name,
             "publisher": None,
             "subscriber": rdr.spec.priority.subscriber}
            for name, rdr in self._readers.items())
        return {"enforced": self.scheduling_enforced,
                "scheduling": self.scheduling, "tracks": tracks}

    @property
    def scheduling_enforced(self) -> bool:
        """Whether a declared priority actually reaches the scheduler.

        Probes the setter the session would really dispatch to, not the
        session's own attribute: on WebTransport `_quic` is the session
        itself, so a hasattr() there answers True for the method being
        asked about and reports enforcement on a transport that has none.
        """
        probe = getattr(self._session, "_transport_priority_setter", None)
        if probe is not None:
            try:
                return probe() is not None
            except Exception:
                return False
        return hasattr(getattr(self._session, "_quic", None),
                       "set_stream_priority")

    def close_reader(self, name: str) -> None:
        rdr = self._readers.pop(name, None)
        if rdr is None:
            return
        if rdr.alias is not None:
            self._session.unregister_object_handler(rdr.alias)
            self._session.unregister_stream_end_handler(rdr.alias)
        rdr.close()

    async def close(self) -> None:
        """Close every reader and fetch, end each writer's track (its
        peers get PUBLISH_DONE), then withdraw the namespaces announced
        for them: at d14/d16 an aiomoqt peer ends the whole session on
        PUBLISH_NAMESPACE_DONE."""
        if self._closed:
            return
        self._closed = True
        for name in list(self._readers):
            self.close_reader(name)
        for rid, task in list(self._fetch_tasks.items()):
            task.cancel()
            self._session.unregister_fetch_object_handler(rid)
            self._fetches[rid].close()
        self._fetches.clear()
        self._fetch_tasks.clear()
        writers = list(self._writers.values())
        self._writers.clear()
        self._publishes.clear()
        await asyncio.gather(*(w.close() for w in writers),
                             return_exceptions=True)
        for namespace, fut in self._announces.items():
            if fut.done() and (fut.cancelled() or fut.exception()):
                continue
            try:
                self._session.publish_namespace_done(namespace=namespace)
            except Exception:
                logger.debug(f"withdrawing {namespace} failed", exc_info=True)
        self._announces.clear()

    def stats(self) -> Dict[str, Any]:
        return {name: rdr.stats.snapshot()
                for name, rdr in self._readers.items()}

    def _guard(self) -> None:
        if self._closed:
            raise AgentError("AgentSession is closed")

    async def __aenter__(self) -> "AgentSession":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


def readers_by_age(readers: List[Reader]) -> List[Tuple[str, Optional[float]]]:
    """Each reader's newest observed age, oldest first.

    The honest freshness view: measured per object, not inferred from a
    requested delivery timeout.
    """
    out = []
    for rdr in readers:
        newest = rdr._ring[-1].age_ms if rdr._ring else None
        out.append((rdr.name, newest))
    return sorted(out, key=lambda pair: (pair[1] is None, -(pair[1] or 0)))
