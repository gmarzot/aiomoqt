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
from aiomoqt.messages.track import ObjectDatagram

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
    # Flush the OK ahead of data: the §10.4.2 data-before-OK race is
    # tolerated-with-warning by design; this test asserts the post-OK
    # steady state (silent admission under the authoritative alias).
    session.transmit()
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
    session.transmit()


async def _on_subscribe_datagrams(session, msg):
    session._next_track_alias = max(session._next_track_alias, _ALIAS_BASE)
    ok = session.subscribe_ok(request_msg=msg, content_exists=0)
    for i in range(_N_OBJECTS):
        dgram = ObjectDatagram(track_alias=ok.track_alias, group_id=0,
                               object_id=i, payload=f"dgram-{i}".encode())
        session.send_dgram_message(dgram.serialize(prof=session._profile))
    session.transmit()


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
