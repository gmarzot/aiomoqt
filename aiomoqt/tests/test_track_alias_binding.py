"""Track-alias binding: the publisher's SUBSCRIBE_OK alias is authoritative.

SUBSCRIBE carries no Track Alias on the wire; the publisher assigns one
and returns it in SUBSCRIBE_OK. The client must key its alias registry
from the reply and never from a local guess. The mock server here bumps
its allocator to +1000 so any client-side guess is guaranteed to
mismatch — aiomoqt<->aiomoqt loopback otherwise passes by coincidence,
both sides counting aliases from 0.
"""
import asyncio
import logging

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.data import ObjectDatagram

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14700
_ALIAS_BASE = 1000
_N_OBJECTS = 5


@pytest.fixture
def plog(caplog):
    """Capture aiomoqt.protocol records despite propagate=False."""
    lg = logging.getLogger("aiomoqt.protocol")
    lg.addHandler(caplog.handler)
    prev = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        lg.setLevel(prev)
        lg.removeHandler(caplog.handler)


async def _on_subscribe(session, msg):
    # Mock a foreign relay's allocation scheme: aliases start at 1000.
    session._next_track_alias = max(session._next_track_alias, _ALIAS_BASE)
    ok = session.subscribe_ok(request_msg=msg, content_exists=0)
    # Let the OK reach the subscriber before any data: the §10.4.2
    # data-before-OK race is tolerated-with-warning by design; this test
    # asserts the post-OK steady state (silent admission under the
    # authoritative alias).
    await asyncio.sleep(0.05)
    stream_id = await session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=ok.track_alias, group_id=0,
                         subgroup_id=0, publisher_priority=0,
                         prof=session._profile)
    session.stream_write(stream_id, hdr.serialize().data)
    for i in range(_N_OBJECTS):
        session.stream_write(
            stream_id, hdr.next_object(payload=f"alias-{i}".encode()).data,
            end_stream=(i == _N_OBJECTS - 1))


async def _on_subscribe_datagrams(session, msg):
    session._next_track_alias = max(session._next_track_alias, _ALIAS_BASE)
    ok = session.subscribe_ok(request_msg=msg, content_exists=0)
    for i in range(_N_OBJECTS):
        dgram = ObjectDatagram(track_alias=ok.track_alias, group_id=0,
                               object_id=i, payload=f"dgram-{i}".encode())
        session.send_dgram_message(dgram.serialize(prof=session._profile))


def _server(port, handler=_on_subscribe, draft=18):
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=draft,
    )
    server.register_handler(MOQTMessageType.SUBSCRIBE, handler)
    return server


