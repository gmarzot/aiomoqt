"""Bounded reads over a live subscription.

The repo's existing tools are duration-bounded: they run for N seconds and
report what arrived. An agent needs the opposite — "give me N objects, or
one group, then return" — because a turn has to end.

Objects arrive on the event loop inside the parse path, so ingest here is
O(1), never blocks and never raises: a consumer fault must not surface as
a protocol error. Everything costly happens on the reading side.

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
from typing import Any, Deque, Dict, List, Optional

from ..utils.stats import send_time_us
from .errors import AgentError
from .spec import SubscribeSpec


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

    @property
    def age_ms(self) -> Optional[float]:
        """Sender-to-receiver age, or None when the object carries no
        timestamp. None means unknown, never zero."""
        if self.sent_us is None:
            return None
        return (self.recv_us - self.sent_us) / 1000.0

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
    """Bounded reads over one subscription's object stream.

    Held by an AgentSession, which owns the connection the subscription
    runs on.
    """

    def __init__(self, spec: SubscribeSpec, name: str):
        self.spec = spec
        self.name = name
        self.stats = ReadStats()
        self._ring: Deque[Obj] = deque()
        self._arrival = asyncio.Event()
        self._closed = False
        self._done = False

    # -- ingest: event-loop side, O(1), never raises -----------------

    def on_object(self, msg, size, recv_time_us, group_id=None,
                  subgroup_id=None) -> None:
        """Object-handler callback. Runs inside the parse path.

        The guard covers admission as well as construction: an exception
        escaping here is reported by the protocol layer as a parse error
        and rejects the stream, so a consumer fault would present as a
        peer protocol violation.
        """
        try:
            self._admit(Obj(
                track=self.name,
                group=group_id if group_id is not None else -1,
                object_id=getattr(msg, "object_id", -1),
                subgroup=subgroup_id,
                payload=getattr(msg, "payload", b"") or b"",
                size=size,
                recv_us=recv_time_us,
                sent_us=send_time_us(getattr(msg, "extensions", None)),
            ))
        except Exception:
            self.stats.errors += 1

    def _admit(self, obj: Obj) -> None:
        st = self.stats
        if len(self._ring) >= self.spec.buffer:
            if self.spec.on_full == "drop_new":
                st.dropped += 1
                return
            # block/error cannot park the parse path; both shed the
            # oldest and are surfaced by stats rather than stalling RX.
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

    def mark_done(self) -> None:
        """The publisher ended the subscription; unblock any waiter."""
        self._done = True
        self._arrival.set()

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
        self._arrival.set()

    @property
    def done(self) -> bool:
        return self._done

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
