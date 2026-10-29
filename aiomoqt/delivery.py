"""Object delivery: how a numbered object sequence reaches peers.

Packaging decides what an object is (group, id, properties); delivery
decides the stream it rides and what the peer is owed at the end.
`SubgroupDelivery` serves one session, `FanoutDelivery` several. The
caller numbers objects, so every peer sees the same group and object
ids.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from enum import Enum
from typing import Any, Callable, Dict, Optional

from .messages import SubgroupHeader
from .types import MOQTStreamStopped, StreamResetCode
from .messages.data import (
    FETCH_FLAGS_END_UNKNOWN, FetchObject, ObjectDatagram,
)
from .utils.logger import TRACE, get_category_logger, get_logger

logger = get_logger(__name__)
data_log = get_category_logger("data")

# Largest Object ID every draft's varint carries (RFC 9000: 2^62-1).
MAX_OBJECT_ID = (1 << 62) - 1

# SendLane control items.
_END_GROUP = ("END_GROUP",)
_CUT = ("CUT",)
_CLOSE = ("CLOSE",)

# Bytes a send lane holds, its queued live objects plus what its streams
# still hold in the transport, before it sheds groups: the relay's per
# subscriber, and a publisher's per peer.
LANE_BYTES = 4 * 1024 * 1024
PUBLISHER_LANE_BYTES = 16 * 1024 * 1024
# Live offers between reads of a lane's transport backlog.
LANE_TX_PROBE = 8
# Seconds between a lane's shed log lines after its first.
SHED_LOG_S = 10.0
# Seconds a lane with work completes nothing before it is logged stalled.
LANE_STALL_S = 10.0


def _no_tx(_stream_id) -> int:
    return 0


def _tx_used(session):
    """The session's per-stream count of bytes queued in the transport."""
    quic = getattr(session, "_quic", None)
    return getattr(quic, "tx_data_ring_used", None) or _no_tx


def _cancelling() -> bool:
    """True when the running task itself is being cancelled."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class DatagramTooLarge(ValueError):
    """An object datagram larger than the session's datagram payload
    limit. It is not sent, nor moved to a stream."""

    def __init__(self, size: int, limit: int):
        super().__init__(f"object datagram of {size} B exceeds the "
                         f"{limit} B datagram limit")
        self.size = size
        self.limit = limit


class StreamMapping(Enum):
    PER_GROUP = "per_group"    # loc-02 §4.2: one uni stream per group
    PER_OBJECT = "per_object"  # msf-01 §6: one uni stream per object
    DATAGRAM = "datagram"      # loc-02 §4.1: one datagram per object


# Charged per retained object on top of its payload and properties, so a
# run of empty objects is bounded too: the relay's cache record (tuple,
# container slot, Object ID, arrival time, payload object header) measures
# about 217 B.
OBJECT_RECORD_BYTES = 224


def properties_charge(extensions) -> int:
    """The properties' share of retained_object_bytes."""
    return retained_object_bytes(None, extensions) - OBJECT_RECORD_BYTES


def retained_object_bytes(payload, extensions=None) -> int:
    """Memory charged for holding one object: its payload, its properties
    (an upper bound of their encoded size) and OBJECT_RECORD_BYTES."""
    n = OBJECT_RECORD_BYTES + (len(payload) if payload else 0)
    if extensions:
        if isinstance(extensions, dict):
            for v in extensions.values():
                n += 16 + (len(v) if isinstance(
                    v, (bytes, bytearray, memoryview, str)) else 0)
        elif isinstance(extensions, (bytes, bytearray, memoryview)):
            n += len(extensions)
    return n


class _HeldGroup:
    """One group's records in publication order, each
    (object_id, subgroup_id, payload, properties, cost). Those before
    `head` are evicted; `evicted` is the largest Object ID among them."""

    __slots__ = ("records", "head", "bytes", "evicted", "priority",
                 "priorities")

    def __init__(self, priority: int):
        self.records: list = []
        self.head = 0
        self.bytes = 0
        self.evicted = -1
        self.priority = priority
        # Record index -> priority, for records not at `priority`.
        self.priorities: Optional[dict] = None

    def held(self):
        """(record index, record) for each record not evicted."""
        recs = self.records
        return ((i, recs[i]) for i in range(self.head, len(recs)))

    def priority_of(self, index: int) -> int:
        if self.priorities is None:
            return self.priority
        return self.priorities.get(index, self.priority)

    def compact(self) -> None:
        """Drop the evicted slots."""
        head = self.head
        del self.records[:head]
        if self.priorities is not None:
            self.priorities = {i - head: p for i, p in self.priorities.items()
                               if i >= head} or None
        self.head = 0


# Evicted slots a group keeps before compacting, so a long group's
# record list stays bounded.
_COMPACT_AT = 1024

_BYTES_TYPES = (bytes, bytearray, memoryview, str)


