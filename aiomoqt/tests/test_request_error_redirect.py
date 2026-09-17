"""d18 REQUEST_ERROR with Error Code REDIRECT carries a Redirect structure
(§10.6.1); pre-d18 has neither the code nor the structure on the wire."""
from aiomoqt.context import profile_for
from aiomoqt.messages.request import RequestError
from aiomoqt.types import RequestErrorCode
from aiopquic.buffer import Buffer


def _round_trip(msg, draft):
    prof = profile_for(draft)
    raw = bytes(msg.serialize(prof=prof).data)
    buf = Buffer(data=raw, vi64=prof.vi64)
    buf.pull_vint()                      # type
    length = buf.pull_uint16()
    end = buf.tell() + length
    out = RequestError.deserialize(buf, prof=prof, buf_end=end)
    assert buf.tell() == end
    return out


def test_d18_redirect_round_trips():
    msg = RequestError(request_id=None, error_code=RequestErrorCode.REDIRECT,
                       retry_interval=1, reason="moved",
                       redirect=(b"https://edge.example/moq", (b"live", b"cam1"),
                                 b"video"))
    out = _round_trip(msg, 18)
    assert out.error_code == RequestErrorCode.REDIRECT
    assert out.redirect == (b"https://edge.example/moq", (b"live", b"cam1"),
                            b"video")


def test_d18_redirect_with_empty_uri_means_this_session():
    msg = RequestError(error_code=RequestErrorCode.REDIRECT,
                       redirect=(b"", (b"live",), b"audio"))
    out = _round_trip(msg, 18)
    assert out.redirect == (b"", (b"live",), b"audio")


def test_d18_other_codes_carry_no_redirect():
    msg = RequestError(error_code=RequestErrorCode.DOES_NOT_EXIST,
                       reason="nope")
    out = _round_trip(msg, 18)
    assert out.redirect is None and out.reason == "nope"


def test_d16_never_emits_the_structure():
    msg = RequestError(request_id=7, error_code=RequestErrorCode.REDIRECT,
                       redirect=(b"", (b"live",), b"a"))
    out = _round_trip(msg, 16)
    assert out.request_id == 7 and out.redirect is None
