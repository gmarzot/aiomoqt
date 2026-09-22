"""A peer's RESET_STREAM error code reaches the application.

Existing coverage drives `_cleanup_stream` / `on_stream_end` directly,
which proves the dispatch but not that the code survives the wire. A
subscriber has to tell an upstream DELIVERY_TIMEOUT from its own
teardown, so the value matters, not just the callback.

Checked on both transports: raw QUIC resets the stream natively, while
WebTransport carries it through the h3 session.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.messages import SubgroupHeader
from aiomoqt.track import SubscribedTrack
from aiomoqt.types import StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14980


def _resetting_publisher(code):
    """SUBSCRIBE handler: one object on a subgroup stream, then reset
    that stream with `code`."""
    async def _on_subscribe(session, msg):
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
        # Let the object land before the reset, so the subscriber is
        # reading a bound stream rather than an unparsed one.
        await asyncio.sleep(0.2)
        session.stream_reset(sid, int(code))
    return _on_subscribe


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize(
    "code",
    [StreamResetCode.DELIVERY_TIMEOUT, StreamResetCode.TOO_FAR_BEHIND],
    ids=["delivery_timeout", "too_far_behind"])
async def test_peer_reset_code_reaches_the_application(use_quic, code):
    from aiomoqt.types import MOQTMessageType
    port = _BASE_PORT + (0 if use_quic else 1) + 2 * int(code)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.SUBSCRIBE,
                            _resetting_publisher(code))
    handle = await server.serve()
    ended = []
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            sub = SubscribedTrack(session, "reset/ns", "t")
            sub._quiet = True
            await sub.subscribe(timeout=8.0)
            session.register_stream_end_handler(
                sub.track_alias,
                lambda g, sg, clean, reset_code: ended.append(
                    (clean, reset_code)))
            for _ in range(200):
                if ended:
                    break
                await asyncio.sleep(0.02)
    finally:
        handle.close()

    assert ended, (
        f"no stream end seen on {'quic' if use_quic else 'wt'}; the peer's "
        f"reset never reached the application")
    clean, seen = ended[0]
    assert clean is False, "a peer reset was reported as a clean end"
    assert seen == int(code), (
        f"reset code {seen} reached the application, peer sent {int(code)}")