class ObjectHistory:
    """Recently published objects, for answering FETCH (§10.12).

    Records are kept per group, bounded by `max_bytes` as charged by
    retained_object_bytes, evicted in batches. The oldest group is evicted
    first: whole when the excess covers it, else record by record in
    publication order. The first record of a Location wins, since a
    published object never changes (§2.1); duplicates are resolved when
    fetched, keeping the per-object add free of hashing. Properties are
    held as given and encoded for the fetching session's draft.
    """

    __slots__ = ("max_bytes", "_slack", "_fixed", "_high", "_groups",
                 "_bytes", "_through", "_last_id", "_last", "_oldest_id",
                 "_oldest")

    def __init__(self, max_bytes: int, slack: Optional[int] = None):
        self.max_bytes = max_bytes
        # Held bytes overshoot max_bytes by at most the slack, then are
        # evicted to max_bytes minus it. The default slack is
        # max(max_bytes // 8, the added object's charge); 0 evicts on
        # every add past max_bytes.
        self._fixed = slack is not None
        self._slack = max_bytes // 8 if slack is None else slack
        self._high = max_bytes + self._slack
        # Group ID -> _HeldGroup, oldest first.
        self._groups: Dict[int, _HeldGroup] = {}
        self._bytes = 0
        # Largest Location of the groups dropped whole.
        self._through: Optional[tuple] = None
        # The group the last add went to, and the oldest group.
        self._last_id: Optional[int] = None
        self._last: Optional[_HeldGroup] = None
        self._oldest_id: Optional[int] = None
        self._oldest: Optional[_HeldGroup] = None

    def __len__(self) -> int:
        return sum(len(g.records) - g.head for g in self._groups.values())

    @property
    def bytes(self) -> int:
        """Bytes charged for the records held."""
        return self._bytes

    @property
    def evicted_through(self) -> Optional[tuple]:
        """Largest Location dropped: below it, status is unknown."""
        through = self._through
        oldest = self._oldest
        if oldest is not None and oldest.evicted >= 0:
            part = (self._oldest_id, oldest.evicted)
            if through is None or part > through:
                return part
        return through

    def add(self, group_id: int, object_id: int, payload: bytes,
            subgroup_id: Optional[int] = 0, extensions: Any = None,
            priority: int = 128, charge: Optional[int] = None) -> None:
        """Record one object. `subgroup_id` None marks one sent as a
        datagram; `extensions` may be a callable taking the session that
        fetches it. `charge`, when given, is properties_charge(extensions)."""
        # retained_object_bytes, inlined for the common property values.
        cost = OBJECT_RECORD_BYTES + (len(payload) if payload else 0)
        if charge is not None:
            cost += charge
        elif extensions:
            if extensions.__class__ is dict:
                for v in extensions.values():
                    cost += 16 if v.__class__ is int else 16 + (
                        len(v) if isinstance(v, _BYTES_TYPES) else 0)
            else:
                cost = retained_object_bytes(payload, extensions)
        if group_id == self._last_id:
            grp = self._last
        else:
            grp = self._groups.get(group_id)
            if grp is None:
                grp = self._groups[group_id] = _HeldGroup(priority)
                if self._oldest is None:
                    self._oldest_id, self._oldest = group_id, grp
            self._last_id = group_id
            self._last = grp
        recs = grp.records
        if priority != grp.priority:
            if grp.priorities is None:
                grp.priorities = {}
            grp.priorities[len(recs)] = priority
        recs.append((object_id, subgroup_id, payload, extensions, cost))
        grp.bytes += cost
        held = self._bytes + cost
        self._bytes = held
        if held > self._high:
            if self._fixed:
                self._evict(self.max_bytes - self._slack)
            elif held - cost > self.max_bytes:
                self._evict(self.max_bytes - max(self._slack, cost))

    def _evict(self, cap: int) -> None:
        """Evict down to `cap` bytes held."""
        held = self._bytes
        grp = self._oldest
        while held > cap:
            recs = grp.records
            head = grp.head
            if head == len(recs) - 1 or grp.bytes <= held - cap:
                self._drop_oldest()
                held -= grp.bytes
                grp = self._oldest
                if grp is None:
                    break
                continue
            rec = recs[head]
            recs[head] = None
            grp.head = head + 1
            grp.bytes -= rec[4]
            held -= rec[4]
            if rec[0] > grp.evicted:
                grp.evicted = rec[0]
            if head >= _COMPACT_AT and 2 * head >= len(recs):
                grp.compact()
        self._bytes = held

    def _drop_oldest(self) -> None:
        """Drop the oldest group whole."""
        groups = self._groups
        group_id, grp = self._oldest_id, self._oldest
        del groups[group_id]
        gone = (group_id, max(grp.evicted,
                               max(r[0] for _, r in grp.held())))
        if self._through is None or gone > self._through:
            self._through = gone
        if grp is self._last:
            self._last_id = self._last = None
        if groups:
            self._oldest_id = next(iter(groups))
            self._oldest = groups[self._oldest_id]
        else:
            self._oldest_id = self._oldest = None

    def _held(self, start: tuple, end: tuple) -> dict:
        """Location -> (subgroup_id, payload, properties, priority) of the
        first record of it, for Locations in [start, end]."""
        held = {}
        for group_id, grp in self._groups.items():
            if not start[0] <= group_id <= end[0]:
                continue
            for i, rec in grp.held():
                key = (group_id, rec[0])
                if start <= key <= end and key not in held:
                    held[key] = (rec[1], rec[2], rec[3], grp.priority_of(i))
        return held

    def first_held(self, group_id: int) -> Optional[int]:
        """The Object ID of a group's earliest record held, None if none is."""
        grp = self._groups.get(group_id)
        if grp is None or grp.head >= len(grp.records):
            return None
        return grp.records[grp.head][0]

    def discard(self, group_id: int) -> None:
        """Drop a group's records; its Locations then read as absent, not
        evicted."""
        grp = self._groups.pop(group_id, None)
        if grp is None:
            return
        self._bytes -= grp.bytes
        if grp is self._last:
            self._last_id = self._last = None
        if grp is self._oldest:
            groups = self._groups
            self._oldest_id = next(iter(groups), None)
            self._oldest = groups.get(self._oldest_id)

    def last_object(self, group_id: int) -> Optional[int]:
        """The largest Object ID held in a group, None if none is."""
        grp = self._groups.get(group_id)
        if grp is None:
            return None
        return max(r[0] for _, r in grp.held())

    def fetch(self, start: tuple, end: tuple, session=None, *,
              descending: bool = False,
              lost_marker: int = FETCH_FLAGS_END_UNKNOWN) -> list:
        """FetchObjects from `start` to `end`, both (group, object) and
        inclusive. Groups ascend, or descend; objects ascend within a
        group (§10.12.3). A part of the range already dropped is one End
        of Range marker of type `lost_marker` (End of Unknown Range by
        default, §11.4.4.2), at the low end of the range. An `end` past
        MAX_OBJECT_ID (a whole group) ends the marker at the group's last
        held Object, else at MAX_OBJECT_ID."""
        lost = None
        through = self.evicted_through
        if through is not None and start <= through:
            lost = min(through, end)
            if lost[1] > MAX_OBJECT_ID:
                last = self.last_object(lost[0])
                lost = (lost[0], MAX_OBJECT_ID if last is None else last)
            start = max(start, (lost[0], lost[1] + 1))
        held = self._held(start, end)
        keys = sorted(held)
        if descending:
            keys.sort(key=lambda k: (-k[0], k[1]))
        out = []
        for key in keys:
            subgroup_id, payload, exts, priority = held[key]
            if callable(exts):
                exts = exts(session)
            out.append(FetchObject(
                group_id=key[0], subgroup_id=subgroup_id or 0,
                object_id=key[1], publisher_priority=priority,
                extensions=exts or None, payload=payload,
                datagram=subgroup_id is None))
        if lost is not None:
            marker = FetchObject(group_id=lost[0], object_id=lost[1],
                                 end_of_range=lost_marker)
            if descending:
                out.append(marker)
            else:
                out.insert(0, marker)
        return out


