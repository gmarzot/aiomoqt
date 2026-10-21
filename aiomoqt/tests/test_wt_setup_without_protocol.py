"""A WebTransport server with no negotiated WT-Protocol runs the d14 fallback;
a d18+ SETUP arriving there on a uni stream closes with a reason naming the
missing WT-Available-Protocols offer, not as an unknown data stream type."""
import asyncio
import logging
import re

from aiopquic.asyncio.webtransport import connect_webtransport

from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.utils.buffer import Buffer

pytestmark = requires_certs

_PORT = 17960


def _setup_prefix() -> bytes:
    """d18+ SETUP type (vi64 0x2F00) followed by a few payload bytes."""
    b = Buffer(capacity=16)
    b.push_uint_vi64(0x2F00)
    return bytes(b.data) + b"\x00\x04\x00\x00\x00\x00"


async def test_d18_setup_without_wt_protocol_names_the_offer(caplog):
    server = MOQTServer(host="localhost", port=_PORT, certificate=CERT,
                        private_key=KEY, path="/", use_quic=False,
                        supported_drafts=[22, 20, 18, 16, 14])
    handle = await server.serve()
    try:
        async with asyncio.timeout(10):
            async with connect_webtransport(
                    "localhost", _PORT, "/", verify_peer=False) as wt:
                assert wt.negotiated_protocol is None
                sid = await wt.create_stream(bidir=False)
                wt.send_stream_data(sid, _setup_prefix())
                await wt.wait_closed()
                closed = wt._session_close_event
        assert closed is not None
        reason = bytes(closed.reason).decode(errors="replace")
        assert "SETUP on a uni stream with no WT-Protocol" in reason, reason
        assert "WT-Available-Protocols" in reason, reason
        assert "unknown data stream type" not in reason
        # The server's stream error line names the peer.
        logged = [r.getMessage() for r in caplog.records
                  if r.levelno == logging.ERROR
                  and r.getMessage().startswith("MOQT stream(")
                  and "no WT-Protocol" in r.getMessage()]
        assert len(logged) == 1, logged
        assert re.search(r"peer \[?[0-9a-f.:]+\]?:\d+\)$", logged[0]), logged
    finally:
        handle.close()
