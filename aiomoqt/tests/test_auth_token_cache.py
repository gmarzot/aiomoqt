"""Authorization tokens and the token cache (§10.2.2, §10.3.1.3-4): an
undecodable Token is a KEY_VALUE_FORMATTING_ERROR, a registration beyond
the advertised cache is AUTH_TOKEN_CACHE_OVERFLOW (in SETUP it is used as
a value instead), and an alias that is not registered refuses its
request."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.messages import AuthTokenRef, MOQTMessage
from aiomoqt.messages.d18 import Setup
from aiomoqt.messages.request import RequestError
from aiomoqt.messages.subscribe import Subscribe
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.types import (
    AuthTokenAliasType as Alias, MOQTException, ParamType, SessionCloseCode,
)
from aiomoqt.utils.buffer import Buffer

from aiomoqt.tests._certs import CERT, KEY, requires_certs

TOKEN = ParamType.AUTH_TOKEN
KVF = SessionCloseCode.KEY_VALUE_FORMATTING_ERROR


# -- codec -------------------------------------------------------------

@pytest.mark.parametrize("draft", [16, 18])
@pytest.mark.parametrize("raw", [
    b"", b"\x04", b"\x01\x05", b"\x02\x05\x00", b"\x00",
], ids=["empty", "alias-type-4", "register-no-type", "use-alias-trailing",
        "delete-no-alias"])
def test_an_undecodable_token_is_a_formatting_error(draft, raw):
    with pytest.raises(MOQTException) as err:
        MOQTMessage._auth_token_unwrap(raw, profile_for(draft))
    assert err.value.error_code == KVF


@pytest.mark.parametrize("draft", [16, 18])
@pytest.mark.parametrize("token", [
    AuthTokenRef(Alias.REGISTER, 7, 0, b"secret"),
    AuthTokenRef(Alias.USE_ALIAS, 7),
    AuthTokenRef(Alias.DELETE, 7),
], ids=["register", "use-alias", "delete"])
def test_cache_operations_round_trip(draft, token):
    prof = profile_for(draft)
    wire = MOQTMessage._auth_token_wrap(token, prof)
    assert MOQTMessage._auth_token_unwrap(wire, prof) == token


@pytest.mark.parametrize("draft", [16, 18])
def test_use_value_decodes_to_the_bare_value(draft):
    prof = profile_for(draft)
    wire = MOQTMessage._auth_token_wrap(b"secret", prof)
    assert MOQTMessage._auth_token_unwrap(wire, prof) == b"secret"


def test_d18_setup_with_an_undecodable_token_is_a_formatting_error():
    # The runner's stimulus body: one AUTH_TOKEN option, Alias Type 4.
    body = b"\x03\x01\x04"
    with pytest.raises(MOQTException) as err:
        Setup.deserialize(Buffer(data=body, vi64=True), prof=profile_for(18),
                          buf_end=len(body))
    assert err.value.error_code == KVF


# -- the session's token cache ------------------------------------------

def _session(cache=0, is_client=False, draft=18):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s.is_client = is_client
    s._auth_tokens = {}
    s._auth_token_cache_used = 0
    s._auth_token_cache_max = cache
    s.sent = []
    s._send_on_request_stream = (
        lambda rid, msg, fin=False: s.sent.append((rid, msg, fin)))
    return s


def _sub(rid, token):
    return Subscribe(request_id=rid, track_namespace=(b"n",), track_name=b"t",
                     parameters={TOKEN: token})


def _session_error(s, msg):
    with pytest.raises(MOQTException) as err:
        s._resolve_auth_token(msg)
    return err.value.error_code


async def _refused(s, msg):
    refuse = s._resolve_auth_token(msg)
    assert refuse is not None
    await refuse(s, msg)
    rid, reply, fin = s.sent.pop()
    assert isinstance(reply, RequestError) and fin
    return reply.error_code


def test_a_registration_with_no_cache_overflows():
    msg = _sub(1, AuthTokenRef(Alias.REGISTER, 1, 0, b"tok"))
    assert _session_error(_session(), msg) \
        == SessionCloseCode.AUTH_TOKEN_CACHE_OVERFLOW


async def test_an_oversize_setup_registration_is_used_as_a_value():
    s = _session()
    setup = Setup(options={TOKEN: AuthTokenRef(Alias.REGISTER, 1, 0, b"tok")})
    assert s._resolve_auth_token(setup) is None
    assert setup.options[TOKEN] == b"tok" and s._auth_tokens == {}
    assert await _refused(s, _sub(1, AuthTokenRef(Alias.USE_ALIAS, 1))) \
        == SessionCloseCode.UNKNOWN_AUTH_TOKEN_ALIAS


async def test_registered_aliases_resolve_until_deleted():
    s = _session(cache=16 + 3)              # exactly one 3-byte token
    reg = _sub(1, AuthTokenRef(Alias.REGISTER, 9, 0, b"tok"))
    assert s._resolve_auth_token(reg) is None
    assert reg.parameters[TOKEN] == b"tok" and s._auth_token_cache_used == 19
    use = _sub(3, AuthTokenRef(Alias.USE_ALIAS, 9))
    assert s._resolve_auth_token(use) is None and use.parameters[TOKEN] == b"tok"
    assert _session_error(s, _sub(5, AuthTokenRef(Alias.REGISTER, 9, 0, b"x"))) \
        == SessionCloseCode.DUPLICATE_AUTH_TOKEN_ALIAS
    assert _session_error(s, _sub(7, AuthTokenRef(Alias.REGISTER, 2, 0, b"x"))) \
        == SessionCloseCode.AUTH_TOKEN_CACHE_OVERFLOW
    delete = _sub(9, AuthTokenRef(Alias.DELETE, 9))
    assert s._resolve_auth_token(delete) is None
    assert TOKEN not in delete.parameters and s._auth_token_cache_used == 0
    assert await _refused(s, _sub(11, AuthTokenRef(Alias.DELETE, 9))) \
        == SessionCloseCode.UNKNOWN_AUTH_TOKEN_ALIAS


def test_a_server_refuses_alias_references_in_setup():
    setup = Setup(options={TOKEN: AuthTokenRef(Alias.USE_ALIAS, 1)})
    assert _session_error(_session(is_client=False), setup) \
        == SessionCloseCode.PROTOCOL_VIOLATION


# -- over a real session: the server closes with the right code ----------

_PORT = 16020


@requires_certs
@pytest.mark.parametrize("token, code", [
    (AuthTokenRef(Alias.REGISTER, 1, 0, b"tok"),
     SessionCloseCode.AUTH_TOKEN_CACHE_OVERFLOW),
    (AuthTokenRef(4, 0), SessionCloseCode.KEY_VALUE_FORMATTING_ERROR),
], ids=["register-overflows", "undecodable"])
async def test_the_server_closes_the_session_with_the_token_error(token, code):
    port = _PORT + (code == KVF)
    server = await MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=18).serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            session.subscribe("n", "t", parameters={TOKEN: token},
                              wait_response=False)
            got, _ = await asyncio.wait_for(session._moqt_session_closed, 5)
            assert got == code
    finally:
        server.close()
