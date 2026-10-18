"""A FETCH_OK whose End Location is before its FETCH's Start closes the
session with PROTOCOL_VIOLATION, at every draft. Before d20 End is the
last Object plus 1, and End Object 0 covers the whole group."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SessionCloseCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17200
_DRAFTS = [14, 16, 18]
# Track name -> End Location the publisher answers with.
_ENDS = {"good": (5, 0), "bad": (2, 1)}


def _port(draft, use_quic):
    return _BASE_PORT + 2 * _DRAFTS.index(draft) + (not use_quic)


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic):
    async def on_fetch(session, msg):
        group, obj = _ENDS[msg.track_name.decode()]
        session.fetch_ok(request_id=msg.request_id, largest_group_id=group,
                         largest_object_id=obj)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.FETCH, on_fetch)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        handle.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_fetch_ok_ending_before_the_start_closes(draft, use_quic):
    async with _loopback(_port(draft, use_quic), draft, use_quic) as session:
        # End {5,0} is all of group 5, which holds Start {5,3}.
        ok = await session.fetch("ns", "good", start_group=5, start_object=3,
                                 end_group=6, end_object=0,
                                 wait_response=True)
        assert (ok.largest_group_id, ok.largest_object_id) == (5, 0)
        assert session._close_err is None
        session.fetch("ns", "bad", start_group=5, start_object=0,
                      end_group=6, end_object=0)
        for _ in range(100):
            if session._close_err is not None:
                break
            await asyncio.sleep(0.02)
        assert session._close_err is not None
        assert session._close_err[0] == SessionCloseCode.PROTOCOL_VIOLATION
