"""Delivery timeouts (d18 §8; d14/d16 DELIVERY_TIMEOUT): an object older
than the subscription's timeout is not sent. On a stream its subgroup is
reset with DELIVERY_TIMEOUT (0x2) and not reopened; a datagram is
dropped."""
import asyncio
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.delivery import FanoutDelivery, StreamMapping, SubgroupDelivery
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, _Subscription, _min_nonzero
from aiomoqt.types import MOQTMessageType, StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs


@pytest.mark.parametrize("a, b, want", [(0, 0, 0), (0, 5, 5), (7, 0, 7),
                                        (7, 5, 5), (3, 9, 3)])
def test_min_nonzero(a, b, want):
    assert _min_nonzero(a, b) == want


@pytest.mark.parametrize("mapping, track, sub, want", [
    (StreamMapping.PER_GROUP, (0, 0), (0, 0), None),
    (StreamMapping.PER_GROUP, (200, 0), (0, 0), 0.2),
    (StreamMapping.PER_GROUP, (200, 0), (100, 0), 0.1),
    (StreamMapping.PER_GROUP, (0, 0), (300, 50), 0.3),
    (StreamMapping.DATAGRAM, (0, 0), (300, 50), 0.05),
    (StreamMapping.DATAGRAM, (0, 80), (300, 0), 0.08),
])
def test_effective_timeout(mapping, track, sub, want):
    t = PublishedTrack(None, "ns", "t")
    t.mapping = mapping
    t.object_delivery_timeout, t.subgroup_delivery_timeout = track
    s = _Subscription(None)
    s.object_timeout, s.subgroup_timeout = sub
    assert t._delivery_deadline(s) == want


@pytest.mark.parametrize("draft, params, want", [
    (16, {0x02: 100}, (100, 0)),
    (16, {0x06: 100}, (0, 0)),
    (18, {0x02: 100, 0x06: 40}, (100, 40)),
])
def test_subscriber_timeouts(draft, params, want):
    s = _Subscription(None)
    s.take_timeouts(params, profile_for(draft))
    assert (s.object_timeout, s.subgroup_timeout) == want


class _FakeSession:
    _profile = profile_for(18)

    def __init__(self):
        self.resets, self.fins, self.writes, self.dgrams = [], [], [], []
        self._next = 2

    async def open_uni_stream(self):
        self._next += 4
        return self._next

    def set_stream_priority(self, *_):
        return True

    async def stream_write_drain(self, sid, data, end_stream=False):
        self.writes.append(sid)

    def stream_write(self, sid, data, end_stream=False):
        if end_stream:
            self.fins.append(sid)

    def stream_reset(self, sid, code):
        self.resets.append((sid, code))

    async def dgram_write_drain(self, buf):
        self.dgrams.append(buf)

    def datagram_max_payload(self):
        return 1200


async def _run_lane(mapping, items, timeout):
    session = _FakeSession()
    delivery = SubgroupDelivery(session, 1, mapping=mapping)
    out = FanoutDelivery()
    lane = out.add(delivery, joining=False, deadline=lambda: timeout)
    for group_id, object_id, age in items:
        lane.queue.put_nowait((group_id, object_id, b"p", None,
                               object_id == 0, time.monotonic() - age,
                               None))
    await out.close()
    return session, delivery, lane


async def test_a_late_datagram_is_dropped():
    session, delivery, lane = await _run_lane(
        StreamMapping.DATAGRAM, [(0, 0, 0.0), (0, 1, 0.3), (0, 2, 0.0)], 0.1)
    assert len(session.dgrams) == 2
    assert lane.expired == 1
    assert delivery.largest == (0, 2)


