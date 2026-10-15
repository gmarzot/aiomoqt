"""Stream control ops on a full TX ring.

aiopquic raises BufferError when its TX ring is full. A reset, FIN or
STOP_SENDING that meets a full ring is queued and sent once the ring
drains; it never escapes into the caller, which may be a fan-out lane or
a control handler. Subgroup headers wait for the ring.
"""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.delivery import FanoutDelivery, StreamMapping, SubgroupDelivery
from aiomoqt.messages import SubgroupHeader
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.types import StreamResetCode

DRAFTS = [14, 16, 18, 20]


class _Ring:
    """Transport TX ring: refuses every op while full."""

    def __init__(self):
        self.full = False
        self.ops = []
        self.tx_event_ring_drain_event = asyncio.Event()

    def arm_tx_event_ring_drain_pending(self):
        pass

    def drain(self):
        self.full = False
        self.tx_event_ring_drain_event.set()


class _Quic:
    closed = False

    def __init__(self, ring):
        self._transport = ring
        self._next = 3
        self._events = {}

    def _push(self, op):
        if self._transport.full:
            raise BufferError("TX ring full")
        self._transport.ops.append(op)

    def reset_stream(self, sid, code):
        self._push(('reset', sid, int(code)))

    def stop_stream(self, sid, code):
        self._push(('stop', sid, int(code)))

    def send_stream_data(self, sid, data, end_stream=False):
        self._push(('data', sid, bytes(data), end_stream))

    async def send_stream_data_drained(self, sid, data, end_stream=False):
        while self._transport.full:
            await asyncio.sleep(0.002)
        self._push(('data', sid, bytes(data), end_stream))

    def get_next_available_stream_id(self, is_unidirectional=True):
        sid = self._next
        self._next += 4
        return sid

    def get_tx_data_drain_event(self, sid):
        return self._events.setdefault(sid, asyncio.Event())


def _session(draft=18):
    s = object.__new__(_MOQTSessionMixin)
    s._close_err = None
    s._session = SimpleNamespace(tx_max_inflight_bytes=None)
    s._profile = profile_for(draft)
    ring = _Ring()
    s._quic = _Quic(ring)
    return s, ring


async def _until(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.002)
    return True


@pytest.mark.parametrize("kind", ["reset", "stop"])
async def test_op_on_full_ring_is_queued_then_sent(kind):
    s, ring = _session()
    ring.full = True
    if kind == 'reset':
        s.stream_reset(7, StreamResetCode.CANCELLED)
    else:
        s.stream_stop_sending(7, StreamResetCode.CANCELLED)
    assert ring.ops == []
    ring.drain()
    assert await _until(lambda: ring.ops)
    assert ring.ops == [(kind, 7, int(StreamResetCode.CANCELLED))]


async def test_op_is_retried_without_a_drain_signal():
    s, ring = _session()
    ring.full = True
    s.stream_reset(7, StreamResetCode.CANCELLED)
    ring.full = False  # drained, but nothing fired the event
    assert await _until(lambda: ring.ops)


async def test_write_after_queued_reset_is_dropped():
    s, ring = _session()
    ring.full = True
    s.stream_reset(7, StreamResetCode.CANCELLED)
    ring.full = False
    s.stream_write(7, b"late")
    assert await _until(lambda: ring.ops)
    await asyncio.sleep(0.05)
    assert ring.ops == [('reset', 7, int(StreamResetCode.CANCELLED))]


async def test_queued_ops_end_with_the_session():
    s, ring = _session()
    ring.full = True
    s.stream_reset(7, StreamResetCode.CANCELLED)
    s._close_err = (0, "closed")
    assert await _until(lambda: s._tx_retry_task is None)
    assert s._tx_deferred is None and ring.ops == []


