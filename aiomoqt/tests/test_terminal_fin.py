"""d18 terminal replies FIN our half of the request stream: REQUEST_ERROR
(§3.3.2), PUBLISH_DONE (§10.11), TRACK_STATUS replies (§10.14). Non-
terminal replies and pre-d18 control-stream replies do not."""
import asyncio
from collections import deque

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.context import profile_for
from aiomoqt.messages.namespace import SubscribeNamespace
from aiomoqt.messages.subscribe import TrackStatus
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.types import MOQTMessageType, MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 14795


def _stub(draft):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._bidi_streams = {5: 40}
    s._sent_requests = deque(maxlen=8)
    s._pending_requests = {}
    s._track_aliases = {}
    s._tx_updates = {}
    s.writes = []
    s.control = []
    s.stream_write = lambda sid, data, end_stream=False: \
        s.writes.append((sid, bytes(data), end_stream))
    s.send_control_message = lambda msg: s.control.append(type(msg).__name__)
    return s


def _fins(s):
    return [sid for sid, data, fin in s.writes if fin]


def test_d18_terminal_replies_fin_the_request_stream():
    s = _stub(18)
    s.subscribe_done(5)
    assert _fins(s) == [40] and s.writes[0][2] is False   # message, then FIN
    s = _stub(18)
    s.subscribe_error(5, RequestErrorCode.NOT_SUPPORTED, "no")
    assert _fins(s) == [40]
    s = _stub(18)
    s.fetch_error(5, RequestErrorCode.NOT_SUPPORTED, "no")
    assert _fins(s) == [40]


@pytest.mark.asyncio
async def test_d18_track_status_reply_fins_the_request_stream():
    s = _stub(18)
    await s._handle_track_status(TrackStatus(request_id=5, track_namespace=(b"n",),
                                             track_name=b"t"))
    assert _fins(s) == [40]


def test_d18_non_terminal_replies_keep_the_stream_open():
    s = _stub(18)
    s.subscribe_namespace_ok(SubscribeNamespace(request_id=5,
                                                namespace_prefix=(b"n",)))
    assert s.writes and _fins(s) == []


def test_pre_d18_replies_ride_the_control_stream_without_fin():
    s = _stub(16)
    s.subscribe_done(5)
    s.subscribe_error(5, RequestErrorCode.NOT_SUPPORTED, "no")
    assert s.control == ["SubscribeDone", "RequestError"] and s.writes == []


def _server(port):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=18)

    async def _on_subscribe(session, msg):
        if msg.track_name == b"reject":
            session.subscribe_error(msg.request_id,
                                    RequestErrorCode.NOT_SUPPORTED, "nope")
            return
        session.subscribe_ok(request_msg=msg, content_exists=0)
        await asyncio.sleep(0.02)
        session.subscribe_done(msg.request_id)

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    return server


@requires_certs
@pytest.mark.asyncio
async def test_subscriber_sees_the_fin_after_error_and_after_publish_done():
    server = await _server(_PORT).serve()
    try:
        client = MOQTClient("localhost", _PORT, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            fins = []
            inner = session._on_control_data

            def _spy(stream_id, data, end_stream, **kw):
                if end_stream:
                    fins.append(stream_id)
                return inner(stream_id, data, end_stream, **kw)

            session._on_control_data = _spy
            with pytest.raises(MOQTRequestError):
                await session.subscribe("ns", "reject", wait_response=True)
            rejected = max(session._bidi_streams.values())
            await session.subscribe("ns", "ok", wait_response=True)
            done = max(session._bidi_streams.values())
            for _ in range(100):
                if rejected in fins and done in fins:
                    break
                await asyncio.sleep(0.02)
            assert rejected in fins and done in fins
            assert session._close_err is None
    finally:
        server.close()
