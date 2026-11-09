"""CAT session admission: moqx-format tokens and the SETUP gate."""
import argparse
import time

import pytest

from aiomoqt import cat
from aiomoqt.cat import CatAuth
from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.messages import AuthToken, AuthTokenRef, ClientSetup
from aiomoqt.messages.d18 import Setup
from aiomoqt.protocol import _closed_reason
from aiomoqt.server import MOQTServer
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import (AuthTokenAliasType, MOQTException,
                          SessionCloseCode as Close, SetupParamType)
from aiomoqt.utils import cli

from aiomoqt.tests._certs import CERT, KEY, requires_certs

SECRET = "golden-secret-0123456789"
AUTH = CatAuth("cat-dev", SECRET)

# From moqx-issuer v0.3.7-23-gdaab1270, expiring in 2090.
ISSUED_ALL_ACTIONS = AuthToken(bytes.fromhex(
    "845819a3011b000000000000000504676361742d6465761063434154a0582da21b"
    "00000000000000041b00000000e2279d111b000000000000fde850838181008183"
    "0102038185040506070858202f06ca68dc2e66bc29b39bf64ff149c46e7099d1f4"
    "5dd730ce22eb4f93fea359"), 1)
ISSUED_NO_SETUP = AuthToken(bytes.fromhex(
    "845819a3011b000000000000000504676361742d6465761063434154a05822a21b"
    "00000000000000041b00000000e2279d111b000000000000fde845818182060458"
    "2013c2c14fc4c9669552acc58c005af37b56e332520c2a9c360afe3d8299a6795e"
), 1)

MALFORMED = Close.MALFORMED_AUTH_TOKEN
DENIED = Close.UNAUTHORIZED
EXPIRED = Close.EXPIRED_AUTH_TOKEN
SETUP_SCOPE = cat._array([cat._array([cat._array([cat._head(0, 0)])])])
BROAD = (cat._CLAIM_MOQT, cat._bstr(SETUP_SCOPE))


def _token(claims, alg=cat._ALG_HS256, kid="cat-dev"):
    """A correctly MACed token with arbitrary contents."""
    header = [(cat._HDR_ALG, cat._head(0, alg))]
    if kid:
        header.append((cat._HDR_KID, cat._tstr(kid)))
    protected = cat._map(header)
    payload = cat._map(claims)
    return AuthToken(
        cat._array([cat._bstr(protected), cat._map([]), cat._bstr(payload),
                    cat._bstr(AUTH._mac(protected, payload))]), 1)


def _past():
    return int(time.time()) - 60


def _flip_last(token):
    return AuthToken(bytes(token)[:-1] + bytes([token[-1] ^ 1]), 1)


def _flip_payload(token):
    raw = bytearray(token)
    raw[len(raw) - 40] ^= 1
    return AuthToken(bytes(raw), 1)


def test_moqx_issued_token_admits():
    assert AUTH.refusal(ISSUED_ALL_ACTIONS) is None


def test_moqx_issued_token_without_client_setup_is_refused():
    assert AUTH.refusal(ISSUED_NO_SETUP)[0] == DENIED


def test_minted_token_grants_every_action_without_expiry():
    token = AUTH.mint()
    assert token.token_type == cat.DEFAULT_TOKEN_TYPE
    assert AUTH.refusal(token) is None
    claims = cat._decode(cat._decode(bytes(token))[2])
    assert cat._CLAIM_EXP not in claims
    assert cat._decode(claims[cat._CLAIM_MOQT]) == [[list(range(9))]]


def test_future_expiry_and_absent_kid_admit():
    assert AUTH.refusal(AUTH.mint(exp=int(time.time()) + 60)) is None
    assert AUTH.refusal(_token([BROAD], kid=None)) is None


@pytest.mark.parametrize("build, code", [
    (lambda: None, DENIED),
    (lambda: AuthToken(AUTH.mint(), 0), DENIED),
    (lambda: CatAuth("cat-dev", "other").mint(), DENIED),
    (lambda: CatAuth("other", SECRET).mint(), DENIED),
    (lambda: AUTH.mint(actions=[4]), DENIED),
    (lambda: _flip_last(AUTH.mint()), DENIED),
    (lambda: _flip_payload(AUTH.mint()), DENIED),
    (lambda: _token([BROAD], alg=6), DENIED),
    (lambda: _token([]), DENIED),
    (lambda: _token([(cat._CLAIM_MOQT, cat._bstr(cat._array([cat._array([
        cat._array([cat._head(0, 0)]),
        cat._array([cat._bstr(b"ns")])])])))]), DENIED),
    (lambda: AUTH.mint(exp=_past()), EXPIRED),
    (lambda: _token([(cat._CLAIM_EXP, cat._tstr("soon")), BROAD]), MALFORMED),
    (lambda: _token([(cat._CLAIM_MOQT, SETUP_SCOPE)]), MALFORMED),
    (lambda: AuthToken(b"", 1), MALFORMED),
    (lambda: AuthToken(b"\x01\x02\x03", 1), MALFORMED),
    (lambda: AuthToken(bytes(AUTH.mint())[:-1], 1), MALFORMED),
    (lambda: AuthToken(bytes(AUTH.mint()) + b"\x00", 1), MALFORMED),
    (lambda: AuthToken(b"\x9f\xff", 1), MALFORMED),
    (lambda: AuthToken(b"\xd8\x3d" + bytes(AUTH.mint()), 1), MALFORMED),
    (lambda: AuthToken(b"\x81" * 64 + b"\x00", 1), MALFORMED),
    (lambda: AuthToken(b"\x9b" + b"\xff" * 8, 1), MALFORMED),
    (lambda: AuthToken(b"\x84" + b"\x00" * 20000, 1), MALFORMED),
], ids=["missing", "wrong-type", "wrong-secret", "wrong-kid", "no-setup-grant",
        "bad-tag", "bad-payload", "other-alg", "no-moqt-claim", "scoped-setup",
        "expired", "exp-not-int", "moqt-not-bstr", "empty", "not-cose",
        "truncated", "trailing", "indefinite", "tagged", "deep", "huge-array",
        "oversize"])
