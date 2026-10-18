"""join(): the SUBSCRIBE carries `parameters`; its Joining FETCH carries
`fetch_parameters` and, of `parameters`, only AUTHORIZATION_TOKEN. A
SUBSCRIBE-only parameter in a FETCH obliges a d18 peer to close."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, ParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17210
_TRACKS = ("video", "audio")
_FETCH_TOKEN = b"fetch-only"


@contextlib.asynccontextmanager
async def _loopback(port, use_quic, seen, fetched):
    async def on_subscribe(session, msg):
        seen.setdefault(msg.track_name.decode(), {})["sub"] = msg.parameters

    async def on_fetch(session, msg):
        sub = next(m for m in seen.values()
                   if m.get("rid") == msg.joining_request_id)
        sub["fetch"] = msg.parameters
        if all("fetch" in seen.get(n, {}) for n in _TRACKS):
            fetched.set()

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    server.register_handler(MOQTMessageType.FETCH, on_fetch)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=18)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        handle.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_joining_fetch_takes_only_the_auth_token(use_quic):
    seen = {}
    fetched = asyncio.Event()
    port = _BASE_PORT + (not use_quic)
    async with _loopback(port, use_quic, seen, fetched) as session:
        for i, name in enumerate(_TRACKS):
            token = f"tok-{name}".encode()
            # The second track's FETCH names its own token.
            extra = ({ParamType.AUTH_TOKEN: _FETCH_TOKEN} if i else None)
            sub, fetch = await session.join(
                "ns", name, joining_start=1,
                parameters={ParamType.DELIVERY_TIMEOUT: 500 + i,
                            ParamType.AUTH_TOKEN: token},
                fetch_parameters=extra)
            seen.setdefault(name, {})["rid"] = sub.request_id
            assert set(fetch.parameters) == {ParamType.AUTH_TOKEN}
        await asyncio.wait_for(fetched.wait(), timeout=2.0)
        assert session._close_err is None
    for i, name in enumerate(_TRACKS):
        token = f"tok-{name}".encode()
        assert seen[name]["sub"][ParamType.DELIVERY_TIMEOUT] == 500 + i
        assert seen[name]["sub"][ParamType.AUTH_TOKEN] == token
        want = _FETCH_TOKEN if i else token
        assert dict(seen[name]["fetch"]) == {ParamType.AUTH_TOKEN: want}

