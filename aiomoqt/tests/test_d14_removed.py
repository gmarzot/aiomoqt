"""Draft 14 and its moq-00 ALPN are not spoken: asking for it is a clear
error, and a peer offering only moq-00 is refused at the handshake."""
import asyncio
import subprocess
import sys

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.types import (MOQT_DEFAULT_DRAFTS, moqt_alpn_for_version,
                           moqt_version_from_alpn, moqt_version_from_draft,
                           normalize_supported_drafts)
from aiopquic.quic.configuration import QuicConfiguration

_PORT = 18140


@pytest.mark.parametrize("call", [
    lambda: moqt_version_from_draft(14),
    lambda: normalize_supported_drafts(14),
    lambda: normalize_supported_drafts([18, 14]),
    lambda: profile_for(14),
    lambda: MOQTClient("h", 1, supported_drafts=14),
    lambda: MOQTServer("h", 1, "c", "k", supported_drafts=14),
], ids=["version", "normalize", "normalize-list", "profile", "client",
        "server"])
def test_draft_14_is_refused(call):
    with pytest.raises(ValueError, match="(?i)not supported"):
        call()


def test_moq_00_is_no_draft():
    with pytest.raises(ValueError, match="moq-00"):
        moqt_version_from_alpn("moq-00")
    assert 14 not in MOQT_DEFAULT_DRAFTS
    assert all(moqt_alpn_for_version(d).startswith("moqt-")
               for d in MOQT_DEFAULT_DRAFTS)


@pytest.mark.parametrize("tool, args", [
    ("pub_bench", ["moqt://localhost:9/"]),
    ("sub_bench", ["moqt://localhost:9/"]),
    ("loopback_bench", []),
], ids=["pub_bench", "sub_bench", "loopback_bench"])
def test_tool_flag_names_the_supported_drafts(tool, args):
    r = subprocess.run(
        [sys.executable, "-m", f"aiomoqt.tools.{tool}", *args,
         "--draft", "14"],
        capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "draft 14 not supported" in r.stderr, r.stderr
    assert "[16, 18, 20, 22]" in r.stderr, r.stderr


@requires_certs
async def test_a_client_offering_only_moq_00_is_refused():
    handle = await MOQTServer(
        host="localhost", port=_PORT, certificate=CERT, private_key=KEY,
        path="/", use_quic=True).serve()
    try:
        cfg = QuicConfiguration(alpn_protocols=["moq-00"], is_client=True,
                                verify_mode=False, server_name="localhost")
        client = MOQTClient("localhost", _PORT, path="/", use_quic=True,
                            verify_tls=False, configuration=cfg)
        with pytest.raises(Exception):
            async with asyncio.timeout(8):
                async with client.connect() as session:
                    await session.client_session_init()
    finally:
        handle.close()
