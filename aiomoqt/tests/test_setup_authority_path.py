"""PATH and AUTHORITY are client SETUP options for native QUIC only
(d18 §10.3.1.1-2, the same rule at d14/d16): from a server or over
WebTransport they close the session, and we never send them there."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.types import SessionCloseCode, SetupParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_DRAFTS = [14, 16, 18, 20, 22]
_OPTIONS = [(SetupParamType.PATH, "/x", SessionCloseCode.INVALID_PATH),
            (SetupParamType.AUTHORITY, "h:1",
             SessionCloseCode.INVALID_AUTHORITY)]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(base, draft, use_quic, i=0):
    return 16620 + base + 10 * _DRAFTS.index(draft) + 2 * i + (
        0 if use_quic else 1)


async def _serve(port, draft, use_quic):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    return await server.serve()


def _client(port, draft, use_quic):
    return MOQTClient("localhost", port, path="/", use_quic=use_quic,
                      verify_tls=False, supported_drafts=draft)


async def _init(session, **kw):
    try:
        await asyncio.wait_for(session.client_session_init(timeout=3, **kw),
                               5.0)
    except Exception:
        pass


@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("i, option", list(enumerate(_OPTIONS)),
                         ids=["path", "authority"])
async def test_client_closes_on_option_from_server(draft, use_quic, i,
                                                   option, monkeypatch):
    ptype, value, code = option
    orig = _MOQTSessionMixin._implementation_option

    def with_option(self):
        opts = orig(self)
        if not self._is_client:
            opts[ptype] = value.encode() if draft < 18 else value
        return opts
    monkeypatch.setattr(_MOQTSessionMixin, "_implementation_option",
                        with_option)
    port = _port(0, draft, use_quic, i)
    server = await _serve(port, draft, use_quic)
    try:
        async with _client(port, draft, use_quic).connect() as session:
            await _init(session)
            for _ in range(100):
                if session._close_err is not None:
                    break
                await asyncio.sleep(0.02)
            assert session._close_err is not None
            assert session._close_err[0] == code
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("ptype", [SetupParamType.PATH,
                                   SetupParamType.AUTHORITY],
                         ids=["path", "authority"])
async def test_wt_client_refuses_to_send_option(draft, ptype):
    port = _port(4, draft, False, 0 if ptype == SetupParamType.PATH else 1)
    server = await _serve(port, draft, False)
    try:
        async with _client(port, draft, False).connect() as session:
            with pytest.raises(ValueError):
                await session.client_session_init(
                    parameters={ptype: "/x"})
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_wt_setup_carries_neither_option(draft, monkeypatch):
    seen = []
    orig = _MOQTSessionMixin.send_control_message

    def record(self, msg, *a, **kw):
        opts = (getattr(msg, "options", None)
                or getattr(msg, "parameters", None))
        if type(msg).__name__ in ("ClientSetup", "ServerSetup", "Setup"):
            seen.append(dict(opts or {}))
        return orig(self, msg, *a, **kw)
    monkeypatch.setattr(_MOQTSessionMixin, "send_control_message", record)
    port = _port(8, draft, False)
    server = await _serve(port, draft, False)
    try:
        async with _client(port, draft, False).connect() as session:
            await session.client_session_init()
            assert session._close_err is None
    finally:
        server.close()
    assert len(seen) == 2
    for opts in seen:
        assert SetupParamType.PATH not in opts
        assert SetupParamType.AUTHORITY not in opts