def test_refusals(build, code):
    assert AUTH.refusal(build())[0] == code


def test_secret_stays_out_of_repr():
    assert SECRET not in repr(AUTH)


@pytest.mark.parametrize("draft, message", [
    (16, ClientSetup(versions=[], parameters={
        SetupParamType.AUTH_TOKEN: AUTH.mint()})),
    (18, Setup(options={SetupParamType.AUTH_TOKEN: AUTH.mint()})),
], ids=["client-setup", "setup"])
def test_token_value_stays_out_of_logged_messages(draft, message):
    token = bytes(AUTH.mint())
    text = message.describe(profile_for(draft))
    assert f"<{len(token)} bytes>" in text
    assert token[:8].hex() not in text and repr(token)[2:20] not in text


def test_registered_token_value_stays_out_of_repr():
    ref = AuthTokenRef(AuthTokenAliasType.REGISTER, 1, 1, b"s3cret")
    assert "s3cret" not in repr(ref)


def test_parse_spec():
    auth = CatAuth.parse("kid:se:cret", 7)
    assert (auth.key_id, auth.token_type, auth.spec) == ("kid", 7, "kid:se:cret")
    for bad in ("nocolon", ":secret", "kid:"):
        with pytest.raises(ValueError):
            CatAuth.parse(bad)


@pytest.mark.parametrize("code, reason, text", [
    (0, "", "session closed"),
    (0, "bye", "session closed: bye"),
    (Close.UNAUTHORIZED, "", "session closed: UNAUTHORIZED"),
    (Close.UNAUTHORIZED, "no", "session closed: UNAUTHORIZED: no"),
    (0x7777, "", "session closed: 0x7777"),
])
def test_closed_reason_names_the_close_code(code, reason, text):
    assert _closed_reason(code, reason) == text


def _auth_args(*argv):
    parser = argparse.ArgumentParser()
    cli.add_auth(parser)
    return parser.parse_args(argv)


def test_cli_flag_reaches_transport_kwargs(monkeypatch):
    monkeypatch.delenv(cli.AUTH_KEY_ENV, raising=False)
    monkeypatch.delenv(cli.AUTH_TYPE_ENV, raising=False)
    assert "auth" not in cli.transport_kwargs(_auth_args())
    auth = cli.transport_kwargs(
        _auth_args("--auth-key", "k:s", "--auth-token-type", "16"))["auth"]
    assert (auth.key_id, auth.token_type) == ("k", 16)


def test_cli_environment_supplies_the_key(monkeypatch):
    monkeypatch.setenv(cli.AUTH_KEY_ENV, "k:s")
    monkeypatch.setenv(cli.AUTH_TYPE_ENV, "16")
    auth = cli.auth_from_args(argparse.Namespace())
    assert (auth.key_id, auth.token_type) == ("k", 16)


def test_cli_bad_key_exits(monkeypatch):
    monkeypatch.delenv(cli.AUTH_KEY_ENV, raising=False)
    with pytest.raises(SystemExit):
        cli.auth_from_args(_auth_args("--auth-key", "nocolon"))


_BASE_PORT = 20700
_DRAFTS = [14, 16, 18, 20, 22]


def _client(port, draft, use_quic, auth=None):
    return MOQTClient("localhost", port, path="/", use_quic=use_quic,
                      verify_tls=False, supported_drafts=draft, auth=auth)


def _setup_token(token):
    return {SetupParamType.AUTH_TOKEN: token}


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_setup_gate(draft, use_quic):
    port = _BASE_PORT + 2 * _DRAFTS.index(draft) + int(use_quic)
    handle = await MOQTServer("localhost", port, CERT, KEY, path="/",
                              use_quic=use_quic, supported_drafts=draft,
                              auth=AUTH).serve()
    try:
        async with _client(port, draft, use_quic, AUTH).connect() as session:
            await session.client_session_init()
        for auth, params, code in (
                (None, None, DENIED),
                (CatAuth("cat-dev", "other"), None, DENIED),
                (CatAuth("cat-dev", SECRET, 0), None, DENIED),
                (None, _setup_token(AUTH.mint(exp=_past())), EXPIRED),
                (None, _setup_token(AuthToken(b"\x01", 1)), MALFORMED)):
            with pytest.raises(MOQTException) as err:
                async with _client(port, draft, use_quic, auth).connect() as s:
                    await s.client_session_init(parameters=params)
            assert err.value.error_code == code
    finally:
        handle.close()


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_no_auth_server_admits_a_session_without_a_token(use_quic):
    port = _BASE_PORT + 20 + int(use_quic)
    handle = await MOQTServer("localhost", port, CERT, KEY, path="/",
                              use_quic=use_quic, supported_drafts=18).serve()
    try:
        async with _client(port, 18, use_quic).connect() as session:
            await session.client_session_init()
    finally:
        handle.close()


@requires_certs
async def test_relay_gate_admits_its_watchdog_probe():
    relay._announced.clear()
    relay._tracks.clear()
    port = _BASE_PORT + 30
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=True, draft=18,
                                       auth=AUTH).serve()
    try:
        assert await relay._probe_in_child("localhost", port, True, 18, 8,
                                           True, AUTH)
        assert not await relay._probe_in_child("localhost", port, True, 18, 8,
                                               False)
    finally:
        handle.close()
