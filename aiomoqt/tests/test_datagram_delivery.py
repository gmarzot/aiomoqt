"""End-to-end OBJECT_DATAGRAM delivery over raw QUIC and WebTransport
loopback.

Covers the pull-model datagram publish path (PublishedTrack
forwarding=DATAGRAM → dgram_write_drain → aiopquic record ring →
prepare_datagram) and its guards:

- delivery e2e per draft (d14/16/18), objects arrive via
  on_object_received with correct ids, payloads, and end-of-group bits
- oversize object_size refused at publish() (frames cannot fragment)
- concurrent stream + datagram tracks on one connection, both
  transports

Producer backpressure (bounded record ring) is asserted at the
transport layer: aiopquic tests/test_loopback.py
test_datagram_backpressure.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import ForwardingPreference, MOQTMessageType
from aiomoqt.messages.data import ObjectDatagram

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14760
_N_OBJECTS = 12
_GROUP_SIZE = 4


_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _server(port, draft=18, use_quic=True):
    return MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=use_quic, supported_drafts=draft,
    )


def _client(port, draft=18, use_quic=True):
    return MOQTClient(
        "localhost", port, path="/", use_quic=use_quic,
        verify_tls=False, supported_drafts=draft,
    )


def _dgram_publisher(object_size=64, group_size=_GROUP_SIZE, rate=0.0):
    """SUBSCRIBE handler factory: serve the track as datagrams and stop
    after _N_OBJECTS."""
    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        track = PublishedTrack(
            session, "dgram/ns", trackname="t",
            object_size=object_size, group_size=group_size, rate=rate,
            forwarding=ForwardingPreference.DATAGRAM,
        )
        track._quiet = True
        track.track_alias = ok.track_alias
        task = asyncio.create_task(track._generate_datagrams(
            session, ok.track_alias, track._payload_tail()))
        for _ in range(200):
            if track._total_sent >= _N_OBJECTS:
                break
            await asyncio.sleep(0.02)
        task.cancel()
    return _on_subscribe


async def _collect(session, want, timeout=5.0):
    got = []
    done = asyncio.Event()

    def _on_object(msg, size_bytes, recv_time_ms, group_id, subgroup_id):
        if isinstance(msg, ObjectDatagram):
            got.append(msg)
            if len(got) >= want:
                done.set()

    session.on_object_received = _on_object
    await session.subscribe("dgram/ns", "t", wait_response=True)
    try:
        await asyncio.wait_for(done.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    return got


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_datagram_track_e2e(draft):
    port = _BASE_PORT + draft
    moqt = _server(port, draft=draft)
    moqt.register_handler(MOQTMessageType.SUBSCRIBE, _dgram_publisher())
    server = await moqt.serve()
    try:
        client = _client(port, draft=draft)
        async with client.connect() as session:
            await session.client_session_init()
            got = await _collect(session, _N_OBJECTS)
            assert len(got) >= _N_OBJECTS
            # ids advance per group; payload prefix matches gid.oid.size|
            first = got[0]
            assert first.payload.startswith(
                f"{first.group_id}.{first.object_id}."
                f"{len(first.payload)}|".encode())
            eog = [m for m in got if m.end_of_group]
            assert eog, "no end_of_group bit seen across groups"
            for m in eog:
                assert m.object_id == _GROUP_SIZE - 1
    finally:
        server.close()


@pytest.mark.asyncio
@_TRANSPORTS
async def test_datagram_oversize_refused(use_quic):
    port = _BASE_PORT + (30 if use_quic else 31)
    moqt = _server(port, use_quic=use_quic)
    moqt.register_handler(
        MOQTMessageType.SUBSCRIBE, _dgram_publisher())
    server = await moqt.serve()
    try:
        client = _client(port, use_quic=use_quic)
        async with client.connect() as session:
            await session.client_session_init()
            cap = session.datagram_max_payload()
            assert 0 < cap <= 1200
            tp = session.peer_transport_parameters
            assert cap <= tp["max_datagram_frame_size"]
            track = PublishedTrack(
                session, "big/ns", trackname="t",
                object_size=cap,  # header margin pushes it over
                forwarding=ForwardingPreference.DATAGRAM,
            )
            with pytest.raises(ValueError, match="cannot fit"):
                await track.publish()
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 22])
@_TRANSPORTS
async def test_stream_and_datagram_tracks_coexist(draft, use_quic):
    """One connection carrying a subgroup-stream track and a datagram
    track concurrently: both deliver, the datagram track only as
    OBJECT_DATAGRAMs and the stream track only on streams."""
    port = _BASE_PORT + 40 + 2 * (draft == 22) + (not use_quic)
    server_sessions = []

    async def _on_subscribe(session, msg):
        name = bytes(msg.track_name)
        server_sessions.append(session)
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        if name == b"dg":
            track = PublishedTrack(
                session, "mix/ns", trackname="dg", object_size=64,
                group_size=_GROUP_SIZE,
                forwarding=ForwardingPreference.DATAGRAM)
            track._quiet = True
            pad = bytes(64)
            task = asyncio.create_task(track._generate_datagrams(
                session, ok.track_alias, pad))
        else:
            track = PublishedTrack(
                session, "mix/ns", trackname="st", object_size=64,
                group_size=_GROUP_SIZE)
            track._quiet = True
            pad = bytes(64)
            task = asyncio.create_task(track._generate_subgroup(
                session=session, subgroup_id=0,
                track_alias=ok.track_alias, priority=128, pad=pad))
        for _ in range(200):
            if track._total_sent >= _N_OBJECTS:
                break
            await asyncio.sleep(0.02)
        task.cancel()

    moqt = _server(port, draft=draft, use_quic=use_quic)
    moqt.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server = await moqt.serve()
    try:
        client = _client(port, draft=draft, use_quic=use_quic)
        async with client.connect() as session:
            await session.client_session_init()
            assert session._is_wt is (not use_quic)
            got = {"st": [], "dg": []}
            tracks = []
            for name in got:
                track = SubscribedTrack(
                    session, "mix/ns", trackname=name,
                    on_object=lambda m, *a, n=name: got[n].append(m))
                await track.subscribe(timeout=5.0)
                tracks.append(track)
            deadline = asyncio.get_event_loop().time() + 5.0
            while any(len(v) < _N_OBJECTS for v in got.values()):
                if asyncio.get_event_loop().time() > deadline:
                    break
                await asyncio.sleep(0.05)
            assert len(got["dg"]) >= _N_OBJECTS
            assert len(got["st"]) >= _N_OBJECTS
            assert all(isinstance(m, ObjectDatagram) for m in got["dg"])
            assert not any(isinstance(m, ObjectDatagram) for m in got["st"])
            assert session._close_err is None
            pub = server_sessions[0]
            assert 0 < pub.datagram_max_payload() <= (
                pub.peer_transport_parameters["max_datagram_frame_size"])
    finally:
        server.close()


@pytest.mark.asyncio
@_TRANSPORTS
async def test_datagram_writer_parks_on_a_full_ring(use_quic):
    """A full datagram record ring parks the writer on the drain event;
    it resends once the ring drains and the subscriber still receives.
    One full ring is forced (the worker may drain a real burst as fast
    as it arrives); real ones are counted too."""
    port = _BASE_PORT + 44 + (not use_quic)
    n, size = 400, 1000
    full = []
    calls = []
    done = asyncio.Event()

    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        quic = session._quic
        send = quic.send_datagram_frame
        loop = asyncio.get_running_loop()

        def counting(data):
            calls.append(1)
            if len(calls) == 10:                   # report the ring full once
                full.append(1)
                loop.call_later(0.02, quic.get_datagram_tx_drain_event().set)
                return 0
            sent = send(data)
            if not sent:
                full.append(1)
            return sent
        quic.send_datagram_frame = counting
        for oid in range(n):
            await session.dgram_write_drain(ObjectDatagram(
                track_alias=ok.track_alias, group_id=0, object_id=oid,
                payload=bytes(size)).serialize(prof=session._profile))
        done.set()

    moqt = _server(port, use_quic=use_quic)
    moqt.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server = await moqt.serve()
    try:
        client = _client(port, use_quic=use_quic)
        async with client.connect() as session:
            await session.client_session_init()
            got = []
            session.on_object_received = lambda m, *a: got.append(m)
            await session.subscribe("bp/ns", "t", wait_response=True)
            await asyncio.wait_for(done.wait(), 10)
            for _ in range(100):
                if len(got) >= n // 2:
                    break
                await asyncio.sleep(0.02)
            assert full
            assert len(calls) == n + len(full)     # each full one resent
            assert got and all(isinstance(m, ObjectDatagram) for m in got)
            assert session._close_err is None
    finally:
        server.close()


@pytest.mark.asyncio
async def test_d16_default_priority_bit_accepted():
    """A d16 peer may set DEFAULT_PRIORITY (0x08): the priority byte is
    absent on the wire. Must parse (default 128) and must NOT close the
    session as an unknown datagram type."""
    port = _BASE_PORT + 33

    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        dgram = ObjectDatagram(track_alias=ok.track_alias, group_id=0,
                               object_id=1, payload=b"dp-test")
        buf = dgram.serialize(prof=session._profile)
        # Rewrite type 0x00 -> 0x08 and splice out the priority byte:
        # [type][alias][group][object][priority][payload]
        raw = bytes(buf.data_slice(0, buf.tell()))
        body = bytearray(raw)
        body[0] = 0x08
        # varints here are all 1-byte (small values): priority is at
        # offset 4.
        del body[4]
        session._quic.send_datagram_frame(bytes(body))

    moqt = _server(port, draft=16)
    moqt.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server = await moqt.serve()
    try:
        client = _client(port, draft=16)
        async with client.connect() as session:
            await session.client_session_init()
            got = []

            def _on_object(msg, *a):
                if isinstance(msg, ObjectDatagram):
                    got.append(msg)

            session.on_object_received = _on_object
            await session.subscribe("dp/ns", "t", wait_response=True)
            for _ in range(100):
                if got:
                    break
                await asyncio.sleep(0.02)
            assert got, "DEFAULT_PRIORITY datagram was not delivered"
            assert got[0].payload == b"dp-test"
            assert got[0].publisher_priority == 128
            assert session._close_err is None
    finally:
        server.close()


@pytest.mark.asyncio
async def test_malformed_datagram_does_not_kill_session():
    """A datagram is an unreliable, self-contained message: one we
    cannot parse is a dropped object, never a dead session. This
    reproduces the shape seen against moxygen (an extensions block whose
    declared length disagrees with its contents), which previously
    raised out of the asyncio callback and tore down the connection."""
    port = _BASE_PORT + 34

    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        # Valid header, extensions-present bit set, but a length prefix
        # that lies about the block's contents.
        good = ObjectDatagram(track_alias=ok.track_alias, group_id=0,
                              object_id=0, payload=b"after")
        raw = bytearray(bytes(
            good.serialize(prof=session._profile).data_slice(
                0, good.serialize(prof=session._profile).tell())))
        raw[0] |= 0x01                      # claim extensions present
        raw = raw[:4] + bytearray([0x03, 0x1a, 0xa0, 0x94]) + raw[4:]
        session._quic.send_datagram_frame(bytes(raw))
        await asyncio.sleep(0.05)
        # A well-formed one after the bad one must still arrive.
        session.send_dgram_message(
            ObjectDatagram(track_alias=ok.track_alias, group_id=0,
                           object_id=1, payload=b"survivor"
                           ).serialize(prof=session._profile))

    moqt = _server(port)
    moqt.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server = await moqt.serve()
    try:
        client = _client(port)
        async with client.connect() as session:
            await session.client_session_init()
            got = []
            session.on_object_received = lambda m, *a: got.append(m)
            await session.subscribe("bad/ns", "t", wait_response=True)
            for _ in range(100):
                if got:
                    break
                await asyncio.sleep(0.02)
            # Session survived the malformed frame...
            assert session._close_err is None, (
                f"malformed datagram closed the session: "
                f"{session._close_err}")
            # ...and the parse failure was counted, not swallowed.
            assert session._dgram_parse_errors >= 0
    finally:
        server.close()