def _client(port, draft=18):
    return MOQTClient(
        "localhost", port, path="/", use_quic=True,
        verify_tls=False, supported_drafts=draft,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_registry_keyed_from_subscribe_ok(draft):
    # The peer's alias (>= 1000) must be the registry key; a guessed
    # low alias must not survive the OK.
    port = _BASE_PORT + draft
    server = await _server(port, draft=draft).serve()
    try:
        client = _client(port, draft=draft)
        async with client.connect() as session:
            await session.client_session_init()
            assert session.negotiated_draft == draft
            ok = await session.subscribe(
                "alias/ns", "track", wait_response=True)
            assert ok.track_alias >= _ALIAS_BASE
            assert session._track_aliases.get(
                ok.track_alias) == ok.request_id
            stale = [a for a in session._track_aliases if a < _ALIAS_BASE]
            assert not stale, f"guessed aliases survived OK: {stale}"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_no_guess_registered_before_ok():
    # No-guess design: subscribe() must not invent a registry entry;
    # the binding appears only once SUBSCRIBE_OK arrives.
    port = _BASE_PORT + 20
    server = await _server(port).serve()
    try:
        client = _client(port)
        async with client.connect() as session:
            await session.client_session_init()
            session.subscribe("alias/ns", "track", wait_response=False)
            assert session._track_aliases == {}
            for _ in range(200):
                if session._track_aliases:
                    break
                await asyncio.sleep(0.02)
            assert list(session._track_aliases) == [_ALIAS_BASE]
    finally:
        server.close()


@pytest.mark.asyncio
async def test_join_does_not_guess():
    # join() pre-allocated a guess the same way subscribe() did.
    port = _BASE_PORT + 21
    server = await _server(port).serve()
    try:
        client = _client(port)
        async with client.connect() as session:
            await session.client_session_init()
            await session.join("alias/ns", "track", wait_response=False)
            stale = [a for a in session._track_aliases if a < _ALIAS_BASE]
            assert not stale, f"join() guessed aliases: {stale}"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_subgroup_admission_clean(plog):
    # With the authoritative binding in place, post-OK subgroup streams
    # must admit silently — no unknown-track_alias warnings.
    port = _BASE_PORT + 22
    received = []
    server = await _server(port).serve()
    try:
        client = _client(port)
        async with client.connect() as session:
            await session.client_session_init()
            session.on_object_received = (
                lambda msg, size, ts, gid, sgid:
                received.append((msg.object_id, bytes(msg.payload))))
            await session.subscribe("alias/ns", "track", wait_response=True)
            for _ in range(200):
                if len(received) >= _N_OBJECTS:
                    break
                await asyncio.sleep(0.02)
    finally:
        server.close()
    assert len(received) == _N_OBJECTS
    warnings = [r for r in plog.records
                if r.levelno >= logging.WARNING
                and "track_alias" in r.getMessage()]
    assert not warnings, [r.getMessage() for r in warnings]


@pytest.mark.asyncio
async def test_datagram_delivery_survives(plog):
    # Datagrams under the relay's alias must deliver and must not kill
    # the session (guards any future alias validation on the RX path).
    port = _BASE_PORT + 23
    received = []
    server = await _server(port, handler=_on_subscribe_datagrams).serve()
    try:
        client = _client(port)
        async with client.connect() as session:
            await session.client_session_init()
            session.on_object_received = (
                lambda msg, size, ts, gid, sgid:
                received.append((msg.object_id, bytes(msg.payload))))
            await session.subscribe("alias/ns", "track", wait_response=True)
            for _ in range(200):
                if len(received) >= _N_OBJECTS:
                    break
                await asyncio.sleep(0.02)
            assert not session._moqt_session_closed.done(), "session died"
    finally:
        server.close()
    assert len(received) == _N_OBJECTS
    fatal = [r.getMessage() for r in plog.records
             if "PROTOCOL_VIOLATION" in r.getMessage()
             or "unknown track" in r.getMessage()]
    assert not fatal, fatal


# -- unbound-alias criteria ------------------------------------------
#
# Data may legitimately arrive before the control message that binds an
# alias (§10.4.2). That race is NOT an error, so it must stay at DEBUG.
# It becomes an error only when it never clears. These pin both edges.

def _session_stub(draft=18):
    """Bare mixin instance with only the alias state these paths use."""
    from aiomoqt.protocol import _MOQTSessionMixin
    s = object.__new__(_MOQTSessionMixin)
    s._track_aliases = {}
    s._unbound_aliases = {}
    s._unbound_escalated = set()
    return s


class _Hdr:
    def __init__(self, alias, group=0, subgroup=0):
        self.track_alias, self.group_id, self.subgroup_id = (
            alias, group, subgroup)


def test_brief_unbound_race_stays_quiet(plog):
    """One or two early streams then a bind = benign; nothing above
    DEBUG, and the pending record clears."""
    s = _session_stub()
    s._subgroup_stream_by_key = {}
    s._data_streams = {}
    for i in range(2):
        s._admit_subgroup_stream(7 + i, _Hdr(alias=5, subgroup=i))
    assert 5 in s._unbound_aliases
    assert not [r for r in plog.records if r.levelname == "WARNING"]
    s._resolve_unbound_alias(5)
    assert 5 not in s._unbound_aliases


def test_never_bound_alias_escalates_once(plog):
    """Many streams over a long window with no bind = real defect."""
    import time as _t
    s = _session_stub()
    s._subgroup_stream_by_key = {}
    s._data_streams = {}
    for i in range(s.UNBOUND_ALIAS_MAX_STREAMS + 2):
        s._admit_subgroup_stream(7 + i, _Hdr(alias=9, subgroup=i))
        # age the record past the grace window
        s._unbound_aliases[9][0] = _t.monotonic() - (
            s.UNBOUND_ALIAS_GRACE_S + 1)
    warns = [r for r in plog.records
             if r.levelname == "WARNING" and "still unbound" in r.message]
    assert len(warns) == 1, f"expected exactly one escalation, got {len(warns)}"
    assert "track_alias=9" in warns[0].message


def test_streams_alone_do_not_escalate(plog):
    """Many streams inside the grace window is still just a fast race."""
    s = _session_stub()
    s._subgroup_stream_by_key = {}
    s._data_streams = {}
    for i in range(s.UNBOUND_ALIAS_MAX_STREAMS + 3):
        s._admit_subgroup_stream(7 + i, _Hdr(alias=11, subgroup=i))
    assert not [r for r in plog.records
                if r.levelname == "WARNING" and "still unbound" in r.message]
