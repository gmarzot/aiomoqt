"""Push-style publication for an agent.

PublishedTrack expects content to be generated from inside produce(); an
agent has the opposite shape — it finishes a turn and wants to emit what
it decided. This wraps the pull model in a queue: produce() drains, the
caller pushes.

produce() is deliberately the path used, not generate(). It numbers each
object once into a delivery the track spreads across every peer, so
fan-out stays where it belongs and nothing here knows a peer exists.

Groups are the unit an agent cares about — one turn, one snapshot, one
decision — so snapshot() starts a group and write() adds to it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..delivery import StreamMapping
from ..track import PublishedTrack
from .errors import AgentError
from .spec import PublishSpec

_MAPPINGS = {
    "per_group": StreamMapping.PER_GROUP,
    "per_object": StreamMapping.PER_OBJECT,
    "datagram": StreamMapping.DATAGRAM,
}

# (group_id, object_id, payload, group_start)
_Item = Tuple[int, int, bytes, bool]


class WriteRefused(AgentError):
    """The outbound queue was full and the policy said to refuse."""


@dataclass
class WriteStats:
    queued: int = 0
    sent: int = 0
    dropped: int = 0
    bytes: int = 0
    groups: int = 0

    def snapshot(self) -> Dict[str, Any]:
        return {"queued": self.queued, "sent": self.sent,
                "dropped": self.dropped, "bytes": self.bytes,
                "groups": self.groups}


class _PushTrack(PublishedTrack):
    """A PublishedTrack whose content arrives from outside."""

    def __init__(self, *args, queue: asyncio.Queue, stats: WriteStats,
                 mapping: StreamMapping, **kwargs):
        super().__init__(*args, **kwargs)
        self._queue = queue
        self._stats = stats
        self.mapping = mapping

    async def produce(self, out) -> None:
        while True:
            item = await self._queue.get()
            if item is None:  # close sentinel
                return
            group_id, object_id, payload, group_start = item
            await out.write(group_id, object_id, payload,
                            group_start=group_start)
            self._stats.sent += 1


class Writer:
    """Publishes an agent's objects onto one track.

    Held by an AgentSession, which owns the connection its priority is
    relative to.
    """

    def __init__(self, spec: PublishSpec, track: _PushTrack,
                 queue: asyncio.Queue, stats: WriteStats):
        self.spec = spec
        self.name = str(spec.track)
        self.stats = stats
        self._track = track
        self._queue = queue
        self._group = -1
        self._object = 0
        self._in_group = 0
        self._force_new = False
        self._live = False
        self._closed = False

    @property
    def group(self) -> int:
        """Current group id, or -1 before the first snapshot."""
        return self._group

    @property
    def live(self) -> bool:
        return self._live

    async def start(self) -> None:
        """Announce and publish the track, so objects can reach peers."""
        if self._live:
            return
        await self._track.publish()
        self._live = True

    async def snapshot(self, payload: Any) -> None:
        """Start a new group with `payload` as its first object.

        The group is MoQT's self-contained unit: a subscriber joining
        later starts here, so this is where a turn or a full state goes.
        """
        self._advance_group()
        await self._put(payload, group_start=True)

    async def write(self, payload: Any) -> None:
        """Append to the current group, starting one if none is open."""
        full = (self.spec.group_size is not None
                and self._in_group >= self.spec.group_size)
        if self._group < 0 or self._force_new or full:
            self._advance_group()
            await self._put(payload, group_start=True)
            return
        await self._put(payload, group_start=False)

    def end_group(self) -> None:
        """Close the current group; the next write starts a new one."""
        self._force_new = True

    async def flush(self, *, timeout: Optional[float] = None) -> None:
        """Wait until every queued object has been written to the wire."""
        deadline = timeout if timeout is not None else self.spec.timeout_s
        try:
            await asyncio.wait_for(self._queue.join(), deadline)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise AgentError(
                f"flush({self.name}): {self._queue.qsize()} objects still "
                f"queued after {deadline}s") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    # -- internals ---------------------------------------------------

    def _advance_group(self) -> None:
        self._group += 1
        self._object = 0
        self._in_group = 0
        self._force_new = False
        self.stats.groups += 1

    async def _put(self, payload: Any, *, group_start: bool) -> None:
        if self._closed:
            raise AgentError(f"writer {self.name} is closed")
        data = _as_bytes(payload)
        item: _Item = (self._group, self._object, data, group_start)
        if not self._admit(item):
            return
        self._object += 1
        self._in_group += 1
        self.stats.queued += 1
        self.stats.bytes += len(data)

    def _admit(self, item: _Item) -> bool:
        """Apply the spec's on_full policy. True if the item was queued.

        Unlike the reader's ingest this runs on the caller's side, not
        inside the parse path, so blocking is a real option here.
        """
        if self._queue.maxsize and self._queue.full():
            policy = self.spec.on_full
            if policy == "drop_new":
                self.stats.dropped += 1
                return False
            if policy == "drop_oldest":
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self.stats.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            elif policy == "error":
                raise WriteRefused(
                    f"writer {self.name}: queue full ({self._queue.maxsize})")
        self._queue.put_nowait(item)
        return True


def _as_bytes(payload: Any) -> bytes:
    """bytes as-is, str as UTF-8, anything else as compact JSON."""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode()
    import json
    return json.dumps(payload, separators=(",", ":")).encode()


def build_writer(session: Any, spec: PublishSpec) -> Tuple[Writer, _PushTrack]:
    """Construct the track and its writer without publishing yet."""
    mapping = _MAPPINGS.get(spec.mapping)
    if mapping is None:
        raise AgentError(f"publish mapping {spec.mapping!r} has no wire form")
    if spec.track.name is None:
        raise AgentError("writer: PublishSpec.track.name is required")
    stats = WriteStats()
    queue: asyncio.Queue = asyncio.Queue(maxsize=spec.buffer)
    track = _PushTrack(
        session, spec.track.namespace, spec.track.name,
        priority=(spec.priority.publisher
                  if spec.priority.publisher is not None else 128),
        queue=queue, stats=stats, mapping=mapping)
    return Writer(spec, track, queue, stats), track


def writers_by_priority(writers: List[Writer]) -> List[Tuple[str, int]]:
    """Declared publisher priority per track, most urgent first.

    Lower is more urgent (§7.1). This is the relationship a connection is
    asked to schedule; whether the transport applies it is reported by
    AgentSession.priority_plan().
    """
    out = [(w.name,
            w.spec.priority.publisher if w.spec.priority.publisher is not None
            else 128)
           for w in writers]
    return sorted(out, key=lambda pair: pair[1])