def _same_extensions(a, b) -> bool:
    """True when two extension blocks encode alike: equal, in the same
    order (d14 encodes them in order)."""
    if a is b:
        return True
    if a is None or b is None or a != b:
        return False
    return list(a) == list(b)


# Encodings of one object kept for other peers. Each is held while the
# object is queued; past this, peers in further states encode their own.
_ENCODINGS_MAX = 4


def _cached(encoded: list, key, extensions) -> Optional[bytes]:
    """The bytes another peer got for this object under `key` with the
    same extensions, None if none did."""
    for k, exts, data in encoded:
        if k == key and _same_extensions(exts, extensions):
            return data
    return None


def _cache(encoded: list, key, extensions, data: bytes) -> None:
    if len(encoded) < _ENCODINGS_MAX:
        encoded.append((key, extensions, data))


class SubgroupDelivery:
    """One session's copy of an object sequence.

    PER_GROUP holds a stream open for the length of a group, PER_OBJECT
    opens one per object, DATAGRAM uses no stream at all. `largest` and
    `stream_count` are what this peer has been sent — the numbers
    SUBSCRIBE_OK and PUBLISH_DONE owe it.
    """

    def __init__(self, session, track_alias: int, *, priority: int = 128,
                 mapping: StreamMapping = StreamMapping.PER_GROUP,
                 stream_priority: Optional[int] = None):
        self.session = session
        self.track_alias = track_alias
        self.priority = priority
        # Transport band for this track's streams, precomputed by the
        # caller; None leaves the session default.
        self.stream_priority = stream_priority
        self.mapping = mapping
        self.stream_count = 0
        self.largest: Optional[tuple] = None
        self.objects_sent = 0
        self.bytes_sent = 0
        # passes(group_id, object_id): the peer's subscription filter
        # (§5.1.2); objects it excludes are not sent. None admits all.
        self.passes: Optional[Callable[[int, int], bool]] = None
        # excludes_end(group_id, object_id): True when the filter ends in
        # this group before object_id, so no END_OF_GROUP there.
        self.excludes_end: Optional[Callable[[int, int], bool]] = None
        self._stream_id: Optional[int] = None
        self._header: Optional[SubgroupHeader] = None
        # Group whose stream the peer stopped; not reopened (§11.3.2).
        self._stopped_group: Optional[int] = None
        # Counts reset_group() calls; a write spanning one is abandoned.
        self._epoch = 0
        # False when no other lane sends the same datagram bytes; set by
        # FanoutDelivery.
        self._datagrams_shared = True
        # The SendLane writing through this delivery, if any.
        self.lane = None
        # The session's write that never waits, if it has one.
        self._write_nowait = getattr(session, "stream_write_nowait", None)
        # stream_id -> group_id of FINed streams the transport may still
        # hold bytes of; kept only behind a lane.
        self._tx_streams: dict = {}

    @property
    def _prof(self):
        return self.session._profile

    def _note_largest(self, group_id: int, object_id: int) -> None:
        """Largest Location is a max: group send order is not guaranteed
        monotonic (§2.3.1)."""
        if self.largest is None or (group_id, object_id) > self.largest:
            self.largest = (group_id, object_id)

    def end_group(self) -> None:
        """Close the open group stream with an END_OF_GROUP marker and a
        FIN. No-op when no stream is open."""
        if self._stream_id is None:
            return
        end_id = self._header.next_object_id
        if (self.excludes_end is not None
                and self.excludes_end(self._header.group_id, end_id)):
            self.session.stream_write(self._stream_id, b"", end_stream=True)
        else:
            buf = self._header.end_group(object_id=end_id)
            self.session.stream_write(self._stream_id, buf.data,
                                      end_stream=True)
        if self.lane is not None:
            self._tx_streams[self._stream_id] = self._header.group_id
        self._stream_id = None
        self._header = None

    async def write(self, group_id: int, object_id: int, payload: bytes, *,
                    extensions: Optional[Dict[int, Any]] = None,
                    group_start: bool = False,
                    encoded: Optional[list] = None) -> bool:
        """Place one object; False when the peer's filter excludes it.
        `group_start` rotates the PER_GROUP stream. A callable
        `extensions` is resolved against this session. `encoded` is a
        list shared by the deliveries writing this object: peers in the
        same encoding state reuse one encoding through it."""
        if self.passes is not None and not self.passes(group_id, object_id):
            if group_start:
                self.end_group()
            return False
        if group_id == self._stopped_group and not group_start:
            return False
        if callable(extensions):
            extensions = extensions(self.session)
        # A reset_group() can land at any await below; the write then
        # stops without touching what the reset left.
        epoch = self._epoch
        try:
            if self.mapping is StreamMapping.DATAGRAM:
                data = self._datagram_bytes(
                    group_id, object_id, payload, extensions, encoded)
                limit = self.session.datagram_max_payload()
                if len(data) > limit:
                    raise DatagramTooLarge(len(data), limit)
                await self.session.dgram_write_drain(data)
            elif self.mapping is StreamMapping.PER_OBJECT:
                # One stream per object ⇒ one subgroup per object (a subgroup
                # owns exactly one stream); the object keeps its decode-order
                # id via subgroup_id.
                lane = self.lane
                if lane is not None:
                    lane.parked, lane.sid = "header", None
                sid = await self.session.open_uni_stream()
                self.stream_count += 1
                if lane is not None:
                    lane.sid = sid
                if self.stream_priority is not None:
                    self.session.set_stream_priority(sid, self.stream_priority)
                hdr = SubgroupHeader(
                    track_alias=self.track_alias, group_id=group_id,
                    subgroup_id=object_id, publisher_priority=self.priority,
                    extensions_present=True, prof=self._prof)
                await self.session.stream_write_drain(sid, hdr.serialize().data)
                if lane is not None:
                    lane.parked = "object"
                data = self._object_bytes(hdr, object_id, payload,
                                          extensions, encoded)
                await self.session.stream_write_drain(sid, data)
                self.session.stream_write(sid, b"", end_stream=True)
                if lane is not None:
                    self._tx_streams[sid] = group_id
            else:  # PER_GROUP
                if group_start or self._stream_id is None:
                    # A filter can admit a group part-way through: the stream
                    # then opens on a later object, not the group's first.
                    self.end_group()
                    lane = self.lane
                    if lane is not None:
                        lane.parked, lane.sid = "header", None
                    sid = await self.session.open_uni_stream()
                    self.stream_count += 1
                    if lane is not None:
                        lane.sid = sid
                    if self._epoch != epoch:
                        self.session.stream_reset(sid, StreamResetCode.CANCELLED)
                        return False
                    if self.stream_priority is not None:
                        self.session.set_stream_priority(
                            sid, self.stream_priority)
                    hdr = SubgroupHeader(
                        track_alias=self.track_alias, group_id=group_id,
                        subgroup_id=0, publisher_priority=self.priority,
                        extensions_present=True, prof=self._prof,
                        first_object=group_start)
                    self._stream_id, self._header = sid, hdr
                    await self.session.stream_write_drain(
                        sid, hdr.serialize().data)
                    if lane is not None:
                        lane.parked = "object"
                    if self._epoch != epoch:
                        return False
                sid, hdr = self._stream_id, self._header
                data = self._object_bytes(hdr, object_id, payload,
                                          extensions, encoded)
                await self.session.stream_write_drain(sid, data)
                if self._epoch != epoch:
                    return False
        except MOQTStreamStopped:
            # The session has reset the stream; a group's is not reopened.
            if (self.mapping is StreamMapping.PER_GROUP
                    and self._epoch == epoch):
                self._stopped_group = group_id
                self._stream_id = None
                self._header = None
            return False
        self._count_sent(group_id, object_id, payload)
        return True

    def _count_sent(self, group_id: int, object_id: int,
                    payload: bytes) -> None:
        self._note_largest(group_id, object_id)
        self.objects_sent += 1
        self.bytes_sent += len(payload)
        if data_log.isEnabledFor(TRACE):
            data_log.log(TRACE, f"MOQT send: object {group_id}.{object_id} "
                                f"alias={self.track_alias} {len(payload)} "
                                f"bytes ({self.mapping.name})")

    def _datagram_key(self) -> tuple:
        """Every field of this delivery its datagrams' bytes depend on."""
        prof = self._prof
        return (self.track_alias, self.priority,
                prof is not None and prof.vi64,
                prof is not None and prof.merged_datagram_layout,
                prof is not None and prof.params_delta_coded)

    def _datagram_bytes(self, group_id: int, object_id: int, payload: bytes,
                        extensions, encoded: Optional[list]) -> bytes:
        """The object as this peer's datagram."""
        key = None
        if encoded is not None and self._datagrams_shared:
            key = self._datagram_key()
            data = _cached(encoded, key, extensions)
            if data is not None:
                return data
        data = ObjectDatagram(
            track_alias=self.track_alias, group_id=group_id,
            object_id=object_id, publisher_priority=self.priority,
            extensions=extensions,
            payload=payload).serialize(prof=self._prof).data
        if key is not None:
            _cache(encoded, key, extensions, data)
        return data

    @staticmethod
    def _object_bytes(hdr: SubgroupHeader, object_id: int, payload: bytes,
                      extensions, encoded: Optional[list]) -> bytes:
        """The object's bytes on hdr's stream, keyed by the header's
        encoding state: the bytes depend on it and the object only."""
        if encoded is None:
            return hdr.next_object_bytes(payload, extensions,
                                         object_id=object_id)
        state = hdr.encoding_state()
        data = _cached(encoded, state, extensions)
        if data is None:
            data = hdr.next_object_bytes(payload, extensions,
                                         object_id=object_id)
            _cache(encoded, state, extensions, data)
        else:
            hdr.advance(object_id)
        return data

    @property
    def open_group(self) -> Optional[int]:
        """Group ID of the open PER_GROUP stream, None when none is."""
        return None if self._header is None else self._header.group_id

    def reset_group(self, code: int = StreamResetCode.CANCELLED) -> None:
        """Reset the open group stream: the group is truncated, and a FIN
        would claim it complete. No-op when no stream is open."""
        self._epoch += 1
        if self._stream_id is None:
            return
        self.session.stream_reset(self._stream_id, code)
        self._stream_id = None
        self._header = None

    def abort(self) -> None:
        """Give up now, resetting the open group."""
        self.reset_group()

    def cancel(self) -> None:
        """The peer's subscription ended part-way through a group."""
        self.reset_group()

    async def close(self) -> None:
        """Finish cleanly: close the open group."""
        self.end_group()

    # -- SendLane writer -------------------------------------------------

    def send(self, item):
        """Write a SendLane item: (group_id, cost, live, born, object_id,
        payload, extensions, group_start, encoded)."""
        return self.write(item[0], item[4], item[5], extensions=item[6],
                          group_start=item[7], encoded=item[8])

    def send_nowait(self, item) -> Optional[bool]:
        """send(item) when it completes without waiting: an object on the
        open group stream that the transport takes now. True when
        written, False when skipped as write() skips it; None, with
        nothing written, when it has to wait."""
        sid = self._stream_id
        write = self._write_nowait
        if sid is None or item[7] or write is None:
            return None
        group_id, object_id = item[0], item[4]
        if self.passes is not None and not self.passes(group_id, object_id):
            return False
        hdr = self._header
        last = hdr.next_object_id - 1
        if last < 0:
            return None
        payload, extensions = item[5], item[6]
        if callable(extensions):
            extensions = extensions(self.session)
        data = self._object_bytes(hdr, object_id, payload, extensions,
                                  item[8])
        try:
            if not write(sid, data):
                hdr.advance(last)
                return None
        except MOQTStreamStopped:
            self._stopped_group = group_id
            self._stream_id = None
            self._header = None
            return False
        self._count_sent(group_id, object_id, payload)
        return True

    def close_streams(self) -> None:
        self.end_group()

    def final(self, *_args) -> None:
        """The track sends PUBLISH_DONE once production ends."""

    def expire(self, group_id: int) -> bool:
        """§8: an object past the delivery timeout is dropped; on a
        stream, its subgroup is reset with DELIVERY_TIMEOUT and not
        reopened. True when the rest of the group is skipped."""
        if self.mapping is StreamMapping.DATAGRAM:
            return False
        if self.open_group == group_id:
            self.reset_group(StreamResetCode.DELIVERY_TIMEOUT)
        else:
            self.end_group()
        return True

    def tx_queued(self, floor: Optional[int] = None) -> int:
        """Bytes the transport still holds on this delivery's streams,
        groups below `floor` excepted. Drained FINed streams are
        forgotten."""
        used = _tx_used(self.session)
        total = 0
        if self._stream_id is not None and (
                floor is None or self._header.group_id >= floor):
            total = used(self._stream_id)
        tx = self._tx_streams
        if tx:
            for sid, gid in list(tx.items()):
                n = used(sid)
                if not n:
                    del tx[sid]
                elif floor is None or gid >= floor:
                    total += n
        return total

    def reset_below(self, floor: int) -> None:
        """Reset the streams of groups below `floor`: the open one, and
        FINed ones the transport has not yet sent out."""
        if self._header is not None and self._header.group_id < floor:
            self.reset_group()
        tx = self._tx_streams
        if tx:
            used = _tx_used(self.session)
            for sid, gid in list(tx.items()):
                if gid < floor:
                    del tx[sid]
                    if used(sid):
                        self.session.stream_reset(
                            sid, StreamResetCode.CANCELLED)


