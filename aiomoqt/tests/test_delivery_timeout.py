"""Delivery timeouts (d18 §8; d14/d16 DELIVERY_TIMEOUT): an object older
than the subscription's timeout is not sent. On a stream its subgroup is
reset with DELIVERY_TIMEOUT (0x2) and not reopened; a datagram is
dropped."""
import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt import delivery as delivery_mod
from aiomoqt.delivery import (FanoutDelivery, OBJECT_RECORD_BYTES,
                              StreamMapping, SubgroupDelivery, SubgroupTimer)
from aiomoqt.server import MOQTServer
from aiomoqt.track import (PublishedTrack, _Subscription, _min_nonzero,
                           _reply_properties)
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

    async def open_uni_stream(self, initial=b"", priority=None,
                              end_stream=False):
        self._next += 4
        if initial or end_stream:
            await self.stream_write_drain(self._next, initial, end_stream)
        return self._next

    def set_stream_priority(self, *_):
        return True

    async def stream_write_drain(self, sid, data, end_stream=False):
        self.writes.append(sid)
        if end_stream:
            self.fins.append(sid)

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
    lane = out.add(delivery, joining=False, deadline=timeout)
    for group_id, object_id, age in items:
        lane.offer((group_id, OBJECT_RECORD_BYTES + 1, True,
                    time.monotonic() - age, object_id, b"p", None,
                    object_id == 0, None, False))
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
    assert session.writes.count(first) == 2  # header with object 0, object 1
    assert session.fins == [second]
    assert delivery.largest == (1, 1)
    assert lane.expired == 1


async def test_no_timeout_sends_everything():
    session, delivery, lane = await _run_lane(
        StreamMapping.PER_GROUP, [(0, 0, 0.0), (0, 1, 5.0)], None)
    assert session.resets == [] and lane.expired == 0
    assert delivery.largest == (0, 1)


async def test_a_late_per_object_object_drops_only_itself():
    session, delivery, lane = await _run_lane(
        StreamMapping.PER_OBJECT,
        [(0, 0, 0.0), (0, 1, 0.3), (0, 2, 0.0), (0, 3, 0.0)], 0.1)
    # Each object is its own subgroup: the rest of the group still goes.
    assert lane.expired == 1 and session.resets == []
    assert len(session.fins) == 3
    assert delivery.objects_sent == 3 and delivery.largest == (0, 3)


_TIMEOUT_PROPS = (0x02, 0x06)


@pytest.mark.parametrize("draft, timeouts, want", [
    (14, (200, 50), None),
    (16, (200, 50), {0x02: 200}),
    (18, (200, 50), {0x02: 200, 0x06: 50}),
    (20, (200, 50), {0x02: 200, 0x06: 50}),
    (20, (0, 50), {0x06: 50}),
    (20, (0, 0), {}),
])
def test_track_properties_carry_the_timeouts(draft, timeouts, want):
    t = PublishedTrack(None, "ns", "t")
    t.object_delivery_timeout, t.subgroup_delivery_timeout = timeouts
    props = t._track_properties(SimpleNamespace(_profile=profile_for(draft)))
    if want is None:
        assert props is None
    else:
        assert {k: v for k, v in (props or {}).items()
                if k in _TIMEOUT_PROPS} == want


@pytest.mark.parametrize("argv, want", [([], 0),
                                         (["--delivery-timeout", "250"], 250)])
def test_pub_media_delivery_timeout(monkeypatch, argv, want):
    from aiomoqt.tools import pub_media
    monkeypatch.setattr(sys, "argv",
                        ["pub_media", "moqt://localhost:4433", *argv])
    args = pub_media.parse_args()
    assert args.delivery_timeout == want
    pub = pub_media._Publisher([], "ns", None)
    pub.delivery_timeout = args.delivery_timeout
    track = pub.add_track(PublishedTrack(None, "ns", "video"))
    assert (track.object_delivery_timeout,
            track.subgroup_delivery_timeout) == (want, want)


# -- SUBGROUP_DELIVERY_TIMEOUT sweep -----------------------------------

_DT = StreamResetCode.DELIVERY_TIMEOUT


class _Quic:
    def __init__(self):
        self.released = set()
        self.asked = []

    def stream_released(self, sid):
        self.asked.append(sid)
        return sid in self.released


class _TimedSession(_FakeSession):
    def __init__(self):
        super().__init__()
        self._quic = _Quic()


async def test_the_sweep_resets_an_unreleased_stream():
    session = _TimedSession()
    timer = SubgroupTimer(session, 0.05)
    timer.complete(5)
    await asyncio.sleep(0.02)
    assert session.resets == []
    await asyncio.sleep(0.06)
    assert session.resets == [(5, _DT)] and timer.expired == 1
    assert timer._handle is None and not timer._due