@pytest.mark.parametrize("draft", DRAFTS)
async def test_reset_on_full_ring_leaves_the_lane_alive(draft):
    s, ring = _session(draft)
    delivery = SubgroupDelivery(s, track_alias=1)
    fan = FanoutDelivery([delivery])
    lane = fan.lanes[0]
    await fan.write(0, 0, b"a", group_start=True)
    await fan.write(0, 1, b"b")
    assert await _until(lambda: lane.queue.empty() and delivery._stream_id)
    first = delivery._stream_id

    ring.full = True
    delivery.cancel()  # a subscription cut mid-group
    ring.drain()
    await fan.write(1, 0, b"c", group_start=True)
    assert await _until(
        lambda: ('reset', first, int(StreamResetCode.CANCELLED)) in ring.ops)
    assert await _until(lambda: any(
        op[0] == 'data' and op[1] != first for op in ring.ops))
    assert not lane.task.done()
    lane.task.cancel()


@pytest.mark.parametrize("draft", DRAFTS)
async def test_lane_truncating_on_full_ring_survives(draft):
    s, ring = _session(draft)
    delivery = SubgroupDelivery(s, track_alias=1)
    gate = SimpleNamespace(forward=True)
    fan = FanoutDelivery()
    lane = fan.add(delivery, lambda: gate.forward, joining=False)
    await fan.write(0, 0, b"a", group_start=True)
    assert await _until(lambda: lane.queue.empty() and delivery._stream_id)
    first = delivery._stream_id

    ring.full = True
    gate.forward = False
    await fan.write(0, 1, b"b")  # gated: the lane resets the open group
    assert await _until(lambda: lane.queue.empty())
    await asyncio.sleep(0.01)
    assert not lane.task.done()
    ring.drain()
    gate.forward = True
    await fan.write(1, 0, b"c", group_start=True)
    assert await _until(
        lambda: ('reset', first, int(StreamResetCode.CANCELLED)) in ring.ops)
    assert await _until(lambda: any(
        op[0] == 'data' and op[1] != first for op in ring.ops))
    assert not lane.task.done()
    assert not [op for op in ring.ops if op[0] == 'data' and op[1] == first
                and op[3]], "the truncated group was FINed"
    lane.task.cancel()


def _drain_later(ring, delay=0.02):
    async def _go():
        await asyncio.sleep(delay)
        ring.drain()
    return asyncio.ensure_future(_go())


def _data(ring, sid):
    return [(op[2], op[3]) for op in ring.ops if op[0] == 'data' and op[1] == sid]


async def test_fin_on_full_ring_is_queued_then_sent():
    s, ring = _session()
    ring.full = True
    s.stream_fin(7)
    s.stream_write(9, b"tail", end_stream=True)
    ring.drain()
    assert await _until(lambda: len(ring.ops) == 2)
    assert sorted(ring.ops) == [('data', 7, b"", True),
                                ('data', 9, b"tail", True)]


@pytest.mark.parametrize("draft", DRAFTS)
async def test_group_header_marker_and_fin_survive_a_full_ring(draft):
    s, ring = _session(draft)
    delivery = SubgroupDelivery(s, track_alias=1)
    ring.full = True
    drain = _drain_later(ring)
    await delivery.write(0, 0, b"a", group_start=True)
    await drain
    sid = delivery._stream_id
    marker = copy.deepcopy(delivery._header).end_group(object_id=1).data
    ring.full = True
    delivery.end_group()
    ring.drain()
    assert await _until(lambda: any(fin for _, fin in _data(ring, sid)))
    sent = _data(ring, sid)
    assert len(sent) == 3, sent
    assert sent[0][0] == SubgroupHeader(
        track_alias=1, group_id=0, subgroup_id=0, publisher_priority=128,
        extensions_present=True, prof=s._profile,
        first_object=True).serialize().data
    assert sent[2] == (marker, True)


@pytest.mark.parametrize("draft", DRAFTS)
async def test_object_stream_header_and_fin_survive_a_full_ring(draft):
    s, ring = _session(draft)
    delivery = SubgroupDelivery(s, track_alias=1,
                                mapping=StreamMapping.PER_OBJECT)
    ring.full = True
    drain = _drain_later(ring)
    await delivery.write(0, 0, b"a", group_start=True)
    await drain
    assert await _until(lambda: len(ring.ops) == 3)
    sids = {op[1] for op in ring.ops}
    assert len(sids) == 1
    sent = _data(ring, sids.pop())
    assert [fin for _, fin in sent] == [False, False, True]
    assert sent[2][0] == b""
