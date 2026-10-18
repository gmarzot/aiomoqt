"""The relay's per-track forward loop must survive an upstream subgroup
END and keep forwarding later groups (a dead loop drops every group
after the first, invisibly to a subscriber that only checks group 0)."""
import asyncio

import pytest

from aiomoqt.context import profile_for
from aiomoqt.tools.moq_interop_relay import _RelayedTrack


class _Downstream:
    def __init__(self):
        self._profile = profile_for(18)
        self._next = 3
        self.writes = []            # (stream_id, nbytes, fin)
        self.dones = []             # (request_id, PUBLISH_DONE status)

    async def open_uni_stream(self):
        sid = self._next
        self._next += 4
        return sid

    def stream_write(self, sid, data, end_stream=False):
        self.writes.append((sid, len(data), end_stream))

    async def stream_write_drain(self, sid, data):
        self.writes.append((sid, len(data), False))

    def stream_reset(self, sid, code):
        self.writes.append((sid, "reset", int(code)))

    def subscribe_done(self, request_id, status_code=0, stream_count=0,
                       reason=""):
        self.dones.append((request_id, int(status_code)))


@pytest.mark.asyncio
async def test_upstream_reset_becomes_a_downstream_reset_not_a_fin():
    # §11.4.2: end-of-group may be inferred from a FIN, never from a reset,
    # so an upstream reset must not be relayed as a clean stream end.
    track = _RelayedTrack(("ns", "t"))
    down = _Downstream()
    track.add_downstream(down, 7, 1)
    loop_task = track.task

    track.on_object(_Obj(group_id=0, object_id=0), 0, 0, 0, 0)
    track.on_stream_end(0, 0, clean=False, reset_code=2)   # DELIVERY_TIMEOUT
    for _ in range(20):
        await asyncio.sleep(0)
    loop_task.cancel()

    assert (3, "reset", 2) in down.writes
    assert not any(fin is True for _sid, _n, fin in down.writes)


@pytest.mark.asyncio
async def test_forward_loop_survives_subgroup_end():
    track = _RelayedTrack(("ns", "t"))
    down = _Downstream()
    track.add_downstream(down, 7, 1)
    loop_task = track.task

    track.on_object(_Obj(group_id=0, object_id=0), 0, 0, 0, 0)
    track.on_stream_end(0, 0)
    track.on_object(_Obj(group_id=1, object_id=0), 0, 0, 1, 0)
    for _ in range(20):
        await asyncio.sleep(0)
    loop_task.cancel()

    assert not loop_task.done() or loop_task.cancelled()
    streams = {sid for sid, _n, _fin in down.writes}
    assert len(streams) == 2                       # group 0 and group 1
    assert any(fin for _sid, _n, fin in down.writes)  # group 0 was FINed


class _Obj:
    def __init__(self, group_id, object_id):
        self.group_id = group_id
        self.object_id = object_id
        self.payload = b"x" * 8
        self.extensions = None
        self.publisher_priority = 128
        self.status = None
        self.stream_flags = None


@pytest.mark.asyncio
async def test_malformed_upstream_ends_downstream_and_drops_the_fanout():
    # §2.4.2: a relay that detects a malformed track terminates every
    # downstream subscription with PUBLISH_DONE MALFORMED_TRACK, resets
    # their streams, and serves nothing further from that track.
    from aiomoqt.tools.moq_interop_relay import _tracks
    from aiomoqt.types import StreamResetCode, SubscribeDoneCode

    key = ("mal-ns", "t")
    track = _RelayedTrack(key)
    down = _Downstream()
    track.add_downstream(down, 7, 1)
    _tracks[key] = track
    loop_task = track.task
    try:
        track.on_object(_Obj(group_id=0, object_id=0), 0, 0, 0, 0)
        for _ in range(20):
            await asyncio.sleep(0)
        track.on_stream_end(0, 0, clean=False,
                            reset_code=StreamResetCode.MALFORMED_TRACK)

        assert down.dones == [(1, int(SubscribeDoneCode.MALFORMED_TRACK))]
        assert (3, "reset", int(StreamResetCode.MALFORMED_TRACK)) in down.writes
        assert not any(fin is True for _sid, _n, fin in down.writes)
        assert key not in _tracks                  # a later SUBSCRIBE starts clean
    finally:
        loop_task.cancel()
        _tracks.pop(key, None)


@pytest.mark.parametrize("draft", [14, 16, 18, 22])
@pytest.mark.parametrize("exts", [None, {0x10: 7}, {0x10: 7, 0x0B: b"ab"}])
def test_fast_object_encoding_matches_next_object(draft, exts):
    # The relay forwards with next_object_bytes; it must equal next_object.
    from aiomoqt.messages import SubgroupHeader
    prof = profile_for(draft)
    slow, fast = (SubgroupHeader(track_alias=1, group_id=3, subgroup_id=0,
                                 publisher_priority=128,
                                 extensions_present=True, prof=prof)
                  for _ in range(2))
    for oid in (0, 1, 4):
        a = slow.next_object(payload=b"p" * oid, extensions=exts,
                             object_id=oid).data
        b = fast.next_object_bytes(payload=b"p" * oid, extensions=exts,
                                   object_id=oid)
        assert bytes(a) == bytes(b), (draft, exts, oid)


class _Capturing(_Downstream):
    """Records the bytes written to each stream."""

    def __init__(self):
        super().__init__()
        self.data = {}

    def stream_write(self, sid, data, end_stream=False):
        self.data.setdefault(sid, bytearray()).extend(data)
        super().stream_write(sid, data, end_stream)

    async def stream_write_drain(self, sid, data):
        self.data.setdefault(sid, bytearray()).extend(data)
        await super().stream_write_drain(sid, data)


@pytest.mark.asyncio
async def test_each_object_is_encoded_once_per_stream_state(monkeypatch):
    # Three subscribers in step share one encoding per object; one joining
    # mid-group starts from another state and costs one more, once.
    from aiomoqt.messages import SubgroupHeader
    calls = []
    real = SubgroupHeader.next_object_bytes

    def counting(self, *args, **kwargs):
        calls.append(kwargs.get("object_id"))
        return real(self, *args, **kwargs)
    monkeypatch.setattr(SubgroupHeader, "next_object_bytes", counting)

    track = _RelayedTrack(("ns", "t"))
    early = [_Capturing() for _ in range(3)]
    for alias, down in enumerate(early, start=1):
        track.add_downstream(down, alias, alias)
    try:
        for oid in range(2):
            track.on_object(_Obj(group_id=0, object_id=oid), 0, 0, 0, 0)
        for _ in range(20):
            await asyncio.sleep(0)
        late = _Capturing()
        track.add_downstream(late, 9, 9)
        for oid in range(2, 4):
            track.on_object(_Obj(group_id=0, object_id=oid), 0, 0, 0, 0)
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        track.task.cancel()

    assert calls == [0, 1, 2, 2, 3], calls
    # Every subscriber's stream carries exactly what its own header would
    # have encoded.
    for down, oids in [*((d, range(4)) for d in early), (late, range(2, 4))]:
        (sid,) = down.data
        header = SubgroupHeader(track_alias=0, group_id=0, subgroup_id=0,
                                publisher_priority=128,
                                extensions_present=True,
                                prof=down._profile)
        objects = b"".join(header.next_object_bytes(payload=b"x" * 8,
                                                    object_id=oid)
                           for oid in oids)
        assert bytes(down.data[sid]).endswith(objects)
