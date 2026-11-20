"""moq_interop_client tells a request our session's close ended from the
peer's REQUEST_ERROR."""
import asyncio
import types

import pytest

from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_client as mic
from aiomoqt.types import MOQTRequestError, RequestErrorCode, SessionCloseCode


async def _closed_by_us():
    fut = asyncio.get_running_loop().create_future()
    session = types.SimpleNamespace(_pending_requests={1: fut},
                                    _fetch_done_futures={})
    _MOQTSessionMixin._fail_pending(
        session, "FETCH_OK End before Start", SessionCloseCode.PROTOCOL_VIOLATION)
    return fut.exception()


async def test_a_request_our_close_ended_is_no_reply():
    e = await _closed_by_us()
    with pytest.raises(mic._NoReply) as raised:
        mic._check_reply(e)
    assert str(raised.value) == ("no reply before the session closed: "
                                 "PROTOCOL_VIOLATION: FETCH_OK End before Start")
    assert mic._refused("FETCH", e) == ("FETCH: no reply before the session "
                                        "closed: PROTOCOL_VIOLATION: FETCH_OK "
                                        "End before Start")


@pytest.mark.parametrize("code, reason", [
    (RequestErrorCode.INTERNAL_ERROR, "relay failed"),
    (RequestErrorCode.DOES_NOT_EXIST, "session closed"),
])
def test_a_peer_refusal_is_reported_as_refused(code, reason):
    e = MOQTRequestError(error_code=int(code), reason=reason)
    assert mic._check_reply(e) is e
    assert mic._refused("SUBSCRIBE", e) == (
        f"SUBSCRIBE refused: code={int(code)} {reason}")
