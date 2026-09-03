"""TRACK_STATUS must be answered (§10.14): exactly one TRACK_STATUS_OK
or REQUEST_ERROR, on the request's own stream at d18. The default
session handler answers NOT_SUPPORTED; the relay answers from its
track table."""
import asyncio

from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.messages.subscribe import TrackStatus
from aiomoqt.context import profile_for
from aiomoqt.types import D16MessageType, RequestErrorCode
from aiopquic.buffer import Buffer


def _session(draft=18):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._bidi_streams = {5: 9}
    s._writes = []
    s.stream_write = lambda sid, data, **kw: s._writes.append(
        (sid, bytes(data)))
    return s


def _reply_type(raw, prof):
    r = Buffer(data=raw, vi64=prof.vi64)
    return r.pull_vint()


def test_default_handler_answers_request_error_on_the_request_stream():
    s = _session(18)
    msg = TrackStatus(request_id=5, track_namespace=(b"ns",),
                      track_name=b"t")
    asyncio.run(s._handle_track_status(msg))
    assert len(s._writes) == 1
    sid, raw = s._writes[0]
    assert sid == 9                      # the request's bidi stream
    assert _reply_type(raw, s._profile) == 0x05  # REQUEST_ERROR


def test_relay_answers_ok_for_served_track_and_error_for_unknown():
    from aiomoqt.tools import moq_interop_relay as relay

    class _Track:
        pending_publish = ("sess", "msg")
        upstream = type("U", (), {"_largest": (7, 42)})()

    s = _session(18)
    saved = dict(relay._tracks)
    relay._tracks.clear()
    relay._tracks[((b"live",), b"cam")] = _Track()
    try:
        ok_msg = TrackStatus(request_id=5, track_namespace=(b"live",),
                             track_name=b"cam")
        asyncio.run(relay._on_track_status(s, ok_msg))
        assert _reply_type(s._writes[-1][1], s._profile) == \
            int(D16MessageType.REQUEST_OK)

        miss = TrackStatus(request_id=5, track_namespace=(b"nope",),
                           track_name=b"x")
        asyncio.run(relay._on_track_status(s, miss))
        sid, raw = s._writes[-1]
        r = Buffer(data=raw, vi64=True)
        assert r.pull_vint() == 0x05     # REQUEST_ERROR
        r.pull_uint16()
        assert r.pull_vint() == int(RequestErrorCode.DOES_NOT_EXIST)
    finally:
        relay._tracks.clear()
        relay._tracks.update(saved)