class SendLane:
    """One peer's objects behind its own FIFO and writer task.

    The source hands each object to every lane without waiting, so a
    peer held up by its transport holds up only itself. Past `max_bytes`
    of live objects, counting what the peer's streams still hold in the
    transport, the lane sheds whole groups: their streams are reset,
    never FINed short, and delivery resumes at a newer group. Group IDs
    are taken to ascend.

    An item is a tuple (group_id, cost, live, born, ...), the rest for
    the writer: `cost` is the object's retained_object_bytes, 0 for a
    stream marker; `live` objects count against the bound, a replay does
    not; `born` is the monotonic time it was produced, or None. The
    writer is the peer's delivery: send(item), end_group(),
    reset_group(code), reset_below(floor), close_streams(), final(*args),
    tx_queued(floor) and expire(group_id). A writer may set `parked`
    ("header" or "object") and `sid` on the lane while a send waits.

    Write-through: a writer with send_nowait(item) is handed an object
    inline from offer() while the lane is empty and under its bound; it
    is queued only when the writer would wait, so order is kept.
    """

    log = logger
    prefix = ""
    write_through = True

    def __init__(self, writer, *, max_bytes: int = LANE_BYTES,
                 name: str = ""):
        self.writer = writer
        self.max_bytes = max_bytes
        self.name = name         # the track's, for logs
        self.items: deque = deque()
        self.bytes = 0           # live object bytes queued
        self.replay_bytes = 0    # replay bytes queued, not bounded
        self.tx_bytes = 0        # held by the transport, as last read
        self._probe_gid = None
        self._probe_n = 0
        self.objects = 0
        self.floor = None        # objects of groups below it are not sent
        self._lost = None        # newest group shed, expired or failed
        self.shed = 0            # objects shed
        self.sheds = 0           # shed episodes
        self.expired = 0         # objects past the delivery timeout (§8)
        # False while the peer's Forward State is 0 (§5.1): nothing is
        # sent, and the lane resumes at a later group.
        self.gate = True
        # Object delivery timeout in seconds (§8), None for none.
        self._deadline: Optional[float] = None
        # Called when `deadline` changes.
        self.on_deadline: Optional[Callable] = None
        # Called with each object item the writer sent.
        self.on_sent: Optional[Callable] = None
        # Called with an error that ends the track; the lane then stops.
        self.on_fatal: Optional[Callable] = None
        self._logged_at = None
        self._logged_sheds = 0
        self.shedding = False
        self.final = False       # finish() queued: takes nothing more
        self.busy = False        # a send is in progress
        self.parked = None       # what that send awaits: header or object
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
        self._nowait = (getattr(writer, "send_nowait", None)
                        if self.write_through else None)
        self.task = asyncio.ensure_future(self._run())

    @property
    def session(self):
        return self.writer.session

    @property
    def deadline(self) -> Optional[float]:
        return self._deadline

    @deadline.setter
    def deadline(self, value: Optional[float]) -> None:
        self._deadline = value or None
        if self.on_deadline is not None:
            self.on_deadline()

    @property
    def idle(self) -> bool:
        return not self.items and not self.busy

    @property
    def opening(self) -> bool:
        """The send in progress waits to open a stream or write its header."""
        return self.parked == "header"

    def _put(self, item) -> None:
        self.items.append(item)
        self._wake.set()

    def offer(self, item) -> bool:
        """Write an object or stream marker through, or queue it. True
        when it was queued."""
        if self.stopped or self.final:
            return False
        gid, cost, live = item[0], item[1], item[2]
        floor = self.floor
        if floor is not None and gid < floor:
            if cost and self.gate:
                self.shed += 1
            return False
        if live:
            if gid != self._probe_gid or self._probe_n >= LANE_TX_PROBE:
                self._probe_gid, self._probe_n = gid, 0
                self.tx_bytes = self.writer.tx_queued(floor)
            self._probe_n += 1
        nowait = self._nowait
        if (nowait is not None and not self.items and not self.busy
                and self.gate and self.tx_bytes + cost <= self.max_bytes):
            try:
                sent = nowait(item)
            except Exception as e:
                if not self._failed(item, e):
                    self.stop()
                return False
            if sent is not None:
                self.done += 1
                if sent and self.on_sent is not None:
                    self.on_sent(item)
                return False
        if cost:
            self.objects += 1
            if live:
                self.bytes += cost
            else:
                self.replay_bytes += cost
        self.items.append(item)
        self._wake.set()
        if live and self.bytes + self.tx_bytes > self.max_bytes:
            self._shed(gid)
        return True

    def end_group(self) -> None:
        """Close the writer's open group once what is queued is out."""
        if not self.stopped and not self.final:
            self._put(_END_GROUP)

    def reset_group(self, group_id: Optional[int] = None) -> None:
        """Reset the writer's open group once what is queued is out; the
        lane resumes past `group_id`."""
        if not self.stopped and not self.final:
            self._put(("RESET_GROUP", group_id))

    def close_streams(self) -> None:
        """FIN the peer's open streams once what is queued is out."""
        if not self._closing and not self.final and not self.stopped:
            self._closing = True
            self._put(_CLOSE)

    def finish(self, *args) -> None:
        """writer.final(*args) once what is queued is out, then end;
        nothing more is taken."""
        if not self.final and not self.stopped:
            self.final = True
            self._put(("FINAL", *args))

    def resume(self, group_id: int, rejoin: bool = False) -> None:
        """Forward State back to 1 (§5.1) with the source in `group_id`.
        What is queued of it and earlier groups is dropped and the
        writer's open group reset: the peer's joining FETCH covers them.
        With `rejoin` the lane continues `group_id` from its next object
        on a new stream (FIRST_OBJECT clear), unless the group was shed,
        expired or failed; otherwise it waits for the next group."""
        if self.stopped or self.final:
            return
        kept = deque()
        for item in self.items:
            gid = item[0]
            if gid.__class__ is not str:
                if gid > group_id:
                    kept.append(item)
                else:
                    self._uncount(item)
            elif gid != "RESET_GROUP" or (item[1] or 0) > group_id:
                kept.append(item)
        self.items = kept
        self.writer.reset_group()
        if not rejoin or self._lost == group_id:
            self._skip(group_id, count=False)
        elif self.floor == group_id + 1:
            self.floor = group_id

    def _uncount(self, item) -> None:
        cost = item[1]
        if cost:
            self.objects -= 1
            if item[2]:
                self.bytes -= cost
            else:
                self.replay_bytes -= cost

    def _drop_below(self, floor, count: bool = True) -> None:
        kept = deque()
        for item in self.items:
            if item[0].__class__ is str or item[0] >= floor:
                kept.append(item)
                continue
            self._uncount(item)
            if count and item[1]:
                self.shed += 1
        self.items = kept

    def _skip(self, gid, count: bool = True) -> None:
        """Send nothing more of groups up to `gid`."""
        floor = gid + 1
        if self.floor is None or floor > self.floor:
            self.floor = floor
            self._drop_below(floor, count)

    def _shed(self, gid) -> None:
        """Drop the groups queued before `gid`, and `gid` too if it alone
        is over the bound; their open streams are reset first."""
        writer = self.writer
        floor = gid
        self._drop_below(floor)
        self.tx_bytes = writer.tx_queued(floor)
        if self.bytes + self.tx_bytes > self.max_bytes:
            floor = gid + 1
            self._drop_below(floor)
            self.tx_bytes = writer.tx_queued(floor)
        self.floor = floor if self.floor is None else max(self.floor, floor)
        self._lost = self.floor - 1
        if self.busy and self.opening:
            # A parked header write waits on this session's transport
            # backlog, which only these resets free.
            writer.reset_below(self.floor)
        if not self._cut:
            self._cut = True
            self.items.appendleft(_CUT)
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
        who = f"{self.prefix}{self._who()}"
        if self._logged_at is None:
            self.log.warning(
                f"{who} over its {self.max_bytes}-byte send lane: resetting "
                f"groups below {self.floor}")
        else:
            self.log.info(
                f"{who} shed {self.sheds - self._logged_sheds} more "
                f"time(s) over its send lane: resetting groups below "
                f"{self.floor}")
        self._logged_at, self._logged_sheds = now, self.sheds

    def stop(self) -> bool:
        """End now, dropping what is queued. True if an object was
        dropped or its send abandoned."""
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
        if self._stalled:
            self._stalled = False
            self.log.info(f"{self.prefix}lane ended stalled after "
                          f"{time.monotonic() - self._moved_at:.0f}s: "
                          f"{self._who()}")

    def _who(self) -> str:
        return (f"lane {self.name} alias="
                f"{getattr(self.writer, 'track_alias', None)} peer="
                f"{getattr(self.session, 'peer_address', None)}")

    def _stall_detail(self) -> str:
        return f"stream {self.sid}"

    def stalled_s(self, now) -> float:
        """Seconds stalled in the current episode; 0 while moving."""
        return now - self._moved_at if self._stalled else 0

    def check(self, now, stall_s: float = LANE_STALL_S) -> None:
        """Track progress: one WARNING when the lane has work and has
        completed nothing for `stall_s`, one INFO when it moves again."""
        if self.done != self._seen or not (self.busy or self.items):
            if self._stalled:
                self._stalled = False
                self.log.info(f"{self.prefix}lane resumed after "
                              f"{now - self._moved_at:.0f}s: {self._who()}")
            self._seen, self._moved_at = self.done, now
            return
        if self._stalled or now - self._moved_at < stall_s:
            return
        self._stalled = True
        self.stalls += 1
        self.log.warning(
            f"{self.prefix}lane stalled {now - self._moved_at:.0f}s: "
            f"{self._who()} parked on "
            f"{self.parked if self.busy else 'queue'}; "
            f"lane_bytes={self.bytes + self.replay_bytes} "
            f"{self._stall_detail()}")

    def _woke(self) -> bool:
        """Before the first item after an idle wait; False ends the lane."""
        return True

    def _failed(self, item, error: BaseException) -> bool:
        """A send raised. True resumes at the next group; False ends the
        lane."""
        if isinstance(error, DatagramTooLarge):
            self.log.error(f"Fanout: {self._who()}: {error}")
            if self.on_fatal is not None:
                self.on_fatal(error)
            return False
        if isinstance(error, asyncio.CancelledError):
            return False
        self.log.warning(f"Fanout: {self._who()} write failed; resuming "
                         f"at the next group", exc_info=error)
        try:
            self.writer.reset_group()
        except Exception:
            pass
        self._skip(item[0])
        self._lost = item[0]
        return True

    def _control(self, item) -> bool:
        """Act on a control item; False ends the lane."""
        kind = item[0]
        writer = self.writer
        try:
            if kind == "CUT":
                self._cut = False
                writer.reset_below(self.floor)
            elif kind == "END_GROUP":
                writer.end_group()
            elif kind == "RESET_GROUP":
                writer.reset_group()
                if item[1] is not None:
                    self._skip(item[1])
            elif kind == "CLOSE":
                self._closing = False
                writer.close_streams()
            else:
                writer.final(*item[1:])
                return False
        except Exception:
            self.log.debug(f"{self.prefix}{kind} on a send lane failed",
                           exc_info=True)
            return kind != "FINAL"
        return True

    async def _run(self) -> None:
        writer = self.writer
        woke = True
        try:
            while not self.stopped:
                items = self.items
                if not items:
                    self.shedding = False
                    self._wake.clear()
                    await self._wake.wait()
                    woke = True
                    continue
                if woke:
                    woke = False
                    if not self._woke():
                        return
                item = items.popleft()
                gid = item[0]
                if gid.__class__ is str:
                    if not self._control(item):
                        return
                    self.done += 1
                    continue
                cost = item[1]
                if cost:
                    self.objects -= 1
                    if item[2]:
                        self.bytes -= cost
                    else:
                        self.replay_bytes -= cost
                if not self.gate:
                    # Forward State 0: send nothing, reset the open group,
                    # and resume at a later group.
                    writer.reset_group()
                    self._skip(gid, count=False)
                    self.done += 1
                    continue
                deadline = self._deadline
                if (deadline is not None and item[3] is not None
                        and time.monotonic() - item[3] > deadline):
                    # §8: not sent; on a stream the group is not resumed.
                    self.expired += 1
                    if writer.expire(gid):
                        self._skip(gid)
                        self._lost = gid
                    self.done += 1
                    continue
                self.busy = True
                try:
                    sent = await writer.send(item)
                except (Exception, asyncio.CancelledError) as e:
                    if _cancelling():
                        raise
                    if not self._failed(item, e):
                        return
                    sent = False
                finally:
                    self.busy = False
                if sent and self.on_sent is not None:
                    self.on_sent(item)
                self.done += 1
        finally:
            self._exit()


