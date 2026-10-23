"""Fan-out encodes each object once per header encoding state.

Peers whose streams are in the same state receive the same bytes; the
rest encode their own. Every peer's bytes must equal its own per-peer
encoding.
"""
import asyncio
import contextlib

import pytest

from aiomoqt import delivery as delivery_mod
from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.delivery import FanoutDelivery, StreamMapping, SubgroupDelivery
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.data import ObjectDatagram
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, ObjectStatus

from aiomoqt.tests._certs import CERT, KEY, requires_certs


class _Session:
    """Records the bytes written to each stream and each datagram."""

    def __init__(self, name, draft=18):
        self.name = name
        self.negotiated_draft = draft
        self._profile = profile_for(draft)
        self._next = 3
        self.streams = {}            # stream_id -> bytearray
        self.fins = set()
        self.dgrams = []

    async def open_uni_stream(self):
        sid = self._next
        self._next += 4
        self.streams[sid] = bytearray()
        return sid

    def stream_write(self, sid, data, end_stream=False):
        self.streams[sid] += data
        if end_stream:
            self.fins.add(sid)

    async def stream_write_drain(self, sid, data):
        await asyncio.sleep(0)
        self.streams[sid] += data

    def datagram_max_payload(self):
        return 1 << 16

    async def dgram_write_drain(self, buf):
        await asyncio.sleep(0)
        self.dgrams.append(bytes(buf if isinstance(buf, bytes) else buf.data))

    def stream_reset(self, sid, code):
        pass

    def set_stream_priority(self, sid, prio):
        pass


class _Encodes:
    """Counts NORMAL object encodes on subgroup streams and datagram
    serializations while active."""

    def __init__(self, monkeypatch):
        self.objects = 0
        self.dgrams = 0
        self.active = False
        nob, nobb = SubgroupHeader.next_object, SubgroupHeader.next_object_bytes
        dser = ObjectDatagram.serialize

        def _normal(a, k):
            status = k.get("status", a[2] if len(a) > 2 else None)
            return status in (None, ObjectStatus.NORMAL)

        def next_object(hdr, *a, **k):
            if self.active and _normal(a, k):
                self.objects += 1
            return nob(hdr, *a, **k)

        def next_object_bytes(hdr, *a, **k):
            if self.active and _normal(a, k):
                self.objects += 1
            return nobb(hdr, *a, **k)

        def serialize(dg, *a, **k):
            if self.active:
                self.dgrams += 1
            return dser(dg, *a, **k)

        monkeypatch.setattr(SubgroupHeader, "next_object", next_object)
        monkeypatch.setattr(SubgroupHeader, "next_object_bytes",
                            next_object_bytes)
        monkeypatch.setattr(ObjectDatagram, "serialize", serialize)


def _resolve(exts, session):
    return exts(session) if callable(exts) else exts


def _group_bytes(session, alias, group_id, objs, first_object=True):
    """Reference PER_GROUP stream: header, objects, END_OF_GROUP."""
    hdr = SubgroupHeader(track_alias=alias, group_id=group_id, subgroup_id=0,
                         publisher_priority=128, extensions_present=True,
                         prof=session._profile, first_object=first_object)
    out = bytearray(hdr.serialize().data)
    for oid, payload, exts in objs:
        out += hdr.next_object(payload=payload,
                               extensions=_resolve(exts, session),
                               object_id=oid).data
    out += hdr.end_group(object_id=hdr.next_object_id).data
    return bytes(out)


def _object_bytes(session, alias, group_id, oid, payload, exts):
    """Reference PER_OBJECT stream: one header, one object."""
    hdr = SubgroupHeader(track_alias=alias, group_id=group_id,
                         subgroup_id=oid, publisher_priority=128,
                         extensions_present=True, prof=session._profile)
    return bytes(hdr.serialize().data + hdr.next_object(
        payload=payload, extensions=_resolve(exts, session),
        object_id=oid).data)


def _by_draft(session):
    # Equal for d18 and d22: one encoding serves both.
    return {2: 7, 5: b"v%d" % session._profile.vi64}


def _by_peer(session):
    return {3: session.name.encode()}


# (group_id, object_id, payload, extensions)
_OBJECTS = [
    (0, 0, b"key" * 200, None),
    (0, 1, b"p1", {4: 1, 9: b"x"}),
    (0, 2, b"p2", _by_draft),
    (0, 3, b"", None),
    (0, 4, b"p4", _by_peer),
    (1, 0, b"key" * 50, _by_draft),
    (1, 1, b"p5", None),
]


async def _run(fan, objects=_OBJECTS, between=None):
    for gid, oid, payload, exts in objects:
        if between is not None:
            between(gid, oid)
        await fan.write(gid, oid, payload, extensions=exts,
                        group_start=oid == 0)
        for _ in range(4):
            await asyncio.sleep(0)
    await fan.close()


