"""A publisher may reset a subgroup's stream and reopen the subgroup on a
new one (§2.2). The new stream can be parsed before the old stream's
RESET arrives; it is accepted, and the late RESET leaves it bound."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.protocol import QuicErrorCode
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_early_objects import _stub

_BASE_PORT = 16710
_NS = "subgroup/reopen"
_NAMES = ("audio", "video")


def _open(s, sid, alias, group_id=0):
    hdr = SubgroupHeader(track_alias=alias, group_id=group_id,
                         subgroup_id=0, prof=s._profile)
    s._on_stream_data(sid, bytes(hdr.serialize().data), False)
    return hdr


def _obj(s, sid, hdr, object_id, fin=False):
    s._on_stream_data(sid, bytes(hdr.next_object(
        payload=b"x", object_id=object_id).data), fin)


@pytest.mark.parametrize("draft", [14, 16, 18, 20])
def test_reopened_subgroup_survives_the_old_streams_reset(draft):
    s = _stub(draft)
    s.stream_stop_sending = lambda sid, code: None
    got, ended = [], []
    for alias in (7, 8):
        s._track_aliases[alias] = alias
        s.register_object_handler(
            alias, lambda obj, size, ts, gid, sgid, a=alias: got.append(
                (a, obj.object_id)))
        s.register_stream_end_handler(
            alias, lambda gid, sgid, clean, reset_code, a=alias:
            ended.append((a, clean)))
    old7, old8 = _open(s, 3, 7), _open(s, 7, 8)
    _obj(s, 3, old7, 0)
    _obj(s, 7, old8, 0)
    new7, new8 = _open(s, 11, 7), _open(s, 15, 8)
    _obj(s, 11, new7, 1)
    _obj(s, 15, new8, 1)
    for sid in (3, 7):
        s._cleanup_stream(sid, QuicErrorCode.APPLICATION_ERROR,
                          reset_code=int(StreamResetCode.CANCELLED))
    assert s._subgroup_stream_by_key == {(7, 0, 0): 11, (8, 0, 0): 15}
    _obj(s, 11, new7, 2, fin=True)
    _obj(s, 15, new8, 2, fin=True)
    assert s.closed == []
    for alias in (7, 8):
        assert [o for a, o in got if a == alias] == [0, 1, 2]
        assert [c for a, c in ended if a == alias] == [True]
    assert s._subgroup_stream_by_key == {}


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
async def test_reset_then_reopen_delivers_the_subgroup(use_quic):
    got = {name: [] for name in _NAMES}

    async def on_publish(session, msg):
        name = msg.track_name.decode()
        session.register_object_handler(
            msg.track_alias,
            lambda obj, size, ts, gid, sgid, n=name: got[n].append(
                obj.object_id))
        session.publish_ok(msg, forward=1)

    port = _BASE_PORT + (0 if use_quic else 1)
    async with _loopback(port, use_quic, on_publish) as session:
        pubs = [session.publish(_NS, name, forward=1) for name in _NAMES]
        await asyncio.sleep(0.2)
        def header(pub):
            return SubgroupHeader(track_alias=pub.track_alias, group_id=0,
                                  subgroup_id=0, publisher_priority=128,
                                  prof=session._profile)
        streams = []
        for pub in pubs:
            hdr = header(pub)
            old = await session.open_uni_stream()
            session.stream_write(old, hdr.serialize().data)
            session.stream_write(old, hdr.next_object(
                payload=b"a", object_id=0).data)
            streams.append((old, hdr))
        await asyncio.sleep(0.1)
        reopened = []
        for pub in pubs:
            hdr = header(pub)
            new = await session.open_uni_stream()
            session.stream_write(new, hdr.serialize().data)
            session.stream_write(new, hdr.next_object(
                payload=b"b", object_id=1).data)
            reopened.append((new, hdr))
        await asyncio.sleep(0.1)
        for old, _ in streams:
            session.stream_reset(old, StreamResetCode.CANCELLED)
        await asyncio.sleep(0.1)
        for new, hdr in reopened:
            session.stream_write(new, hdr.next_object(
                payload=b"c", object_id=2).data, end_stream=True)
        try:
            async with asyncio.timeout(5.0):
                while any(len(v) < 3 for v in got.values()):
                    await asyncio.sleep(0.02)
        except TimeoutError:
            pass

    for name in _NAMES:
        assert got[name] == [0, 1, 2], got
