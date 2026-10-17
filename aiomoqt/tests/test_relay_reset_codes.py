"""The relay forwards an upstream subgroup-stream reset by meaning: the
code is re-encoded for each subscriber's draft."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import SubscribedTrack
from aiomoqt.types import MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 17570
_NS = "resets/live"
# (publisher draft, subscriber draft): {track: (upstream wire, downstream wire)}
_CASES = {
    (16, 18): {"a": (0x4, 0x6),    # UNKNOWN_OBJECT_STATUS, not GOING_AWAY
               "b": (0x2, 0x2)},   # DELIVERY_TIMEOUT
    (18, 16): {"a": (0x6, 0x4),    # UNKNOWN_OBJECT_STATUS
               "b": (0x4, 0x0)},   # GOING_AWAY: d16 lacks it
}


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture(autouse=True)
def _clean_relay():
    _clear_relay()
    yield
    _clear_relay()


def _resetting_publisher(codes):
    """One object per track on a subgroup stream, then reset that stream
    with the track's raw wire code."""
    async def on_subscribe(session, msg):
        wire = codes[msg.track_name.decode()][0]
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(
            track_alias=ok.track_alias, group_id=0, subgroup_id=0,
            publisher_priority=128, extensions_present=False,
            prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        await session.stream_write_drain(
            sid, hdr.next_object(payload=b"x", extensions=None,
                                 object_id=0).data)
        await asyncio.sleep(0.3)
        session.stream_reset(sid, wire)
    return on_subscribe


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("drafts", list(_CASES), ids=["d16-d18", "d18-d16"])
async def test_an_upstream_reset_reaches_the_subscriber_in_its_draft(
        use_quic, drafts):
    codes = _CASES[drafts]
    port = _PORT + 2 * list(_CASES).index(drafts) + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=[18, 16])
    handle = await server.serve()
    ended = {}
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect(draft):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            pub = await connect(drafts[0])
            pub.register_handler(MOQTMessageType.SUBSCRIBE,
                                 _resetting_publisher(codes))
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub = await connect(drafts[1])
            assert (pub.negotiated_draft, sub.negotiated_draft) == drafts
            for name in codes:
                track = SubscribedTrack(sub, _NS, name)
                track._quiet = True
                await track.subscribe(timeout=8.0)
                sub.register_stream_end_handler(
                    track.track_alias,
                    lambda g, sg, clean, reset_code, n=name:
                        ended.setdefault(n, (clean, reset_code)))
            for _ in range(200):
                if len(ended) == len(codes):
                    break
                await asyncio.sleep(0.02)
    finally:
        handle.close()
    assert ended == {name: (False, down)
                     for name, (_, down) in codes.items()}
