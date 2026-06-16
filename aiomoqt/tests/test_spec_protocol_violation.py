"""Spec-compliant bounded parsing (draft-16 §9 line 2268).

The control-frame Length is the authoritative message extent: parameters
that would read past it are a wire violation, surfaced as
MOQTProtocolViolation (→ session close PROTOCOL_VIOLATION) rather than a
raw BufferReadError. Draft-agnostic; exercised under both supported
drafts.
"""
import pytest

from aiomoqt.messages.base import MOQTMessage
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
        MOQTMessage._deserialize_params(buf, draft=draft, buf_end=end)


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
        MOQTMessage._deserialize_params(buf, draft=draft, buf_end=end)


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
        ServerSetup.deserialize(buf, draft=16, buf_end=end)


@pytest.mark.parametrize("draft", [14, 16])
def test_well_formed_params_round_trip(draft):
    """Sanity: params that fit exactly within buf_end parse cleanly
    (encode + decode under the same draft so key coding matches)."""
    buf = Buffer(capacity=64)
    MOQTMessage._serialize_params(buf, {0x20: 7, 0x22: 1}, draft=draft)
    end = buf.tell()
    buf.seek(0)
    params = MOQTMessage._deserialize_params(buf, draft=draft, buf_end=end)
    assert params == {0x20: 7, 0x22: 1}