class FanoutDelivery:
    """Several deliveries of one object sequence, behind the
    SubgroupDelivery interface.

    The source numbers objects once and writes them here, so every peer
    sees the same group and object ids — a subscriber that moves between
    two relays fed by this publisher finds the same content under the
    same numbers. Each peer has its own SendLane: one that cannot keep up
    loses whole groups rather than slowing the others down.
    """

    def __init__(self, deliveries=(), *,
                 max_bytes: int = PUBLISHER_LANE_BYTES):
        self.max_bytes = max_bytes
        self.name = ""       # the track's, for logs
        self.lanes: list = []
        # Called as on_write(group_id, object_id, payload, extensions) for
        # each object, once, however many lanes there are.
        self.on_write: Optional[Callable] = None
        # Called as on_sent(group_id, object_id) each time a lane writes
        # an object to its peer: once per peer, never for one gated,
        # filtered out or shed.
        self.on_sent: Optional[Callable] = None
        # An error that ends the track (DatagramTooLarge); write() raises it.
        self.error: Optional[Exception] = None
        # Group of the last object written.
        self._group: Optional[int] = None
        # Some lane has a delivery timeout: objects are stamped when made.
        self._timed = False
        for delivery in deliveries:
            self.add(delivery, joining=False)

    def add(self, delivery: SubgroupDelivery, gate: bool = True,
            joining: bool = True,
            deadline: Optional[float] = None) -> SendLane:
        """Attach another peer. `joining` holds it to the next group
        boundary, which is where a mid-stream arrival can start. `gate`
        and `deadline` set the lane's Forward State and delivery timeout
        in seconds; the owner updates them on the lane."""
        lane = SendLane(delivery, max_bytes=self.max_bytes, name=self.name)
        lane.gate = gate
        lane.on_sent = self._sent
        lane.on_fatal = self._fail
        lane.on_deadline = self._retime
        delivery.lane = lane
        if joining and self._group is not None:
            lane.floor = self._group + 1
        self.lanes = [*self.lanes, lane]
        lane.deadline = deadline
        self._mark_shared_datagrams()
        return lane

    def _retime(self) -> None:
        self._timed = any(ln.deadline is not None for ln in self.lanes)

    def _fail(self, error: Exception) -> None:
        if self.error is None:
            self.error = error

    def _mark_shared_datagrams(self) -> None:
        """Datagrams carry the alias, so a lane shares their bytes only
        with lanes of the same datagram key; the rest keep none."""
        lanes = [ln.writer for ln in self.lanes
                 if ln.writer.mapping is StreamMapping.DATAGRAM]
        keys = Counter(d._datagram_key() for d in lanes)
        for d in lanes:
            d._datagrams_shared = keys[d._datagram_key()] > 1

    def _sent(self, item) -> None:
        if self.on_sent is not None:
            self.on_sent(item[0], item[4])

    def drop_session(self, session) -> bool:
        """Detach a session's lanes, resetting any open group. True if it
        had one."""
        mine = [ln for ln in self.lanes if ln.session is session]
        if not mine:
            return False
        self.lanes = [ln for ln in self.lanes if ln.session is not session]
        for lane in mine:
            lane.stop()
            lane.writer.reset_group()
        self._mark_shared_datagrams()
        self._retime()
        return True

    @property
    def stream_count(self) -> int:
        return max((ln.writer.stream_count for ln in self.lanes),
                   default=0)

    @property
    def largest(self) -> Optional[tuple]:
        seen = [ln.writer.largest for ln in self.lanes
                if ln.writer.largest is not None]
        return max(seen) if seen else None

    async def write(self, group_id: int, object_id: int, payload: bytes, *,
                    extensions: Optional[Dict[int, Any]] = None,
                    group_start: bool = False) -> None:
        if self.error is not None:
            raise self.error
        if self.on_write is not None:
            self.on_write(group_id, object_id, payload, extensions)
        lanes = self.lanes
        # One encoding per header state, shared by the lanes in it; it
        # lives as long as the item is queued.
        encoded = [] if len(lanes) > 1 else None
        cost = OBJECT_RECORD_BYTES + (len(payload) if payload else 0)
        if extensions.__class__ is dict:
            # retained_object_bytes, inlined for the common property values.
            for v in extensions.values():
                cost += 16 if v.__class__ is int else 16 + (
                    len(v) if isinstance(v, _BYTES_TYPES) else 0)
        elif extensions is not None and not callable(extensions):
            cost = retained_object_bytes(payload, extensions)
        item = (group_id, cost, True,
                time.monotonic() if self._timed else None, object_id,
                payload, extensions, group_start, encoded)
        self._group = group_id
        queued = False
        for lane in lanes:
            if lane.offer(item):
                queued = True
        if queued:
            # Let a lane that could not take the object inline catch up.
            await asyncio.sleep(0)

    def end_group(self) -> None:
        for lane in list(self.lanes):
            lane.end_group()

    def reset_group(self) -> None:
        """Truncate the open group on every lane; each resumes at the
        next group."""
        for lane in list(self.lanes):
            lane.reset_group(self._group)

    def abort(self) -> None:
        """Give up on every lane without waiting, resetting open groups."""
        for lane in list(self.lanes):
            lane.stop()
            lane.writer.reset_group()

    async def close(self, timeout: float = 5.0) -> None:
        """Close the open group on every lane and wait for them to
        flush. A lane that will not drain is abandoned, not waited on."""
        lanes = list(self.lanes)
        for lane in lanes:
            lane.end_group()
            lane.finish()
        await asyncio.gather(
            *(asyncio.wait_for(ln.task, timeout) for ln in lanes),
            return_exceptions=True)
        for lane in lanes:
            if not lane.task.done():
                lane.stop()
