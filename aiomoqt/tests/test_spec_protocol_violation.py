"""Spec-compliant bounded parsing (draft-16 §9 line 2268).

The control-frame Length is the authoritative message extent: parameters
that would read past it are a wire violation, surfaced as
MOQTProtocolViolation (→ session close PROTOCOL_VIOLATION) rather than a
raw BufferReadError. Draft-agnostic; exercised under both supported
drafts.
"""
import pytest

from aiomoqt.messages.base import MOQTMessage
from aiomoqt.context import profile_for
from aiomoqt.messages.session_setup import ServerSetup
from aiomoqt.types import MOQTProtocolViolation, SessionCloseCode
from aiomoqt.utils.buffer import Buffer


def test_violation_maps_to_protocol_violation_code():
    exc = MOQTProtocolViolation("bad frame")
    assert exc.error_code == SessionCloseCode.PROTOCOL_VIOLATION


@pytest.mark.parametrize("draft", [14, 16])
def test_param_count_overrun_raises(draft):
    """Declared param count exceeds the bytes within buf_end."""
    buf = Buffer(capacity=64)
    buf.push_uint_var(3)         # declares 3 params...
    buf.push_uint_var(0x20)      # ...but only one (even type) fits
    buf.push_uint_var(1)
    end = buf.tell()             # frame ends here
    buf.push_uint_var(0x22)      # bytes past the frame (next message)
    buf.push_uint_var(2)
    buf.seek(0)
    with pytest.raises(MOQTProtocolViolation):
        MOQTMessage._deserialize_params(buf, prof=profile_for(draft), buf_end=end)


@pytest.mark.parametrize("draft", [14, 16])
def test_param_length_overrun_raises(draft):
    """A length-prefixed param whose Length runs past buf_end."""
    buf = Buffer(capacity=64)
    buf.push_uint_var(1)         # one param
    buf.push_uint_var(0x21)      # odd type → length-prefixed value
    buf.push_uint_var(100)       # claims 100 bytes...
    buf.push_bytes(b'xxxx')      # ...only 4 present within the frame
    end = buf.tell()
    buf.seek(0)
    with pytest.raises(MOQTProtocolViolation):
        MOQTMessage._deserialize_params(buf, prof=profile_for(draft), buf_end=end)


def test_setup_with_short_length_raises():
    """A SERVER_SETUP whose params overrun the frame length closes the
    session as a protocol violation, not a BufferReadError."""
    buf = Buffer(capacity=64)
    # d16 SERVER_SETUP body = params only (no version field).
    buf.push_uint_var(5)         # NumParams lies (5)...
    buf.push_uint_var(0x20)      # ...one real param
    buf.push_uint_var(1)
    end = buf.tell()
    buf.push_uint_var(0x22)      # trailing bytes beyond the declared frame
    buf.push_uint_var(2)
    buf.seek(0)
    with pytest.raises(MOQTProtocolViolation):
        ServerSetup.deserialize(buf, prof=profile_for(16), buf_end=end)


@pytest.mark.parametrize("draft", [14, 16])
def test_well_formed_params_round_trip(draft):
    """Sanity: params that fit exactly within buf_end parse cleanly
    (encode + decode under the same draft so key coding matches)."""
    buf = Buffer(capacity=64)
    MOQTMessage._serialize_params(buf, {0x20: 7, 0x22: 1}, prof=profile_for(draft))
    end = buf.tell()
    buf.seek(0)
    params = MOQTMessage._deserialize_params(buf, prof=profile_for(draft), buf_end=end)
    assert params == {0x20: 7, 0x22: 1}


# -- malformed control messages through a live session ------------------

import asyncio  # noqa: E402

from aiomoqt.client import MOQTClient  # noqa: E402
from aiomoqt.messages.subscribe import SubscribeOk  # noqa: E402
from aiomoqt.protocol import _MOQTSessionMixin  # noqa: E402
from aiomoqt.server import MOQTServer  # noqa: E402
from aiomoqt.tests._certs import CERT, KEY, requires_certs  # noqa: E402

_PORT = 23700


def _wire(draft, msg_type, body):
    """A control message: type, 16-bit Length, body."""
    buf = Buffer(capacity=len(body) + 16, vi64=profile_for(draft).vi64)
    buf.push_vint(msg_type)
    buf.push_uint16(len(body))
    buf.push_bytes(body)
    return bytes(buf.data_slice(0, buf.tell()))


def _truncated_track_extension():
    """d16 SUBSCRIBE_OK whose trailing Track Extension declares a value
    longer than the message."""
    raw = bytes(SubscribeOk(request_id=0, track_alias=1,
                            track_extensions={0x3B: b"abcdef"}
                            ).serialize(prof=profile_for(16)).data)
    return _wire(16, 0x04, raw[3:-3])


_MALFORMED = {
    # Invalid UTF-8 in the New Session URI.
    "goaway-uri": (16, False, lambda: _wire(16, 0x10, b"\x04\xff\xfe\xfd\xfc")),
    # Group Order 9 in a d14 TRACK_STATUS_OK.
    "group-order": (14, False, lambda: _wire(
        14, 0x0E, bytes([0, 1, 0, 9, 0, 0]))),
    "track-extension": (16, False, _truncated_track_extension),
    # A namespace field declaring 2^63 bytes, on a d18 request stream.
    "namespace-length": (18, True, lambda: _wire(
        18, 0x03, b"\x00\x01\xff" + (1 << 63).to_bytes(8, "big"))),
}


@requires_certs
@pytest.mark.parametrize("case", list(_MALFORMED))
async def test_malformed_control_message_closes_the_session_once(
        case, monkeypatch, caplog):
    draft, request_stream, build = _MALFORMED[case]
    closes = []
    orig = _MOQTSessionMixin._close_session

    def record(self, code=SessionCloseCode.NO_ERROR, reason="no error",
               **kw):
        if not self._is_client and kw.get("transmit", True):
            closes.append((code, reason))
        return orig(self, code, reason, **kw)
    monkeypatch.setattr(_MOQTSessionMixin, "_close_session", record)
    monkeypatch.setattr(MOQTMessage, "_tolerate_trailing_extensions", False)

    port = _PORT + list(_MALFORMED).index(case)
    server = await MOQTServer(host="localhost", port=port, certificate=CERT,
                              private_key=KEY, path="/", use_quic=True).serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            if request_stream:
                sid = await session.open_bidi_stream()
                session.stream_write(sid, build())
            else:
                session._quic.send_stream_data(
                    stream_id=session._control_write_stream_id,
                    data=build(), end_stream=False)
            for _ in range(150):
                if session._close_err is not None:
                    break
                await asyncio.sleep(0.02)
            assert session._close_err is not None
            assert session._close_err[0] == SessionCloseCode.PROTOCOL_VIOLATION
    finally:
        server.close()
    # One close, sent by the server, with a short reason.
    assert len(closes) == 1, closes
    code, reason = closes[0]
    assert code == SessionCloseCode.PROTOCOL_VIOLATION
    assert 0 < len(reason) < 120 and "\n" not in reason
    assert "traceback" not in caplog.text.lower()


def test_decode_reports_an_unexpected_fault_as_a_protocol_violation(
        monkeypatch):
    def broken(cls, buf, **kw):
        raise IndexError("tuple index out of range")
    monkeypatch.setattr(SubscribeOk, "deserialize", classmethod(broken))
    with pytest.raises(MOQTProtocolViolation, match="malformed SubscribeOk"):
        SubscribeOk.decode(Buffer(data=b"\x00"), prof=profile_for(16),
                           buf_end=1)
