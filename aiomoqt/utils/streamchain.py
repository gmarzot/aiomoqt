"""StreamChain — per-stream byte accumulator backed by a deque of
memoryviews, with cross-boundary parsing and single-level rollback.

Replaces the prior per-stream `re_buf` (a 32 MB pre-allocated Buffer
that grew via `seek(0) + push_bytes(saved_bytes)`). The chain holds
each incoming chunk by reference (no concatenation), advances a
position cursor across chunk boundaries during parsing, and drops
fully-consumed chunks from the front on commit.

API mirrors the subset of qh3._hazmat.Buffer that the MoQT parser
uses (pull_uint_var, pull_uint, pull_bytes, tell, seek, capacity,
push_bytes, data_slice) so the parser code is unchanged.

Save/rollback are single-level: MoQT data-stream parsing is one
message at a time, never nested. save() at the start of a message
attempt, rollback() if MOQTUnderflow fires, commit() after success.
"""

from collections import deque


class StreamChain:
    __slots__ = (
        "_chunks",
        "_chunk_idx",
        "_chunk_off",
        "_pos",
        "_total",
        "_save_chunk_idx",
        "_save_chunk_off",
        "_save_pos",
    )

    def __init__(self) -> None:
        self._chunks: deque = deque()
        self._chunk_idx: int = 0
        self._chunk_off: int = 0
        self._pos: int = 0     # bytes consumed since chain anchor
        self._total: int = 0   # bytes pushed since chain anchor
        self._save_chunk_idx: int = 0
        self._save_chunk_off: int = 0
        self._save_pos: int = 0

    # --- ingest ---------------------------------------------------------

    def extend(self, data) -> None:
        """Append a chunk to the end. Accepts bytes, bytearray, or
        memoryview. Empty chunks are dropped."""
        if not isinstance(data, memoryview):
            if not data:
                return
            data = memoryview(data)
        n = len(data)
        if n == 0:
            return
        self._chunks.append(data)
        self._total += n

    def push_bytes(self, data) -> None:
        """qh3.Buffer-compatible alias for extend()."""
        self.extend(data)

    # --- query ----------------------------------------------------------

    @property
    def capacity(self) -> int:
        """Total bytes from the chain anchor to the end."""
        return self._total

    def __len__(self) -> int:
        return self._total - self._pos

    def tell(self) -> int:
        """Bytes consumed since the chain anchor (the start of the
        current message frame, reset by commit())."""
        return self._pos

    def seek(self, pos: int) -> None:
        if pos < 0 or pos > self._total:
            raise ValueError(
                f"StreamChain.seek out of range: {pos} not in [0, {self._total}]"
            )
        running = 0
        for i, ch in enumerate(self._chunks):
            chlen = len(ch)
            if running + chlen > pos:
                self._chunk_idx = i
                self._chunk_off = pos - running
                self._pos = pos
                return
            running += chlen
        # Position is exactly at end (no current chunk).
        self._chunk_idx = len(self._chunks)
        self._chunk_off = 0
        self._pos = pos

    def data_slice(self, start: int, end: int) -> bytes:
        """Return bytes [start:end) from the chain. Used for logging
        (hex dumps in error paths) and reset paths in protocol.py.
        Does a copy when the range crosses chunk boundaries."""
        if start < 0 or end > self._total or start > end:
            raise ValueError(
                f"StreamChain.data_slice out of range: [{start}, {end}) "
                f"in [0, {self._total})"
            )
        if start == end:
            return b""
        # Walk to start
        running = 0
        idx = 0
        off = 0
        for i, ch in enumerate(self._chunks):
            if running + len(ch) > start:
                idx = i
                off = start - running
                break
            running += len(ch)
        # Fast path: range is in one chunk
        first_chunk_len = len(self._chunks[idx]) - off
        n = end - start
        if n <= first_chunk_len:
            return bytes(self._chunks[idx][off:off + n])
        # Slow path: gather across boundaries
        out = bytearray(n)
        out_pos = 0
        while out_pos < n:
            ch = self._chunks[idx]
            avail = len(ch) - off
            take = min(n - out_pos, avail)
            out[out_pos:out_pos + take] = ch[off:off + take]
            out_pos += take
            off = 0
            idx += 1
        return bytes(out)

    # --- pull ----------------------------------------------------------

    def pull_bytes(self, n: int) -> bytes:
        """Pull n bytes; returns bytes (copy when cross-boundary)."""
        if n < 0:
            raise ValueError(f"StreamChain.pull_bytes negative: {n}")
        if self._pos + n > self._total:
            from ..messages.base import MOQTUnderflow
            raise MOQTUnderflow(self._pos, self._pos + n)
        if n == 0:
            return b""
        # Fast path: fits in current chunk
        ch = self._chunks[self._chunk_idx]
        avail = len(ch) - self._chunk_off
        if n <= avail:
            out = bytes(ch[self._chunk_off:self._chunk_off + n])
            self._chunk_off += n
            self._pos += n
            if self._chunk_off >= len(ch):
                self._chunk_idx += 1
                self._chunk_off = 0
            return out
        # Slow path: cross-boundary
        out = bytearray(n)
        out_pos = 0
        while out_pos < n:
            ch = self._chunks[self._chunk_idx]
            avail = len(ch) - self._chunk_off
            take = min(n - out_pos, avail)
            out[out_pos:out_pos + take] = ch[self._chunk_off:self._chunk_off + take]
            out_pos += take
            self._chunk_off += take
            self._pos += take
            if self._chunk_off >= len(ch):
                self._chunk_idx += 1
                self._chunk_off = 0
        return bytes(out)

    def pull_uint(self, n: int) -> int:
        """Fixed-width big-endian unsigned int."""
        return int.from_bytes(self.pull_bytes(n), "big")

    def pull_uint8(self) -> int:
        return self.pull_uint(1)

    def pull_uint16(self) -> int:
        return self.pull_uint(2)

    def pull_uint32(self) -> int:
        return self.pull_uint(4)

    def pull_uint64(self) -> int:
        return self.pull_uint(8)

    def pull_uint_var(self) -> int:
        """QUIC variable-length integer (RFC 9000 §16)."""
        if self._pos >= self._total:
            from ..messages.base import MOQTUnderflow
            raise MOQTUnderflow(self._pos, self._pos + 1)
        # Peek first byte
        ch = self._chunks[self._chunk_idx]
        first = ch[self._chunk_off]
        prefix = first >> 6
        nbytes = 1 << prefix  # 1, 2, 4, or 8
        if self._pos + nbytes > self._total:
            from ..messages.base import MOQTUnderflow
            raise MOQTUnderflow(self._pos, self._pos + nbytes)
        if nbytes == 1:
            self._chunk_off += 1
            self._pos += 1
            if self._chunk_off >= len(ch):
                self._chunk_idx += 1
                self._chunk_off = 0
            return first & 0x3F
        raw = self.pull_bytes(nbytes)
        # Mask top 2 bits of first byte; concatenate big-endian.
        result = raw[0] & 0x3F
        for b in raw[1:]:
            result = (result << 8) | b
        return result

    # --- save/commit/rollback -----------------------------------------

    def save(self) -> None:
        """Snapshot current position. Single-level: each save replaces
        the previous anchor."""
        self._save_chunk_idx = self._chunk_idx
        self._save_chunk_off = self._chunk_off
        self._save_pos = self._pos

    def rollback(self) -> None:
        """Restore position to the last save() anchor."""
        self._chunk_idx = self._save_chunk_idx
        self._chunk_off = self._save_chunk_off
        self._pos = self._save_pos

    def commit(self) -> None:
        """Drop fully-consumed chunks from the front; reset tell to 0.
        Sets a new save anchor at the new origin."""
        # Drop chunks fully behind the cursor.
        while self._chunk_idx > 0:
            dropped = self._chunks.popleft()
            self._total -= len(dropped)
            self._pos -= len(dropped)
            self._chunk_idx -= 1
        # Trim the leading offset of the current first chunk.
        if self._chunk_off > 0 and self._chunks:
            front = self._chunks[0]
            self._chunks[0] = front[self._chunk_off:]
            self._total -= self._chunk_off
            self._pos -= self._chunk_off
            self._chunk_off = 0
        # Now _pos = 0 should hold (we consumed exactly to the cursor).
        # Adjust the absolute origin: since we just made cursor the new
        # anchor, _pos resets to 0 and _total shrinks by the consumed
        # amount. The cursor is at the start of chunks[0] (or at end if
        # no chunks remain).
        # Drop the leading empty chunk if it exists.
        while self._chunks and len(self._chunks[0]) == 0:
            self._chunks.popleft()
        # Reset save anchor.
        self._save_chunk_idx = 0
        self._save_chunk_off = 0
        self._save_pos = 0
