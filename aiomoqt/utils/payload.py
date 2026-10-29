"""Bench object payloads: every byte follows from the object's location.

A bench object is f"{group}.{object}.{size}|" (a VideoTrack frame adds
".{type}" before the "|") followed by the 0..255 counting pattern, cut to
size. PayloadCheck verifies received objects against that layout.
"""
from typing import Optional, Tuple

_PATTERN = bytes(range(256)) * 1024


def counted(n: int) -> bytes:
    """The first `n` bytes of the counting pattern."""
    global _PATTERN
    while len(_PATTERN) < n:
        _PATTERN += _PATTERN
    return _PATTERN[:n]


class PayloadCheck:
    """Counts received objects whose payload breaks the bench layout,
    checking every `every`-th object."""

    def __init__(self, every: int = 1):
        self.every = max(1, every)
        self.seen = 0
        self.checked = 0
        self.bad = 0
        self.first_bad: Optional[Tuple[int, int, bytes]] = None
        self._tails = {}

    def __call__(self, group_id: int, object_id: int, payload) -> bool:
        self.seen += 1
        if self.seen % self.every:
            return True
        self.checked += 1
        if self.matches(group_id, object_id, payload):
            return True
        self.bad += 1
        if self.first_bad is None:
            self.first_bad = (group_id, object_id, bytes(payload[:48]))
        return False

    def matches(self, group_id: int, object_id: int, payload) -> bool:
        if not isinstance(payload, bytes):
            payload = bytes(payload)
        n = len(payload)
        head = b"%d.%d.%d" % (group_id, object_id, n)
        h = len(head)
        if n <= h + 1:
            return payload == (head + b"|")[:n]
        if not payload.startswith(head):
            return False
        c = payload[h]
        if c == 0x2E:   # ".{type}|"
            if n < h + 3 or payload[h + 2] != 0x7C:
                return False
            h += 2
        elif c != 0x7C:
            return False
        k = n - h - 1
        tail = self._tails.get(k)
        if tail is None:
            if len(self._tails) >= 64:
                self._tails.clear()
            tail = self._tails[k] = counted(k)
        return payload.endswith(tail)
