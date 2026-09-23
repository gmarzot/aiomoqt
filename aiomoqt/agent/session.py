"""Connection-scoped owner of an agent's readers and writers.

MoQT priority is a relation over the schedulable objects of one
connection (transport §7.1), so the object expressing "decisions before
reasoning" has to be the object holding the connection. A list of
independent publish specs cannot say they share one.

AgentSession is that owner. It wraps a connected MOQT session, registers
each reader against the per-alias object handler so several tracks can
share the connection, and keeps the declared priorities together.

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

from typing import Any, Dict, List, Optional, Tuple

from ..types import FilterType, GroupOrder
from .errors import AgentError
from .reader import Reader
from .spec import PublishSpec, SubscribeSpec
from .writer import Writer, build_writer

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


SCHEDULING = ("round_robin", "fifo")


def to_stream_priority(moqt_priority: int, *,
                       discipline: str = "round_robin") -> int:
    """MoQT priority (§7.1: 0-255, lower = more urgent) to a transport byte.

    picoquic reads the low bit as a scheduling-discipline selector among
    streams of equal priority — even round robin, odd FIFO by stream id.
    That is its model, not MoQT's: §7.2 defines an ordering and leaves
    ordering among equals implementation-defined. So the bit is set
    uniformly from configuration and never inherited from the MoQT
    value, or two adjacent priorities would differ in discipline as well
    as in order.

    Costs one bit: 128 ordered levels. Still more resolution than peers
    read — moxygen maps the top three bits only.
    """
    if discipline not in SCHEDULING:
        raise AgentError(
            f"scheduling must be one of {', '.join(SCHEDULING)}, "
            f"got {discipline!r}")
    return (moqt_priority & 0xFE) | (1 if discipline == "fifo" else 0)


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
        self._publishes: Dict[str, PublishSpec] = {}
        self._writers: Dict[str, Writer] = {}
        self._closed = False

    @property
    def session(self) -> Any:
        return self._session

    @property
    def readers(self) -> Dict[str, Reader]:
        return dict(self._readers)

    async def reader(self, spec: SubscribeSpec) -> Reader:
        """Subscribe and return a bounded reader for the track."""
        self._guard()
        if spec.track.name is None:
            raise AgentError(
                "reader: SubscribeSpec.track.name is required; namespace "
                "discovery is a separate call")
        name = str(spec.track)
        if name in self._readers:
            raise AgentError(f"reader: already subscribed to {name}")

        rdr = Reader(spec, name)
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
            wait_response=True,
        )
        alias = getattr(ok, "track_alias", None)
        if alias is None:
            raise AgentError(f"reader: no track alias in the reply for {name}")
        rdr.alias = alias
        self._session.register_object_handler(alias, rdr.on_object)
        self._readers[name] = rdr
        return rdr

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
                                   scheduling=self.scheduling)
        self._writers[name] = wtr
        self._publishes[name] = spec
        return wtr

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

        False on a transport with no priority API (aiopquic < 0.4.1), so
        a caller is never told a relationship is applied when it is not.
        """
        return hasattr(getattr(self._session, "_quic", None),
                       "set_stream_priority")

    def close_reader(self, name: str) -> None:
        rdr = self._readers.pop(name, None)
        if rdr is None:
            return
        alias = getattr(rdr, "alias", None)
        if alias is not None:
            self._session.unregister_object_handler(alias)
        rdr.close()

    async def close(self) -> None:
        for name in list(self._readers):
            self.close_reader(name)
        for wtr in self._writers.values():
            await wtr.close()
        self._writers.clear()
        self._publishes.clear()
        self._closed = True

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