def _streams(session):
    return [bytes(session.streams[sid]) for sid in sorted(session.streams)]


@pytest.mark.asyncio
async def test_per_group_peers_share_one_encode_per_state(monkeypatch):
    # d18 and d22 share a state; d16 and d14 each have their own.
    peers = [_Session("a", 18), _Session("b", 18), _Session("c", 22),
             _Session("d", 16), _Session("e", 14)]
    fan = FanoutDelivery()
    for alias, s in enumerate(peers, 1):
        fan.add(SubgroupDelivery(s, alias), joining=False)
    spy = _Encodes(monkeypatch)
    spy.active = True
    await _run(fan)
    spy.active = False

    for alias, s in enumerate(peers, 1):
        expect = [_group_bytes(s, alias, g, [(o, p, e) for gg, o, p, e
                                             in _OBJECTS if gg == g])
                  for g in (0, 1)]
        assert _streams(s) == expect, s.name
    # Three states per object; _by_peer splits the shared one by peer.
    assert spy.objects == 3 * len(_OBJECTS) + 2


@pytest.mark.asyncio
async def test_out_of_step_peers_encode_their_own(monkeypatch):
    # b's filter admits group 0 from object 2, so its stream starts
    # there; c joins at group 1; d is gated through group 0.
    a, b, c, d = (_Session(n) for n in "abcd")
    db = SubgroupDelivery(b, 2)
    db.passes = lambda g, o: (g, o) >= (0, 2)
    fan = FanoutDelivery()
    fan.add(SubgroupDelivery(a, 1), joining=False)
    fan.add(db, joining=False)
    forward = {"d": False}
    fan.add(SubgroupDelivery(d, 4), gate=lambda: forward["d"],
            joining=False)

    def between(gid, oid):
        if (gid, oid) == (0, 1):
            fan.add(SubgroupDelivery(c, 3))
        if gid == 1:
            forward["d"] = True

    objects = [o for o in _OBJECTS if o[3] is not _by_peer]
    spy = _Encodes(monkeypatch)
    spy.active = True
    await _run(fan, objects, between)
    spy.active = False

    g0 = [(o, p, e) for g, o, p, e in objects if g == 0]
    g1 = [(o, p, e) for g, o, p, e in objects if g == 1]
    assert _streams(a) == [_group_bytes(a, 1, 0, g0),
                           _group_bytes(a, 1, 1, g1)]
    assert _streams(b) == [_group_bytes(b, 2, 0, g0[2:], first_object=False),
                           _group_bytes(b, 2, 1, g1)]
    assert _streams(c) == [_group_bytes(c, 3, 1, g1)]
    assert _streams(d) == [_group_bytes(d, 4, 1, g1)]
    # Group 0: b's first object differs from a's state, the next is
    # shared again. Group 1: one encode for all four.
    assert spy.objects == len(g0) + 1 + len(g1)


@pytest.mark.asyncio
async def test_per_object_peers_share_one_encode(monkeypatch):
    peers = [_Session("a", 18), _Session("b", 22), _Session("c", 18)]
    fan = FanoutDelivery()
    for alias, s in enumerate(peers, 1):
        fan.add(SubgroupDelivery(s, alias, mapping=StreamMapping.PER_OBJECT),
                joining=False)
    objects = [o for o in _OBJECTS if o[3] is not _by_peer]
    spy = _Encodes(monkeypatch)
    spy.active = True
    await _run(fan, objects)
    spy.active = False

    for alias, s in enumerate(peers, 1):
        assert _streams(s) == [_object_bytes(s, alias, g, o, p, e)
                               for g, o, p, e in objects], s.name
        assert s.fins == set(s.streams)
    assert spy.objects == len(objects)


@pytest.mark.asyncio
async def test_datagram_peers_with_one_alias_share_one_encode(monkeypatch):
    # The alias is in every datagram: only a and b match.
    peers = [(_Session("a", 18), 1), (_Session("b", 18), 1),
             (_Session("c", 18), 2), (_Session("d", 16), 1)]
    fan = FanoutDelivery()
    for s, alias in peers:
        fan.add(SubgroupDelivery(s, alias, mapping=StreamMapping.DATAGRAM),
                joining=False)
    objects = [o for o in _OBJECTS if o[3] is not _by_peer]
    spy = _Encodes(monkeypatch)
    spy.active = True
    await _run(fan, objects)
    spy.active = False

    for s, alias in peers:
        expect = [ObjectDatagram(
            track_alias=alias, group_id=g, object_id=o,
            publisher_priority=128, extensions=_resolve(e, s),
            payload=p).serialize(prof=s._profile).data
            for g, o, p, e in objects]
        assert s.dgrams == expect, s.name
    assert spy.dgrams == 3 * len(objects)


