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


SCHEDULING = ("fifo", "round_robin")

# MoQT's neutral publisher priority (transport §12.4: omitted means 128).
MOQT_NEUTRAL_PRIORITY = 128
# picoquic's own default stream priority (PICOQUIC_DEFAULT_STREAM_PRIORITY).
# Odd, so picoquic's chosen discipline among equals is FIFO.
PICOQUIC_DEFAULT_PRIORITY = 9


def to_stream_priority(moqt_priority: int, *,
                       discipline: str = "fifo") -> int:
    """MoQT priority (§7.1: 0-255, lower = more urgent) to a transport byte.

    Local only. RFC 9000 §2.3 gives QUIC no wire mechanism for priority
    and asks only that an implementation offer an API, and MoQT does not
    say how its priority maps onto a scheduler — §7.2 defines an ordering
    and leaves ordering among equals implementation-defined. The wire
    still carries the full 8-bit MoQT value; only this mapping is banded.

    picoquic reads the low bit as a discipline selector among streams of
    *equal* priority — even orders by least-recently-sent (round robin),
    odd by lowest stream id (FIFO). That costs a bit of resolution and
    makes adjacent values differ in kind, so the bit is set uniformly
    from configuration and never inherited from the MoQT value.

    The map is centred on picoquic's default so a track declaring MoQT's
    neutral 128 lands exactly on 9 under the default FIFO discipline,
    which is picoquic's own. An undeclared stream, left at 9, therefore
    ranks equal to a declared-neutral one instead of outranking it — the
    two scales have different neutral points, and an identity map would
    demote every track that declared the neutral value.

    0 is only nine levels below picoquic's default, so centring costs
    resolution: eight bands, MoQT's top three bits. That is what peers
    consume anyway (moxygen reads three bits; Cloudflare ignores priority
    entirely) and more than any realistic tier count.

    round_robin cannot sit exactly on 9, which is odd, so it centres on 8
    — a declared-neutral track then outranks undeclared traffic by one
    band rather than being demoted by it.

    Follow-up worth doing: §7.1 makes priority a relation over the
    schedulable objects of one connection, so the absolute value is
    meaningless locally and only the ordering is real. Ranking the
    declared tracks and handing them consecutive bands around picoquic's
    default would give exactly as much resolution as there are distinct
    tracks. It needs re-prioritising open streams when a new declaration
    reorders the set, so it is a design step rather than an edit here.
    """
    if discipline not in SCHEDULING:
        raise AgentError(
            f"scheduling must be one of {', '.join(SCHEDULING)}, "
            f"got {discipline!r}")
    if not 0 <= moqt_priority <= 255:
        raise AgentError(
            f"MoQT priority must be 0-255, got {moqt_priority}")
    base = (PICOQUIC_DEFAULT_PRIORITY if discipline == "fifo"
            else PICOQUIC_DEFAULT_PRIORITY - 1)
    band = (moqt_priority - MOQT_NEUTRAL_PRIORITY) >> 5
    return base + 2 * band


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

    def __init__(self, session: Any, *, scheduling: str = "fifo"):
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
