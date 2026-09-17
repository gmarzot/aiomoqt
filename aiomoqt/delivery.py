"""Object delivery: how a numbered object sequence reaches peers.

Packaging decides what an object is (group, id, properties); delivery
decides the stream it rides and what the peer is owed at the end.
`SubgroupDelivery` serves one session, `FanoutDelivery` several. The
caller numbers objects, so every peer sees the same group and object
ids.
"""
from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Dict, Optional

from .messages import SubgroupHeader
from .messages.data import ObjectDatagram
from .utils.logger import get_logger

logger = get_logger(__name__)

# Lane queue sentinels.
_END_GROUP = object()
_STOP = object()

# Objects a lane may fall behind by before it is dropped to the next
# group. 256 is ~8 s of 30 fps video: past that the peer is not slow,
# it is gone.
LANE_QUEUE_DEFAULT = 256


class StreamMapping(Enum):
    PER_GROUP = "per_group"    # loc-02 §4.2: one uni stream per group
    PER_OBJECT = "per_object"  # msf-01 §6: one uni stream per object
    DATAGRAM = "datagram"      # loc-02 §4.1: one datagram per object


class SubgroupDelivery:
    """One session's copy of an object sequence.

    PER_GROUP holds a stream open for the length of a group, PER_OBJECT
    opens one per object, DATAGRAM uses no stream at all. `largest` and
    `stream_count` are what this peer has been sent — the numbers
    SUBSCRIBE_OK and PUBLISH_DONE owe it.
    """

    def __init__(self, session, track_alias: int, *, priority: int = 128,
                 mapping: StreamMapping = StreamMapping.PER_GROUP):
        self.session = session
        self.track_alias = track_alias
        self.priority = priority
        self.mapping = mapping
        self.stream_count = 0
        self.largest: Optional[tuple] = None
        self.objects_sent = 0
        self.bytes_sent = 0
        self._stream_id: Optional[int] = None
        self._header: Optional[SubgroupHeader] = None

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
        buf = self._header.end_group(object_id=self._header.next_object_id)
        self.session.stream_write(self._stream_id, buf.data, end_stream=True)
        self._stream_id = None
        self._header = None

    async def write(self, group_id: int, object_id: int, payload: bytes, *,
                    extensions: Optional[Dict[int, Any]] = None,
                    group_start: bool = False) -> None:
        """Place one object. `group_start` rotates the PER_GROUP stream.
        A callable `extensions` is resolved against this session."""
        if callable(extensions):
            extensions = extensions(self.session)
        if self.mapping is StreamMapping.DATAGRAM:
            dgram = ObjectDatagram(
                track_alias=self.track_alias, group_id=group_id,
                object_id=object_id, publisher_priority=self.priority,
                extensions=extensions, payload=payload)
            await self.session.dgram_write_drain(
                dgram.serialize(prof=self._prof))
        elif self.mapping is StreamMapping.PER_OBJECT:
            # One stream per object ⇒ one subgroup per object (a subgroup
            # owns exactly one stream); the object keeps its decode-order
            # id via subgroup_id.
            sid = await self.session.open_uni_stream()
            self.stream_count += 1
            hdr = SubgroupHeader(
                track_alias=self.track_alias, group_id=group_id,
                subgroup_id=object_id, publisher_priority=self.priority,
                extensions_present=True, prof=self._prof)
            self.session.stream_write(sid, hdr.serialize().data)
            buf = hdr.next_object(payload=payload, extensions=extensions,
                                  object_id=object_id)
            await self.session.stream_write_drain(sid, buf.data)
            self.session.stream_write(sid, b"", end_stream=True)
        else:  # PER_GROUP
            if group_start:
                self.end_group()
                self._stream_id = await self.session.open_uni_stream()
                self.stream_count += 1
                self._header = SubgroupHeader(
                    track_alias=self.track_alias, group_id=group_id,
                    subgroup_id=0, publisher_priority=self.priority,
                    extensions_present=True, prof=self._prof)
                self.session.stream_write(self._stream_id,
                                          self._header.serialize().data)
            buf = self._header.next_object(payload=payload,
                                           extensions=extensions,
                                           object_id=object_id)
            await self.session.stream_write_drain(self._stream_id, buf.data)

        self._note_largest(group_id, object_id)
        self.objects_sent += 1
        self.bytes_sent += len(payload)

    def abort(self) -> None:
        """Give up now, closing the open group if the session still
        takes writes."""
        self.end_group()

    async def close(self) -> None:
        """Finish cleanly: close the open group."""
        self.end_group()