@pytest.mark.parametrize("aliases, kept", [
    ((1, 2, 3, 4, 5, 6), 0),                      # nothing to share
    ((1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6), 4),    # capped
])
@pytest.mark.asyncio
async def test_datagram_encodings_kept_are_bounded(monkeypatch, aliases,
                                                   kept):
    peers = [_Session(str(i)) for i in range(len(aliases))]
    fan = FanoutDelivery()
    for alias, s in zip(aliases, peers):
        fan.add(SubgroupDelivery(s, alias, mapping=StreamMapping.DATAGRAM),
                joining=False)
    items = []
    offer = delivery_mod._Lane.offer

    def spy(lane, item):
        items.append(item)
        offer(lane, item)

    monkeypatch.setattr(delivery_mod._Lane, "offer", spy)
    await _run(fan, _OBJECTS[:3])
    held = [len(item[-1]) for item in items if isinstance(item, tuple)]
    assert held and max(held) == kept
    for alias, s in zip(aliases, peers):
        assert s.dgrams == [ObjectDatagram(
            track_alias=alias, group_id=g, object_id=o,
            publisher_priority=128, extensions=_resolve(e, s),
            payload=p).serialize(prof=s._profile).data
            for g, o, p, e in _OBJECTS[:3]]


# -- end to end ------------------------------------------------------

_BASE_PORT = 16800
_GROUPS, _PER_GROUP = 3, 4
# Three publisher sessions per track; d18 and d22 share one encoding.
_PEER_DRAFTS = (18, 22, 18)


class _Produced(PublishedTrack):
    """Waits for every peer's lane, then writes a fixed sequence."""

    def __init__(self, *a, index, peers, **k):
        super().__init__(*a, **k)
        self.index = index
        self.peers = peers

    def payload(self, g, o):
        return bytes([self.index, g, o]) * (100 * (self.index + 1))

    async def produce(self, out):
        while len(out.lanes) < self.peers:
            await asyncio.sleep(0.01)
        for g in range(_GROUPS):
            for o in range(_PER_GROUP):
                await out.write(g, o, self.payload(g, o),
                                extensions={2: g},
                                group_start=o == 0)
                await asyncio.sleep(0.002)


class _Receiver:
    def __init__(self):
        self.publishes: asyncio.Queue = asyncio.Queue()
        self.got = {}                # (session id, alias) -> payloads

    async def on_publish(self, session, msg):
        key = (id(session), msg.track_alias)
        self.got[key] = []

        def _on_object(obj, _size, _now, group_id, *_):
            if obj.payload:
                self.got[key].append(
                    (msg.track_name, group_id, obj.object_id,
                     bytes(obj.payload)))
        session.register_object_handler(msg.track_alias, _on_object)
        session.publish_ok(msg, forward=1)
        await self.publishes.put(msg)


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_fanout_to_three_peers_encodes_once(monkeypatch, use_quic):
    port = _BASE_PORT + (0 if use_quic else 1)
    rx = _Receiver()
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=[22, 18])
    server.register_handler(MOQTMessageType.PUBLISH, rx.on_publish)
    quic_server = await server.serve()
    spy = _Encodes(monkeypatch)
    try:
        async with contextlib.AsyncExitStack() as stack:
            sessions = []
            for draft in _PEER_DRAFTS:
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                sessions.append(s)
            tracks = [_Produced(sessions[0], namespace="enc/once",
                                trackname=name, index=i,
                                peers=len(sessions))
                      for i, name in enumerate(("audio", "video"))]
            for t in tracks:
                for s in sessions[1:]:
                    t.add_session(s)
            spy.active = True
            for t in tracks:
                for s in sessions:
                    await t.publish(forward=0, session=s)
            for _ in range(len(tracks) * len(sessions)):
                await asyncio.wait_for(rx.publishes.get(), 5.0)
            async with asyncio.timeout(5.0):
                while any(t._production is None for t in tracks):
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(
                asyncio.gather(*(t._production for t in tracks)), 10.0)
            want = _GROUPS * _PER_GROUP
            async with asyncio.timeout(5.0):
                while not all(len(v) >= want for v in rx.got.values()):
                    await asyncio.sleep(0.02)
            spy.active = False
    finally:
        quic_server.close()

    assert len(rx.got) == len(tracks) * len(sessions)
    by_name = {t.trackname.encode(): t for t in tracks}
    for got in rx.got.values():
        assert len(got) == want
        for name, g, o, payload in got:
            assert payload == by_name[name].payload(g, o)
    assert spy.objects == len(tracks) * want