async def test_a_released_stream_is_not_reset():
    session = _TimedSession()
    timer = SubgroupTimer(session, 0.03)
    timer.complete(5)
    session._quic.released.add(5)
    await asyncio.sleep(0.06)
    assert session._quic.asked == [5]
    assert session.resets == [] and timer.expired == 0


async def test_entries_come_due_in_order():
    session = _TimedSession()
    timer = SubgroupTimer(session, 0.06)
    timer.complete(5)
    await asyncio.sleep(0.04)
    timer.complete(9)
    timer.complete(13)
    session._quic.released.add(9)
    await asyncio.sleep(0.04)
    # 5 is due; 9 and 13 are not, and are not asked about yet.
    assert session.resets == [(5, _DT)]
    assert session._quic.asked == [5]
    await asyncio.sleep(0.05)
    assert session._quic.asked == [5, 9, 13]
    assert session.resets == [(5, _DT), (13, _DT)]


async def test_no_timeout_arms_nothing():
    session = _TimedSession()
    delivery = SubgroupDelivery(session, 1)
    delivery.subgroup_timeout = 0
    assert delivery._subgroups is None
    for gid in range(3):
        await delivery.write(gid, 0, b"p", group_start=True,
                             end_of_group=True)
    assert len(session.fins) == 3
    await asyncio.sleep(0.02)
    assert session._quic.asked == [] and session.resets == []
    timer = SubgroupTimer(session, 0)
    timer.complete(5)
    assert timer._handle is None and not timer._due


async def test_no_stream_released_arms_nothing(monkeypatch, caplog):
    monkeypatch.setattr(delivery_mod, "_unenforced_logged", False)
    session = _FakeSession()
    delivery = SubgroupDelivery(session, 1)
    delivery.subgroup_timeout = 0.01
    with caplog.at_level("INFO", logger="aiomoqt.delivery"):
        for gid in range(3):
            await delivery.write(gid, 0, b"p", group_start=True,
                                 end_of_group=True)
    timer = delivery._subgroups
    assert timer._handle is None and not timer._due
    lines = [r for r in caplog.records
             if "SUBGROUP_DELIVERY_TIMEOUT is not enforced" in r.message]
    assert len(lines) == 1 and lines[0].levelname == "INFO"
    await asyncio.sleep(0.03)
    assert session.resets == []


@pytest.mark.parametrize("mapping, fins", [(StreamMapping.PER_GROUP, 2),
                                           (StreamMapping.PER_OBJECT, 4)])
async def test_lane_fins_start_the_subgroup_timer(mapping, fins):
    session = _TimedSession()
    delivery = SubgroupDelivery(session, 1, mapping=mapping)
    delivery.subgroup_timeout = 0.03
    out = FanoutDelivery()
    out.add(delivery, joining=False)
    for gid, oid in ((0, 0), (0, 1), (1, 0), (1, 1)):
        delivery.lane.offer((gid, OBJECT_RECORD_BYTES + 1, True,
                             time.monotonic(), oid, b"p", None, oid == 0,
                             None, False))
    await out.close()
    assert len(session.fins) == fins
    released = session.fins[0]
    session._quic.released.add(released)
    await asyncio.sleep(0.06)
    assert sorted(session._quic.asked) == sorted(session.fins)
    assert sorted(sid for sid, _ in session.resets) == sorted(
        f for f in session.fins if f != released)
    assert all(code == _DT for _, code in session.resets)


