"""Loopback draft-18 object round-trip (publish -> subscribe), no relay.

Codifies the SUBGROUP object delivery that was otherwise only proven against
the live fb.mvfst.net d18 relay. A loopback MOQTServer (draft-18) answers
SUBSCRIBE by emitting N subgroup objects on a uni data stream; the client
receives them via on_object_received. This exercises the d18 vi64 data plane
end to end in-process:

  * group_id is 100 (>= 64), so a small-value/RFC9000 coincidence cannot pass
    it — the SUBGROUP_HEADER must be vi64-encoded for the group to round-trip;
  * object payloads are byte-verified;
  * the control plane (uni-pair SETUP, bidi SUBSCRIBE/SUBSCRIBE_OK) runs
    underneath.

d18 is gated behind AIOMOQT_ENABLE_D18; WebTransport d18 control is deferred,
so this is raw-QUIC only.
"""
import asyncio
import os

import pytest

from aiomoqt.types import MOQTMessageType
from aiomoqt.messages import Subscribe
from aiomoqt.messages.track import SubgroupHeader
from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer

_CERT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'certs')
CERT = os.path.realpath(os.path.join(_CERT_DIR, 'cert.pem'))
KEY = os.path.realpath(os.path.join(_CERT_DIR, 'key.pem'))

pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)

_BASE_PORT = 14490
_N_OBJECTS = 6
_GROUP = 100          # >= 64 → forces real vi64 in the SUBGROUP_HEADER
_OBJ_SIZE = 64


def _make_subscribe_handler(n_objects):
    async def _handle_subscribe(session, msg: Subscribe):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        track_alias = ok.track_alias
        stream_id = await session.open_uni_stream()
        # draft=session._draft selects the vi64 codec for the header/objects.
        header = SubgroupHeader(
            track_alias=track_alias, group_id=_GROUP, subgroup_id=0,
            publisher_priority=128, extensions_present=False,
            draft=session._draft,
        )
        session.stream_write(stream_id, header.serialize().data)
        for obj_id in range(n_objects):
            payload = f"d18-{obj_id}".encode().ljust(_OBJ_SIZE, b'\x00')
            buf = header.next_object(payload=payload, object_id=obj_id)
            session.stream_write(stream_id, buf.data)
        session.stream_write(stream_id, b'', end_stream=True)

    return _handle_subscribe


@pytest.mark.asyncio
async def test_d18_subscribe_object_roundtrip_quic(monkeypatch):
    monkeypatch.setenv("AIOMOQT_ENABLE_D18", "1")
    port = _BASE_PORT + 1

    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, draft_version=18,
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE, _make_subscribe_handler(_N_OBJECTS))
    server = await server.serve()

    received = []

    def on_obj(msg, size, ts, group_id, subgroup_id):
        # Callback contract: msg is valid only until the next call — copy now.
        received.append((group_id, msg.object_id, bytes(msg.payload)))

    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True,
            verify_tls=False, draft_version=18,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._draft == 18
            session.on_object_received = on_obj
            await session.subscribe("test/ns", "clock", wait_response=True)
            for _ in range(100):
                if len(received) >= _N_OBJECTS:
                    break
                await asyncio.sleep(0.02)
    finally:
        server.close()

    assert len(received) == _N_OBJECTS, f"got {len(received)}/{_N_OBJECTS}"
    for i, (group_id, object_id, payload) in enumerate(
            sorted(received, key=lambda r: r[1])):
        # group 100 >= 64 round-tripped → the SUBGROUP_HEADER was vi64.
        assert group_id == _GROUP, f"group {group_id} != {_GROUP}"
        assert object_id == i, f"object_id {object_id} != {i}"
        assert payload.startswith(f"d18-{i}".encode()), f"payload {payload!r}"
