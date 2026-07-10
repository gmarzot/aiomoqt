"""Control-send readiness: defer replies while the control write stream
comes up.

The d18 server brings its control write-uni up inside the SETUP handler
(an await); a pipelined client's request handlers can generate replies
during that window. send_control_message must defer (queue) instead of
failing the handler task, and deferred messages must flush AFTER our
SETUP so SETUP stays the first message on the control stream (peers bind
the control uni by peeking SETUP). Regression for the moq-dev-rs →
aiomoqt-relay "control task failed with exception: control stream not
initialized" interop failure.
"""
import pytest

from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.messages.request import RequestOk
from aiomoqt.messages.d18.session_setup import Setup
from aiomoqt.types import MOQTException
from aiomoqt.context import profile_for


def _send_session(draft=18):
    """Minimal session for exercising send_control_message in isolation:
    no control write stream yet, a recording _quic stub (bypasses
    __init__ / transport)."""
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._d18_control_write_sid = None
    s._control_stream_id = None
    s._pending_control_msgs = []
    s._writes = []

    class _Quic:
        def send_stream_data(self, stream_id, data, end_stream=False):
            s._writes.append((stream_id, bytes(data)))

    s._quic = _Quic()
    return s


def _wire(msg, prof):
    return bytes(msg.serialize(prof=prof).data)


@pytest.mark.parametrize("draft", [14, 16, 18])
def test_defers_while_write_stream_pending(draft):
    # The race: a reply generated before the control write stream is up
    # must be deferred — not raise and kill the handler task.
    s = _send_session(draft)
    s.send_control_message(RequestOk(request_id=7, parameters={}))
    assert s._writes == []                       # nothing hit the wire
    assert len(s._pending_control_msgs) == 1     # deferred


def test_flush_after_setup_keeps_setup_first():
    # d18 server bring-up order: reply deferred during the SETUP handler's
    # stream-open await; then the write-uni latches, SETUP is sent, and
    # the deferred reply flushes — SETUP first on the stream.
    s = _send_session(18)
    reply = RequestOk(request_id=7, parameters={})
    s.send_control_message(reply)                # races the bring-up
    assert s._writes == []

    s._d18_control_write_sid = 3                 # write-uni latched
    setup = Setup(options={})
    s.send_control_message(setup)                # SETUP goes out
    s._flush_pending_control()                   # then the deferred reply

    assert [w[0] for w in s._writes] == [3, 3]
    assert s._writes[0][1] == _wire(setup, s._profile)
    assert s._writes[1][1] == _wire(reply, s._profile)
    assert s._pending_control_msgs == []


def test_flush_preserves_defer_order():
    s = _send_session(18)
    msgs = [RequestOk(request_id=i, parameters={}) for i in range(3)]
    for m in msgs:
        s.send_control_message(m)
    s._d18_control_write_sid = 3
    s._flush_pending_control()
    assert [w[1] for w in s._writes] == [_wire(m, s._profile) for m in msgs]


def test_dead_session_still_raises():
    s = _send_session(18)
    s._quic = None
    with pytest.raises(MOQTException):
        s.send_control_message(RequestOk(request_id=7, parameters={}))


def test_defer_queue_is_bounded():
    # A peer flooding requests pre-SETUP must not grow the queue
    # unbounded — past the cap the old hard failure returns.
    s = _send_session(18)
    for i in range(s._PENDING_CONTROL_MAX):
        s.send_control_message(RequestOk(request_id=i, parameters={}))
    with pytest.raises(MOQTException):
        s.send_control_message(RequestOk(request_id=9999, parameters={}))


def test_ready_stream_sends_immediately():
    # No deferral when the write stream is already up.
    s = _send_session(18)
    s._d18_control_write_sid = 3
    msg = RequestOk(request_id=7, parameters={})
    s.send_control_message(msg)
    assert s._writes == [(3, _wire(msg, s._profile))]
    assert s._pending_control_msgs == []
