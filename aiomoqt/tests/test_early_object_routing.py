"""An object that arrives before its track's SUBSCRIBE_OK reaches that
track once the reply binds it, not another track's callback. Two tracks on
one session, over raw QUIC and WebTransport."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.server import MOQTServer
from aiomoqt.track import SubscribedTrack
from aiomoqt.types import MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 18300
_NS = "early/route"
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


async def _send_object(session, alias, payload):
    sid = await session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=alias, group_id=0, subgroup_id=0,
                         publisher_priority=128, extensions_present=False,
                         prof=session._profile)
    session.stream_write(sid, hdr.serialize().data)
    await session.stream_write_drain(
        sid, hdr.next_object(payload=payload, extensions=None,
                             object_id=0).data)


def _publisher(aliases):
    """Track a's object goes out on its alias before its SUBSCRIBE_OK;
    track b is answered after a, then sends its object."""
    a_bound = asyncio.Event()

    async def on_subscribe(session, msg):
        name = msg.track_name.decode()
        if name == "a":
            early = session._next_track_alias    # what subscribe_ok assigns
            await _send_object(session, early, b"a")
            await asyncio.sleep(0.3)
            ok = session.subscribe_ok(request_msg=msg, content_exists=0)
            aliases["a"] = (early, ok.track_alias)
            a_bound.set()
        else:
            await a_bound.wait()
            ok = session.subscribe_ok(request_msg=msg, content_exists=0)
            aliases["b"] = ok.track_alias
            await _send_object(session, ok.track_alias, b"b")
    return on_subscribe


@_TRANSPORTS
@pytest.mark.asyncio
async def test_an_early_object_reaches_its_own_track(use_quic):
    port = _BASE_PORT + (not use_quic)
    aliases, got = {}, {}
    moqt = MOQTServer(host="localhost", port=port, certificate=CERT,
                      private_key=KEY, path="/", use_quic=use_quic,
                      supported_drafts=18)
    moqt.register_handler(MOQTMessageType.SUBSCRIBE, _publisher(aliases))
    server = await moqt.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            tracks = [SubscribedTrack(
                session, _NS, name,
                on_object=lambda obj, *a, n=name: got.setdefault(
                    n, []).append(bytes(obj.payload)))
                for name in ("a", "b")]
            for track in tracks:
                track._quiet = True
            await asyncio.gather(*(t.subscribe(timeout=8.0) for t in tracks))
            for _ in range(100):
                if sum(len(v) for v in got.values()) >= 2:
                    break
                await asyncio.sleep(0.02)
    finally:
        server.close()
    early, bound = aliases["a"]
    assert early == bound                    # the object really was early
    assert got == {"a": [b"a"], "b": [b"b"]}
