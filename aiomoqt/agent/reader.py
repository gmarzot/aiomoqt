"""Bounded reads over a live subscription or a fetch.

The repo's existing tools are duration-bounded: they run for N seconds and
report what arrived. An agent needs the opposite — "give me N objects, or
one group, then return" — because a turn has to end.

Objects arrive on the event loop inside the parse path, so ingest here is
O(1), never blocks and never raises: a consumer fault must not surface as
a protocol error. Everything costly happens on the reading side, payload
decoding included.

A subscription is done once its PUBLISH_DONE has arrived and every stream
it counts has ended, since objects can follow PUBLISH_DONE (§10.11), or
when the session's PUBLISH_DONE_GRACE_S runs out first. A fetch is done
when its stream ends.

Staleness is measured, not assumed. `age_ms` comes from the object's own
LOC timestamp against arrival; it is never inferred from a delivery
timeout, because no draft requires a relay to honour one.
"""
from __future__ import annotations

import asyncio
import json as _json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Union

from ..utils.stats import send_time_us
from .errors import AgentError, DecodeError
from .spec import FetchSpec, SubscribeSpec


class ReadTimeout(AgentError):
    """A bounded read did not fill before its deadline."""


@dataclass(frozen=True)
class Obj:
    """One received object, detached from transport state.

    A copy by value: the protocol layer reuses its own message object
    across a stream, so nothing here refers back into it.
    """
    track: str
    group: int
    object_id: int
    subgroup: Optional[int]
    payload: bytes
    size: int
    recv_us: int
    sent_us: Optional[int] = None
    decode: str = "bytes"

    @property
    def age_ms(self) -> Optional[float]:
        """Sender-to-receiver age, or None when the object carries no
        timestamp. None means unknown, never zero."""
        if self.sent_us is None:
            return None
        return (self.recv_us - self.sent_us) / 1000.0

    @property
    def value(self) -> Any:
        """The payload as `decode` asks: bytes, str (UTF-8) or parsed
        JSON. Decoded on each access; raises DecodeError when the payload
        does not decode."""
        if self.decode == "bytes":
            return self.payload
        try:
            if self.decode == "text":
                return self.payload.decode("utf-8")
            return _json.loads(self.payload)
        except ValueError as exc:
            raise DecodeError(
                f"{self.track} {self.group}/{self.object_id}: payload is "
                f"not {self.decode}: {exc}") from exc

    def text(self, encoding: str = "utf-8") -> str:
        return self.payload.decode(encoding)

    def json(self) -> Any:
        return _json.loads(self.payload)


@dataclass
class ReadStats:
    """Counters for one subscription. `dropped` is our own shedding."""
    received: int = 0
    dropped: int = 0
    bytes: int = 0
    groups: int = 0
    errors: int = 0
    last_group: Optional[int] = None
    first_recv_us: Optional[int] = None
    last_recv_us: Optional[int] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "received": self.received, "dropped": self.dropped,
            "bytes": self.bytes, "groups": self.groups,
            "errors": self.errors, "last_group": self.last_group,
        }


