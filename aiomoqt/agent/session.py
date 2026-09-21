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

The declared relationship is carried, not yet enforced: applying it needs
per-stream priority plumbed through aiopquic (0.5.0). Until then
`priority_plan()` reports what was asked for, and `enforced` is False.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..types import FilterType, GroupOrder
from .errors import AgentError
from .reader import Reader
from .spec import PublishSpec, SubscribeSpec

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


class AgentSession:
    """One connection, its readers, and its writers."""

    def __init__(self, session: Any):
        self._session = session
        self._readers: Dict[str, Reader] = {}
        self._publishes: Dict[str, PublishSpec] = {}
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

    def declare_publish(self, spec: PublishSpec) -> None:
        """Record a publication and its priority on this connection.

        Separate from opening a writer so the relative priorities of an
        agent's tracks can be stated before any of them produce.
        """
        self._guard()
        self._publishes[str(spec.track)] = spec

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
        return {"enforced": False, "tracks": tracks}

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