async def test_a_late_subgroup_object_resets_its_stream():
    session, delivery, lane = await _run_lane(
        StreamMapping.PER_GROUP,
        [(0, 0, 0.0), (0, 1, 0.0), (0, 2, 0.3), (0, 3, 0.0),
         (1, 0, 0.0), (1, 1, 0.0)], 0.1)
    first, second = sorted(set(session.writes))
    assert session.resets == [(first, StreamResetCode.DELIVERY_TIMEOUT)]
    # Nothing more on group 0's subgroup, no reopen; group 1 flows.
    assert session.writes.count(first) == 3  # header + objects 0, 1
    assert session.fins == [second]
    assert delivery.largest == (1, 1)
    assert lane.expired == 1


async def test_no_timeout_sends_everything():
    session, delivery, lane = await _run_lane(
        StreamMapping.PER_GROUP, [(0, 0, 0.0), (0, 1, 5.0)], None)
    assert session.resets == [] and lane.expired == 0
    assert delivery.largest == (0, 1)


# -- loopback: two tracks on one session, one with a timeout ----------

_DRAFTS = [16, 18, 20]
_GROUPS = 6
_PER_GROUP = 10
_NS = "dlvto"


class _Paced(PublishedTrack):
    async def produce(self, out):
        for group_id in range(_GROUPS):
            for object_id in range(_PER_GROUP):
                await out.write(group_id, object_id, bytes(self.object_size),
                                group_start=object_id == 0)
                await asyncio.sleep(0.01)


def _stall_once(session, after: int, seconds: float):
    """Hold every stream write of the publisher for `seconds`, starting
    at its `after`-th write: the drain is blocked for all lanes."""
    orig = session.stream_write_drain
    count, until = [0], [None]

    async def held(sid, data, end_stream=False):
        count[0] += 1
        if count[0] == after:
            until[0] = time.monotonic() + seconds
        if until[0] is not None and time.monotonic() < until[0]:
            await asyncio.sleep(until[0] - time.monotonic())
        return await orig(sid, data, end_stream=end_stream)
    session.stream_write_drain = held


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_late_objects_reset_the_subgroup(draft, use_quic):
    port = 16720 + _DRAFTS.index(draft) + (0 if use_quic else 4)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    rx = {}

    async def on_publish_namespace(session, msg):
        rx["session"] = session
        session.publish_namepace_ok(msg)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            on_publish_namespace)
    quic_server = await server.serve()
    got = {"timed": [], "untimed": []}
    resets = {"timed": [], "untimed": []}
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            tracks = [_Paced(session, _NS, name, object_size=50)
                      for name in got]
            for track in tracks:
                track.attach()
            await session.publish_namespace(namespace=_NS,
                                            wait_response=True)
            _stall_once(session, after=12, seconds=0.3)
            sub = rx["session"]
            for name in got:
                params = {0x02: 100} if name == "timed" else {}

                def on_object(obj, _n, _t, group_id, *_, name=name):
                    if obj.payload:
                        got[name].append((group_id, obj.object_id))
                ok = await sub.subscribe(_NS, name, forward=1,
                                         parameters=params,
                                         on_object=on_object,
                                         wait_response=True)
                sub.register_stream_end_handler(
                    ok.track_alias,
                    lambda g, s, clean, reset_code, name=name:
                        resets[name].append((g, clean, reset_code)))
            total = _GROUPS * _PER_GROUP
            for _ in range(300):
                if len(got["untimed"]) >= total:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
    finally:
        quic_server.close()

    assert len(got["untimed"]) == _GROUPS * _PER_GROUP
    assert all(code != StreamResetCode.DELIVERY_TIMEOUT
               for _, _, code in resets["untimed"])
    timed_out = [g for g, clean, code in resets["timed"]
                 if not clean and code == StreamResetCode.DELIVERY_TIMEOUT]
    assert timed_out, resets["timed"]
    assert len(got["timed"]) < len(got["untimed"])
    # Each group arrives as a prefix: nothing after the cut, no reopen.
    for group_id in {g for g, _ in got["timed"]}:
        ids = sorted(o for g, o in got["timed"] if g == group_id)
        assert ids == list(range(len(ids)))
    # Production after the stall reaches the timed subscriber again.
    assert max(g for g, _ in got["timed"]) == _GROUPS - 1
