"""stream_write_drain when the transport goes away under a write.

aiopquic raises ConnectionError for a write to a closed WebTransport
session, and WebTransportError (a ConnectionError) for a stream torn down
before the session-close event. Either must end the producer through the
cancellation path: not escape as a task exception, not return silently.
"""
import asyncio
from types import SimpleNamespace

import pytest
from aiopquic.asyncio.webtransport import WebTransportError

from aiomoqt.protocol import _MOQTSessionMixin


def _drain_session(exc):
    """Session whose transport raises `exc` from every drained write."""
    s = object.__new__(_MOQTSessionMixin)
    s._close_err = None
    s._session = SimpleNamespace(tx_max_inflight_bytes=None)

    class _Quic:
        closed = False

        async def send_stream_data_drained(self, stream_id, data,
                                           end_stream=False):
            raise exc

    s._quic = _Quic()
    return s


@pytest.mark.parametrize("exc_type,msg", [
    (WebTransportError, "WT stream 4 not available"),
    (ConnectionError, "WT session closed"),
], ids=["stream", "session"])
async def test_teardown_error_cancels(exc_type, msg):
    exc = exc_type(msg)
    s = _drain_session(exc)
    assert s._session_writable()  # the close event has not landed yet
    with pytest.raises(asyncio.CancelledError) as info:
        await s.stream_write_drain(4, b"x")
    assert info.value.__cause__ is exc


async def test_unpaced_producer_ends_cancelled():
    # Never sleeps between writes, so a silent return would spin it.
    s = _drain_session(WebTransportError("WT stream 4 not available"))
    written = 0

    async def produce():
        nonlocal written
        for _ in range(1000):
            await s.stream_write_drain(4, b"x")
            written += 1

    task = asyncio.create_task(produce())
    await asyncio.wait({task}, timeout=5)
    assert task.cancelled()
    assert written == 0


async def test_write_race_still_swallowed():
    s = _drain_session(AssertionError("stream gone"))
    await s.stream_write_drain(4, b"x")
