"""A peer stream reset before its subgroup header is logged at DEBUG: no
track can count it in a PUBLISH_DONE (§9.9). One reset after the header is
not."""
import asyncio
import logging

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state
from aiomoqt.types import StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 33740
_LINE = "before its subgroup header"


@requires_certs
async def test_a_reset_before_the_header_is_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="aiomoqt.data")
    _reset_relay_state()
    server = relay._build_server("localhost", _PORT, CERT, KEY,
                                 use_quic=True, draft=22)
    handle = await server.serve()
    try:
        async with MOQTClient("localhost", _PORT, path="/", use_quic=True,
                              verify_tls=False,
                              supported_drafts=22).connect() as s:
            await s.client_session_init()
            hdr = SubgroupHeader(track_alias=7, group_id=0, subgroup_id=0,
                                 prof=s._profile).serialize().data
            # The first byte of a header, then the reset.
            bare = await s.open_uni_stream()
            s.stream_write(bare, bytes(hdr[:1]))
            headed = await s.open_uni_stream()
            s.stream_write(headed, hdr)
            await asyncio.sleep(0.2)
            s.stream_reset(bare, StreamResetCode.CANCELLED)
            s.stream_reset(headed, StreamResetCode.CANCELLED)
            async with asyncio.timeout(3):
                while _LINE not in caplog.text:
                    await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
        lines = [r.getMessage() for r in caplog.records if _LINE in r.getMessage()]
        assert lines == [f"stream({bare}): reset (code "
                         f"{int(StreamResetCode.CANCELLED)}) before its "
                         f"subgroup header"], lines
    finally:
        handle.close()
        _reset_relay_state()
