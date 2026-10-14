"""Objects that arrive before their alias is bound are held and replayed
to the handler that binds it, within a count, byte and time bound."""
import asyncio
import contextlib
import time
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.messages import SubgroupHeader
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 16700
_NS = "early/objects"
_NAMES = ("audio", "video")
_GROUPS = 4


def _stub(draft=18):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._data_streams = {}
    s._control_chains = {}
    s._uni_peek_stash = {}
    s._stream_torn_down = {}
    s._stream_torn_down_last_sweep = time.monotonic()
    s._stream_torn_down_evict_after = 30.0
    s._stream_end_handlers = {}
    s._early_stream_ends = OrderedDict()
    s._reset_early_objects()
    s._early_objects_dropped = 0
    s._fetch_done_futures = {}
    s._fetch_object_handlers = {}
    s._fetch_done_results = OrderedDict()
    s._subgroup_stream_by_key = {}
    s._fetch_stream_by_request = {}
    s._track_aliases = {}
    s._unbound_aliases = {}
    s._unbound_escalated = set()
    s._malformed_aliases = set()
    s._group_bound = {}
    s._track_bound = {}
    s._object_handlers = {}
    s._track_default_priority = {}
    s.on_object_received = None
    s._loop = SimpleNamespace(call_later=lambda delay, cb: SimpleNamespace(
        cancel=lambda: None))
    s.closed = []
    s._close_session = lambda code, reason: s.closed.append((int(code), reason))
    return s


def _send_group(s, sid, alias, group_id, payloads, fin=True):
    hdr = SubgroupHeader(track_alias=alias, group_id=group_id,
                         subgroup_id=0, prof=s._profile)
    s._on_stream_data(sid, bytes(hdr.serialize().data), False)
    for i, payload in enumerate(payloads):
        last = fin and i == len(payloads) - 1
        s._on_stream_data(sid, bytes(hdr.next_object(payload=payload).data),
                          last)


def test_held_objects_replay_before_the_stream_end():
    s = _stub()
    _send_group(s, 3, alias=7, group_id=2, payloads=[b"a", b"b", b"c"])
    events = []
    s.register_object_handler(
        7, lambda obj, size, ts, gid, sgid: events.append(
            ("obj", gid, obj.object_id, bytes(obj.payload))))
    s.register_stream_end_handler(
        7, lambda gid, sgid, clean, reset_code: events.append(
            ("end", gid, clean)))
    assert events == [("obj", 2, 0, b"a"), ("obj", 2, 1, b"b"),
                      ("obj", 2, 2, b"c"), ("end", 2, True)]
    assert s._early_objects == {} and s._early_objects_count == 0
    assert s._early_objects_bytes == 0 and s._early_objects_dropped == 0


def test_held_objects_are_bounded_by_count():
    s = _stub()
    s.EARLY_OBJECT_MAX = 2
    _send_group(s, 3, alias=7, group_id=0, payloads=[b"a", b"b", b"c"])
    assert s._early_objects_count == 2 and s._early_objects_dropped == 1
    got = []
    s.register_object_handler(7, lambda obj, *_: got.append(obj.object_id))
    assert got == [0, 1]


def test_held_objects_are_bounded_by_bytes():
    s = _stub()
    _send_group(s, 3, alias=7, group_id=0, payloads=[b"a" * 8])
    s.EARLY_OBJECT_MAX_BYTES = s._early_objects_bytes + 1
    _send_group(s, 7, alias=7, group_id=1, payloads=[b"b" * 8])
    assert s._early_objects_count == 1 and s._early_objects_dropped == 1


def test_held_objects_expire():
    s = _stub()
    s.EARLY_OBJECT_HOLD_S = 0.0
    _send_group(s, 3, alias=7, group_id=0, payloads=[b"a", b"b"])
    got = []
    s.register_object_handler(7, lambda obj, *_: got.append(obj.object_id))
    assert got == []
    assert s._early_objects_dropped == 2 and s._early_objects_count == 0


def test_the_session_wide_handler_takes_objects_directly():
    s = _stub()
    got = []
    s.on_object_received = lambda obj, size, ts, gid, sgid: got.append(
        (gid, obj.object_id))
    _send_group(s, 3, alias=7, group_id=0, payloads=[b"a", b"b"])
    assert got == [(0, 0), (0, 1)]
    assert s._early_objects == {}


def test_a_bound_alias_without_a_handler_holds_nothing():
    s = _stub()
    s._track_aliases = {7: 1}
    _send_group(s, 3, alias=7, group_id=0, payloads=[b"a"])
    assert s._early_objects == {}


@contextlib.asynccontextmanager
async def _loopback(port, use_quic, on_publish):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH, on_publish)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=18)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        handle.close()


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_objects_sent_before_publish_ok_reach_their_track(use_quic):
    got = {name: [] for name in _NAMES}
    rx = {}

    async def on_publish(session, msg):
        rx["session"] = session
        # Bind late so the data streams land first.
        await asyncio.sleep(0.2)
        name = msg.track_name.decode()
        session.register_object_handler(
            msg.track_alias,
            lambda obj, size, ts, gid, sgid, n=name: got[n].append(
                (gid, obj.object_id, bytes(obj.payload))))
        session.publish_ok(msg, forward=1)

    port = _BASE_PORT + (0 if use_quic else 1)
    async with _loopback(port, use_quic, on_publish) as session:
        pubs = [session.publish(_NS, name, forward=1) for name in _NAMES]
        for group_id in range(_GROUPS):
            for pub in pubs:
                sid = await session.open_uni_stream()
                hdr = SubgroupHeader(track_alias=pub.track_alias,
                                     group_id=group_id, subgroup_id=0,
                                     publisher_priority=128,
                                     prof=session._profile)
                session.stream_write(sid, hdr.serialize().data)
                payload = f"{pub.track_name.decode()}-{group_id}".encode()
                session.stream_write(
                    sid, hdr.next_object(payload=payload).data,
                    end_stream=True)
        try:
            async with asyncio.timeout(5.0):
                while any(len(v) < _GROUPS for v in got.values()):
                    await asyncio.sleep(0.02)
        except TimeoutError:
            pass
        dropped = rx["session"]._early_objects_dropped if rx else None

    for name in _NAMES:
        assert sorted(got[name]) == [
            (g, 0, f"{name}-{g}".encode()) for g in range(_GROUPS)], got
    assert dropped == 0
