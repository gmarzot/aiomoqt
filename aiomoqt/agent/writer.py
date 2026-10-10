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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..delivery import StreamMapping
from ..track import PublishedTrack
from .errors import AgentError
from .spec import DEFAULT_TIMEOUT_S, PublishSpec, check_supported

_MAPPINGS = {
    "per_group": StreamMapping.PER_GROUP,
    "per_object": StreamMapping.PER_OBJECT,
    "datagram": StreamMapping.DATAGRAM,
}

# (group_id, object_id, payload, group_start)
_Item = Tuple[int, int, bytes, bool]

# Announces a namespace on the writer's session; returns once it is.
Announcer = Callable[[str], Awaitable[None]]


class WriteRefused(AgentError):
    """The outbound queue was full and the policy said to refuse."""


@dataclass
class WriteStats:
    """`sent` counts per peer, objects actually written: one reaching two
    peers counts twice, one a peer was not sent (paused, filtered out,
    shed) not at all."""
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
        self._stopping = False

    async def produce(self, out) -> None:
        while not (self._stopping and self._queue.empty()):
            item = await self._queue.get()
            try:
                if item is None:  # close sentinel
                    return
                group_id, object_id, payload, group_start = item
                await out.write(group_id, object_id, payload,
                                group_start=group_start)
            finally:
                self._queue.task_done()

    def _on_sent(self, group_id: int, object_id: int) -> None:
        self._stats.sent += 1

    def stop(self) -> None:
        """produce() returns once the queue is drained. The sentinel wakes
        it on an empty queue; a full one ends on the flag."""
        self._stopping = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def finished(self, timeout: float) -> None:
        """Wait up to `timeout` for production to end, after which every
        peer it served has had PUBLISH_DONE."""
        if self._production is not None:
            await asyncio.wait({self._production}, timeout=timeout)


class Writer:
    """Publishes an agent's objects onto one track.

    Held by an AgentSession, which owns the connection its priority is
    relative to.
    """

    def __init__(self, spec: PublishSpec, track: _PushTrack,
                 queue: asyncio.Queue, stats: WriteStats,
                 announcer: Optional[Announcer] = None):
        self.spec = spec
        self.name = str(spec.track)
        self.stats = stats
        self._track = track
        self._queue = queue
        self._announcer = announcer
        # Held from choosing an object's number until it is queued, so
        # concurrent writes queue in the order they were numbered.
        self._lock = asyncio.Lock()
        self._putter: Optional[asyncio.Future] = None
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
        """Make the track reachable: with `announce`, announce its
        namespace (once per session) and serve SUBSCRIBEs for it;
        otherwise send a bare PUBLISH."""
        if self._live:
            return
        track = self._track
        if not self.spec.announce:
            await track.publish()
        elif self._announcer is None:
            await track.publish(announce_namespace=True, publish_track=False)
        else:
            # Before the announcement: a SUBSCRIBE may overtake its reply.
            track.attach()
            await self._announcer(track.namespace)
        self._live = True

    async def snapshot(self, payload: Any) -> None:
        """Start a new group with `payload` as its first object.

        The group is MoQT's self-contained unit: a subscriber joining
        later starts here, so this is where a turn or a full state goes.
        """
        data = _as_bytes(payload)
        async with self._lock:
            self._check_open()
            self._advance_group()
            await self._put(data, group_start=True)

    async def write(self, payload: Any) -> None:
        """Append to the current group, starting one if none is open."""
        data = _as_bytes(payload)
        async with self._lock:
            self._check_open()
            full = (self.spec.group_size is not None
                    and self._in_group >= self.spec.group_size)
            start = self._group < 0 or self._force_new or full
            if start:
                self._advance_group()
            await self._put(data, group_start=start)

    def end_group(self) -> None:
        """Close the current group; the next write starts a new one."""
        self._force_new = True

    async def flush(self, *, timeout: Optional[float] = None) -> None:
        """Wait until produce() has handed every queued object to the
        track's delivery."""
        deadline = DEFAULT_TIMEOUT_S if timeout is None else timeout
        try:
            await asyncio.wait_for(self._queue.join(), deadline)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise AgentError(
                f"flush({self.name}): {self._queue.qsize()} objects still "
                f"queued after {deadline}s") from exc

    async def close(self, *, timeout: Optional[float] = None) -> None:
        """Stop taking writes. Production hands on what is queued, then
        each peer it served gets PUBLISH_DONE; this waits up to `timeout`
        for that. A write still waiting for room raises AgentError."""
        if self._closed:
            return
        self._closed = True
        if self._putter is not None:
            self._putter.cancel()
        self._track.stop()
        await self._track.finished(
            DEFAULT_TIMEOUT_S if timeout is None else timeout)

    # -- internals ---------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise AgentError(f"writer {self.name} is closed")

    def _advance_group(self) -> None:
        self._group += 1
        self._object = 0
        self._in_group = 0
        self._force_new = False
        self.stats.groups += 1

    async def _put(self, data: bytes, *, group_start: bool) -> None:
        """Number and queue one object. Called with the lock held."""
        item: _Item = (self._group, self._object, data, group_start)
        if self._queue.full() and self.spec.on_full == "block":
            await self._wait_for_room(item)
        elif not self._admit(item):
            return
        self._object += 1
        self._in_group += 1
        self.stats.queued += 1
        self.stats.bytes += len(data)

    async def _wait_for_room(self, item: _Item) -> None:
        """Queue `item` once there is room; close() abandons the wait."""
        put = asyncio.ensure_future(self._queue.put(item))
        self._putter = put
        try:
            await put
        except asyncio.CancelledError:
            if (put.cancelled() and self._closed
                    and not asyncio.current_task().cancelling()):
                raise AgentError(
                    f"writer {self.name} closed while waiting for room"
                ) from None
            raise
        finally:
            self._putter = None

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


def build_writer(session: Any, spec: PublishSpec, *,
                 scheduling: str = "round_robin",
                 announcer: Optional[Announcer] = None,
                 ) -> Tuple[Writer, _PushTrack]:
    """Construct the track and its writer without publishing yet.

    `announcer(namespace)` announces for an `announce` writer; without
    one, start() has the track announce its own namespace.

    A declared publisher priority is mapped to a transport byte once,
    here — constant for the track's life, so it never costs work in the
    send loop. Undeclared leaves streams at the transport default.
    """
    check_supported(spec)
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
        auth_token=None, queue=queue, stats=stats, mapping=mapping)
    if spec.priority.publisher is not None:
        from ..types import to_stream_priority
        track.stream_priority = to_stream_priority(
            spec.priority.publisher, discipline=scheduling)
    return Writer(spec, track, queue, stats, announcer), track


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