class _Lane:
    """One delivery behind its own queue and task.

    The queue is what decouples peers: the source hands an object to
    every lane without waiting, so a peer parked on transport
    backpressure holds up only itself.
    """

    def __init__(self, delivery: SubgroupDelivery, queue_size: int,
                 gate=None):
        self.delivery = delivery
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.shed = 0
        self.need_group = False   # skipping until the next group starts
        # Returns False while this peer's Forward State is 0 (§5.1).
        self.gate = gate
        self._truncated = False
        self.task = asyncio.ensure_future(self._drain())

    def _close_truncated(self) -> None:
        """FIN the group this lane fell out of, so the subscriber sees
        an end and not a hole."""
        if not self._truncated:
            self.delivery.end_group()
            self._truncated = True

    @property
    def session(self):
        return self.delivery.session

    def _flush_backlog(self) -> int:
        """Drop what is queued, keeping the stop sentinel. A lane that
        has fallen behind is skipping to the next group, so its backlog
        is already spent — and dropping it leaves room for the object
        that lets the lane rejoin."""
        dropped = 0
        keep = []
        while True:
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _STOP:
                keep.append(item)
            else:
                dropped += 1
        for item in keep:
            self.queue.put_nowait(item)
        return dropped

    def offer(self, item) -> None:
        """Queue an object, or shed it. A lane that sheds anything skips
        to the next group: half a group is worse to a subscriber than
        none of it."""
        try:
            self.queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        self.need_group = True
        self.shed += self._flush_backlog()
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.shed += 1

    async def _drain(self) -> None:
        while True:
            item = await self.queue.get()
            if item is _STOP:
                return
            if item is _END_GROUP:
                self.delivery.end_group()
                self._truncated = False
                continue
            group_id, object_id, payload, extensions, group_start = item
            if self.gate is not None and not self.gate():
                # Forward State 0: send nothing, end the open group, and
                # resume only at a fresh group.
                self._close_truncated()
                self.need_group = True
                continue
            if self.need_group:
                if not group_start:
                    self._close_truncated()
                    self.shed += 1
                    continue
                self.need_group = False
                self._truncated = False
            await self.delivery.write(group_id, object_id, payload,
                                      extensions=extensions,
                                      group_start=group_start)


class FanoutDelivery:
    """Several deliveries of one object sequence, behind the
    SubgroupDelivery interface.

    The source numbers objects once and writes them here, so every peer
    sees the same group and object ids — a subscriber that moves between
    two relays fed by this publisher finds the same content under the
    same numbers. A peer that cannot keep up loses whole groups rather
    than slowing the others down.
    """

    def __init__(self, deliveries=(), *,
                 queue_size: int = LANE_QUEUE_DEFAULT):
        self.queue_size = queue_size
        self.lanes: list = []
        for delivery in deliveries:
            self.add(delivery, joining=False)

    def add(self, delivery: SubgroupDelivery, gate=None,
            joining: bool = True) -> _Lane:
        """Attach another peer. `joining` holds it to the next group
        boundary, which is where a mid-stream arrival can start."""
        lane = _Lane(delivery, self.queue_size, gate)
        lane.need_group = joining
        self.lanes.append(lane)
        return lane

    def drop_session(self, session) -> bool:
        """Detach a session's lanes, closing any open group. True if it
        had one."""
        mine = [ln for ln in self.lanes if ln.session is session]
        for lane in mine:
            lane.task.cancel()
            self.lanes.remove(lane)
            try:
                lane.delivery.end_group()
            except Exception:
                logger.debug("fanout: lane end_group failed at drop",
                             exc_info=True)
        return bool(mine)

    @property
    def stream_count(self) -> int:
        return max((ln.delivery.stream_count for ln in self.lanes),
                   default=0)

    @property
    def largest(self) -> Optional[tuple]:
        seen = [ln.delivery.largest for ln in self.lanes
                if ln.delivery.largest is not None]
        return max(seen) if seen else None

    async def write(self, group_id: int, object_id: int, payload: bytes, *,
                    extensions: Optional[Dict[int, Any]] = None,
                    group_start: bool = False) -> None:
        item = (group_id, object_id, payload, extensions, group_start)
        for lane in list(self.lanes):
            lane.offer(item)

    def end_group(self) -> None:
        for lane in list(self.lanes):
            lane.offer(_END_GROUP)

    def abort(self) -> None:
        """Give up on every lane without waiting."""
        for lane in list(self.lanes):
            lane.task.cancel()
            try:
                lane.delivery.end_group()
            except Exception:
                logger.debug("fanout: lane end_group failed at abort",
                             exc_info=True)

    async def close(self, timeout: float = 5.0) -> None:
        """Close the open group on every lane and wait for them to
        flush. A lane that will not drain is abandoned, not waited on."""
        async def _finish(lane: _Lane) -> None:
            await lane.queue.put(_END_GROUP)
            await lane.queue.put(_STOP)
            await lane.task

        await asyncio.gather(
            *(asyncio.wait_for(_finish(ln), timeout) for ln in self.lanes),
            return_exceptions=True)
        for lane in self.lanes:
            if not lane.task.done():
                lane.task.cancel()