class Reader:
    """Bounded reads over one subscription's or one fetch's objects.

    Held by an AgentSession, which owns the connection the request runs
    on. `grace_s` bounds the wait for streams after PUBLISH_DONE.
    """

    def __init__(self, spec: Union[SubscribeSpec, FetchSpec], name: str, *,
                 grace_s: float = 5.0):
        self.spec = spec
        self.name = name
        self.stats = ReadStats()
        self.alias: Optional[int] = None
        self.request_id: Optional[int] = None
        # Status code, reason and Stream Count of the PUBLISH_DONE.
        self.publish_done: Optional[Dict[str, Any]] = None
        self._decode = spec.decode
        # A fetch is finite, so its ring is unbounded.
        self._limit = spec.buffer if isinstance(spec, SubscribeSpec) else None
        self._drop_new = (isinstance(spec, SubscribeSpec)
                          and spec.on_full == "drop_new")
        self._grace_s = grace_s
        self._grace: Optional[asyncio.TimerHandle] = None
        self._streams_ended = 0
        self._streams_expected: Optional[int] = None
        self._ring: Deque[Obj] = deque()
        self._arrival = asyncio.Event()
        self._closed = False
        self._done = False
        self._ended: Optional[str] = None

    # -- ingest: event-loop side, O(1), never raises -----------------
    # The guards cover admission as well as construction: an exception
    # escaping here is reported by the protocol layer as a parse error
    # and rejects the stream, so a consumer fault would present as a
    # peer protocol violation.

    def on_object(self, msg, size, recv_time_us, group_id=None,
                  subgroup_id=None) -> None:
        """Object-handler callback for a subscription. Runs inside the
        parse path."""
        try:
            self._ingest(msg, size, recv_time_us, group_id, subgroup_id)
        except Exception:
            self.stats.errors += 1

    def on_fetch_object(self, msg, size, recv_time_us,
                        request_id=None) -> None:
        """Fetch-object callback; the object carries its own Location."""
        try:
            subgroup = (None if getattr(msg, "datagram", False)
                        else msg.subgroup_id)
            self._ingest(msg, size, recv_time_us, msg.group_id, subgroup)
        except Exception:
            self.stats.errors += 1

    def _ingest(self, msg, size, recv_time_us, group_id,
                subgroup_id) -> None:
        if getattr(msg, "status", 0):
            return  # END_OF_GROUP, END_OF_TRACK: no payload to read
        self._admit(Obj(
            track=self.name,
            group=group_id if group_id is not None else -1,
            object_id=getattr(msg, "object_id", -1),
            subgroup=subgroup_id,
            payload=getattr(msg, "payload", b"") or b"",
            size=size,
            recv_us=recv_time_us,
            sent_us=send_time_us(getattr(msg, "extensions", None)),
            decode=self._decode,
        ))

    def _admit(self, obj: Obj) -> None:
        st = self.stats
        if self._limit is not None and len(self._ring) >= self._limit:
            if self._drop_new:
                st.dropped += 1
                return
            self._ring.popleft()
            st.dropped += 1
        if obj.group != st.last_group:
            st.groups += 1
            st.last_group = obj.group
        st.received += 1
        st.bytes += obj.size
        if st.first_recv_us is None:
            st.first_recv_us = obj.recv_us
        st.last_recv_us = obj.recv_us
        self._ring.append(obj)
        self._arrival.set()

    # -- completion ----------------------------------------------------

    def on_publish_done(self, msg) -> None:
        """PUBLISH_DONE handler: done once the streams it counts have
        ended, else when the grace period runs out."""
        count = getattr(msg, "stream_count", None)
        self.publish_done = {"status_code": getattr(msg, "status_code", None),
                             "reason": getattr(msg, "reason", None),
                             "stream_count": count}
        self._streams_expected = count
        if count is not None and self._streams_ended >= count:
            self.mark_done("complete")
        elif not (self._done or self._closed) and self._grace is None:
            self._grace = asyncio.get_running_loop().call_later(
                self._grace_s, self.mark_done, "grace")

    def on_stream_end(self, group_id, subgroup_id, clean=True,
                      reset_code=0) -> None:
        """Stream-end handler for the subscription's track alias."""
        self._streams_ended += 1
        expected = self._streams_expected
        if expected is not None and self._streams_ended >= expected:
            self.mark_done("complete")

    def mark_done(self, how: str = "complete") -> None:
        """End the read side; reads return what is buffered instead of
        waiting. `how` becomes `ended` unless the reader was closed."""
        if self._done:
            return
        self._done = True
        if self._ended is None:
            self._ended = how
        self._cancel_grace()
        self._arrival.set()

    def _cancel_grace(self) -> None:
        if self._grace is not None:
            self._grace.cancel()
            self._grace = None

    # -- bounded reads: caller side ----------------------------------

    def drain(self) -> List[Obj]:
        """Everything buffered right now. Never waits."""
        out = list(self._ring)
        self._ring.clear()
        return out

    async def read(self, n: int = 1, *, timeout: Optional[float] = None,
                   partial: bool = True) -> List[Obj]:
        """Up to `n` objects, waiting until they arrive or the deadline.

        `partial` returns what arrived when the deadline passes; set it
        false to raise ReadTimeout instead of returning a short read.
        """
        if n < 1:
            raise AgentError(f"read: n must be >= 1, got {n}")
        deadline = self._deadline(timeout)
        out: List[Obj] = []
        while len(out) < n:
            while self._ring and len(out) < n:
                out.append(self._ring.popleft())
            if len(out) == n or self._done or self._closed:
                break
            if not await self._wait(deadline):
                if not partial:
                    raise ReadTimeout(
                        f"read({n}) on {self.name}: got {len(out)} before "
                        f"the deadline")
                break
        return out

    async def read_group(self, *,
                         timeout: Optional[float] = None) -> List[Obj]:
        """One whole group: objects until the group id changes.

        A group is MoQT's self-contained unit, so this is the natural
        turn-sized read when object counts are not known in advance.
        """
        deadline = self._deadline(timeout)
        out: List[Obj] = []
        group: Optional[int] = None
        while True:
            while self._ring:
                if group is not None and self._ring[0].group != group:
                    return out
                obj = self._ring.popleft()
                group = obj.group if group is None else group
                out.append(obj)
            if self._done or self._closed:
                return out
            if not await self._wait(deadline):
                return out

    def close(self) -> None:
        self._closed = True
        self._cancel_grace()
        if self._ended is None:
            self._ended = "closed"
        self._arrival.set()

    @property
    def done(self) -> bool:
        return self._done

    @property
    def ended(self) -> Optional[str]:
        """How the read side ended. None while live; "complete" once
        every stream PUBLISH_DONE counts has ended, or the fetch stream
        finished; "grace" when PUBLISH_DONE_GRACE_S ran out first, so
        objects may be missing; "failed" when the fetch stream was reset
        or timed out; "closed" when closed here first."""
        return self._ended

    @property
    def buffered(self) -> int:
        return len(self._ring)

    def _deadline(self, timeout: Optional[float]) -> float:
        window = self.spec.timeout_s if timeout is None else timeout
        return time.monotonic() + window

    async def _wait(self, deadline: float) -> bool:
        """Wait for an arrival. False once the deadline has passed."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        self._arrival.clear()
        if self._ring:
            return True
        try:
            await asyncio.wait_for(self._arrival.wait(), remaining)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True