def test_subgroup_deadline_per_mapping():
    t = PublishedTrack(None, "ns", "t")
    s = _Subscription(None)
    t.subgroup_delivery_timeout, s.subgroup_timeout = 300, 100
    assert t._subgroup_deadline(s) == 0.1
    s.subgroup_timeout = 0
    assert t._subgroup_deadline(s) == 0.3
    t.subgroup_delivery_timeout = 0
    assert t._subgroup_deadline(s) is None
    t.subgroup_delivery_timeout = 300
    t.mapping = StreamMapping.DATAGRAM
    assert t._subgroup_deadline(s) is None


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
    at its `after`-th write: the drain is blocked for all lanes, and a
    write that would not wait is refused."""
    orig = session.stream_write_drain
    orig_nowait = session.stream_write_nowait
    count, until = [0], [None]

    def stalled():
        count[0] += 1
        if count[0] == after:
            until[0] = time.monotonic() + seconds
        return until[0] is not None and time.monotonic() < until[0]

    async def held(sid, data, end_stream=False, **kw):
        if stalled():
            await asyncio.sleep(until[0] - time.monotonic())
        return await orig(sid, data, end_stream=end_stream, **kw)

    def refused(sid, data):
        if stalled():
            return False
        return orig_nowait(sid, data)
    session.stream_write_drain = held
    session.stream_write_nowait = refused


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
        session.publish_namespace_ok(msg)
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


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [18, 20])
async def test_track_property_timeouts_reset_the_subgroup(draft, use_quic):
    """The publisher's own timeouts, announced as Track Properties, time
    out one track's subgroups; the other track, with longer ones, loses
    nothing."""
    port = 16730 + 2 * (draft == 20) + (0 if use_quic else 1)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    rx = {}

    async def on_publish_namespace(session, msg):
        rx["session"] = session
        session.publish_namespace_ok(msg)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            on_publish_namespace)
    quic_server = await server.serve()
    timeouts = {"timed": (100, 300), "patient": (60_000, 60_000)}
    got = {name: [] for name in timeouts}
    resets = {name: [] for name in timeouts}
    props = {}
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            for name, (obj_ms, sg_ms) in timeouts.items():
                track = _Paced(session, _NS, name, object_size=50)
                track.object_delivery_timeout = obj_ms
                track.subgroup_delivery_timeout = sg_ms
                track.attach()
            await session.publish_namespace(namespace=_NS,
                                            wait_response=True)
            _stall_once(session, after=12, seconds=0.3)
            sub = rx["session"]
            for name in timeouts:
                def on_object(obj, _n, _t, group_id, *_, name=name):
                    if obj.payload:
                        got[name].append((group_id, obj.object_id))
                ok = await sub.subscribe(_NS, name, forward=1,
                                         on_object=on_object,
                                         wait_response=True)
                props[name] = dict(_reply_properties(ok) or {})
                sub.register_stream_end_handler(
                    ok.track_alias,
                    lambda g, s, clean, reset_code, name=name:
                        resets[name].append((g, clean, reset_code)))
            total = _GROUPS * _PER_GROUP
            for _ in range(300):
                if len(got["patient"]) >= total:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
    finally:
        quic_server.close()

    for name, (obj_ms, sg_ms) in timeouts.items():
        assert props[name].get(0x02) == obj_ms, props[name]
        assert props[name].get(0x06) == sg_ms, props[name]
    assert len(got["patient"]) == _GROUPS * _PER_GROUP
    assert all(code != StreamResetCode.DELIVERY_TIMEOUT
               for _, _, code in resets["patient"])
    timed_out = [g for g, clean, code in resets["timed"]
                 if not clean and code == StreamResetCode.DELIVERY_TIMEOUT]
    assert timed_out, resets["timed"]
    assert len(got["timed"]) < len(got["patient"])


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [18, 20])
async def test_released_streams_are_not_reset(draft, use_quic):
    """Over the real transport: a subgroup timer arms only on the track
    with a SUBGROUP_DELIVERY_TIMEOUT, and the transport reports every
    delivered stream released, so none is reset."""
    port = 16734 + 2 * (draft == 20) + (0 if use_quic else 1)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    rx = {}

    async def on_publish_namespace(session, msg):
        rx["session"] = session
        session.publish_namespace_ok(msg)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            on_publish_namespace)
    quic_server = await server.serve()
    names = {"timed": 60_000, "plain": 0}
    got = {name: 0 for name in names}
    resets = {name: [] for name in names}
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            if getattr(session._quic, "stream_released", None) is None:
                pytest.skip("aiopquic has no stream_released()")
            await session.client_session_init()
            tracks = {}
            for name, ms in names.items():
                tracks[name] = _Paced(session, _NS, name, object_size=50)
                tracks[name].subgroup_delivery_timeout = ms
                tracks[name].attach()
            await session.publish_namespace(namespace=_NS,
                                            wait_response=True)
            sub = rx["session"]
            for name in names:
                def on_object(obj, *_, name=name):
                    if obj.payload:
                        got[name] += 1
                ok = await sub.subscribe(_NS, name, forward=1,
                                         on_object=on_object,
                                         wait_response=True)
                sub.register_stream_end_handler(
                    ok.track_alias,
                    lambda g, s, clean, reset_code, name=name:
                        resets[name].append((g, clean, reset_code)))
            total = _GROUPS * _PER_GROUP
            for _ in range(300):
                if all(n >= total for n in got.values()):
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            timers = {name: [s.delivery._subgroups
                             for s in tracks[name]._subs]
                      for name in names}
            timed = timers["timed"][0]
            queued = [sid for sid, _ in timed._due]
            released = [timed._released(sid) for sid in queued]
            timed.cancel()
    finally:
        quic_server.close()

    assert got == {name: _GROUPS * _PER_GROUP for name in names}
    assert timers["plain"] == [None]
    assert len(queued) == _GROUPS and all(released), (queued, released)
    assert timed.expired == 0
    assert not any(code == StreamResetCode.DELIVERY_TIMEOUT
                   for r in resets.values() for _, _, code in r)
