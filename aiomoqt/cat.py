"""Common Access Token (CAT) credentials for MoQT session admission.

Tokens follow the profile moqx verifies, which differs from
draft-ietf-moq-c4m-01: the HMAC covers a COSE "Signature1" structure, the
moqt claim (key 65000) is a CBOR byte string, and the HMAC key is derived
from the shared secret with HKDF.
"""
import hashlib
import hmac
import time
from typing import Any, Iterable, Optional, Tuple

from .messages import AuthToken
from .types import SessionCloseCode

DEFAULT_TOKEN_TYPE = 1
ACTION_CLIENT_SETUP = 0
ALL_ACTIONS = tuple(range(9))

_ALG_HS256 = 5
_HDR_ALG, _HDR_KID, _HDR_TYP = 1, 4, 16
_CLAIM_EXP, _CLAIM_MOQT = 4, 65000
_HKDF_SALT = b"moqx-catapult-v1"
_HKDF_INFO = b"moqx-catapult-hmac-token-verify"
_MAX_TOKEN_BYTES = 16384
_MAX_DEPTH = 8


class _Refused(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _malformed() -> _Refused:
    return _Refused(SessionCloseCode.MALFORMED_AUTH_TOKEN,
                    "malformed authorization token")


def _bad_signature() -> _Refused:
    return _Refused(SessionCloseCode.UNAUTHORIZED,
                    "invalid authorization token signature")


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * size):
            return bytes([major << 5 | info]) + n.to_bytes(size, 'big')
    raise ValueError("CBOR integer out of range")


def _bstr(b: bytes) -> bytes:
    return _head(2, len(b)) + b


def _tstr(s: str) -> bytes:
    b = s.encode()
    return _head(3, len(b)) + b


def _array(items: Iterable[bytes]) -> bytes:
    items = list(items)
    return _head(4, len(items)) + b''.join(items)


def _map(pairs: Iterable[Tuple[int, bytes]]) -> bytes:
    pairs = list(pairs)
    return _head(5, len(pairs)) + b''.join(_head(0, k) + v for k, v in pairs)


class _Decoder:
    """Definite-length CBOR: integers, strings, arrays, maps and null."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise _malformed()
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def item(self, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            raise _malformed()
        first = self._take(1)[0]
        major, info = first >> 5, first & 0x1f
        if info > 27:
            raise _malformed()
        arg = (info if info < 24
               else int.from_bytes(self._take(1 << (info - 24)), 'big'))
        if major == 0:
            return arg
        if major == 1:
            return -1 - arg
        if major == 2:
            return self._take(arg)
        if major == 3:
            try:
                return self._take(arg).decode()
            except UnicodeDecodeError:
                raise _malformed() from None
        if major == 4:
            return [self.item(depth + 1) for _ in range(arg)]
        if major == 5:
            out = {}
            for _ in range(arg):
                key = self.item(depth + 1)
                if not isinstance(key, (int, str)) or key in out:
                    raise _malformed()
                out[key] = self.item(depth + 1)
            return out
        if major == 7 and info == 22:
            return None
        raise _malformed()


def _decode(data: bytes) -> Any:
    decoder = _Decoder(data)
    value = decoder.item()
    if decoder.pos != len(data):
        raise _malformed()
    return value


class CatAuth:
    """A shared HMAC key: a client mints its SETUP token with it, a server
    verifies one."""

    def __init__(self, key_id: str, secret: str,
                 token_type: int = DEFAULT_TOKEN_TYPE):
        if not key_id or not secret:
            raise ValueError("key id and secret must be non-empty")
        self.key_id = key_id
        self.token_type = int(token_type)
        self._secret = secret
        prk = hmac.new(_HKDF_SALT, secret.encode(), hashlib.sha256).digest()
        self._key = hmac.new(prk, _HKDF_INFO + b"\x01", hashlib.sha256).digest()

    @classmethod
    def parse(cls, spec: str, token_type: int = DEFAULT_TOKEN_TYPE) -> 'CatAuth':
        """From "KID:SECRET"; the secret may contain colons."""
        key_id, sep, secret = spec.partition(':')
        if not sep:
            raise ValueError("expected KID:SECRET")
        return cls(key_id, secret, token_type)

    @property
    def spec(self) -> str:
        return f"{self.key_id}:{self._secret}"

    def __repr__(self) -> str:
        return f"CatAuth(key_id={self.key_id!r}, token_type={self.token_type})"

    def _mac(self, protected: bytes, payload: bytes) -> bytes:
        signed = _array([_tstr("Signature1"), _bstr(protected), _bstr(b""),
                         _bstr(payload)])
        return hmac.new(self._key, signed, hashlib.sha256).digest()

    def mint(self, actions: Iterable[int] = ALL_ACTIONS,
             exp: Optional[int] = None) -> AuthToken:
        """A token granting `actions` on every namespace and track; without
        `exp` it does not expire."""
        protected = _map([(_HDR_ALG, _head(0, _ALG_HS256)),
                          (_HDR_KID, _tstr(self.key_id)),
                          (_HDR_TYP, _tstr("CAT"))])
        scopes = _array([_array([_array(_head(0, a) for a in actions)])])
        claims = [] if exp is None else [(_CLAIM_EXP, _head(0, exp))]
        claims.append((_CLAIM_MOQT, _bstr(scopes)))
        payload = _map(claims)
        return AuthToken(
            _array([_bstr(protected), _map([]), _bstr(payload),
                    _bstr(self._mac(protected, payload))]),
            self.token_type)

    def refusal(self, token: Optional[bytes]) -> Optional[Tuple[int, str]]:
        """(close code, reason) when `token` does not admit a session, else
        None."""
        if token is None:
            return (SessionCloseCode.UNAUTHORIZED,
                    "missing authorization token")
        if getattr(token, 'token_type', None) != self.token_type:
            return (SessionCloseCode.UNAUTHORIZED,
                    "wrong authorization token type")
        try:
            self._check(bytes(token))
        except _Refused as e:
            return e.code, e.reason
        return None

    def _check(self, raw: bytes) -> None:
        if not 0 < len(raw) <= _MAX_TOKEN_BYTES:
            raise _malformed()
        envelope = _decode(raw)
        if not isinstance(envelope, list) or len(envelope) != 4:
            raise _malformed()
        protected, unprotected, payload, tag = envelope
        if not (isinstance(protected, bytes) and isinstance(unprotected, dict)
                and isinstance(payload, bytes) and isinstance(tag, bytes)):
            raise _malformed()
        header = _decode(protected)
        if not isinstance(header, dict):
            raise _malformed()
        kid = header.get(_HDR_KID)
        if header.get(_HDR_ALG) != _ALG_HS256 or kid not in (None, self.key_id):
            raise _bad_signature()
        if not hmac.compare_digest(self._mac(protected, payload), tag):
            raise _bad_signature()

        claims = _decode(payload)
        if not isinstance(claims, dict):
            raise _malformed()
        exp = claims.get(_CLAIM_EXP)
        if exp is not None:
            if not isinstance(exp, int) or exp < 0:
                raise _malformed()
            if exp <= time.time():
                raise _Refused(SessionCloseCode.EXPIRED_AUTH_TOKEN,
                               "expired authorization token")
        moqt = claims.get(_CLAIM_MOQT)
        if moqt is not None and not isinstance(moqt, bytes):
            raise _malformed()
        scopes = _decode(moqt) if moqt is not None else []
        if not isinstance(scopes, list):
            raise _malformed()
        if not any(isinstance(scope, list) and scope
                   and isinstance(scope[0], list)
                   and ACTION_CLIENT_SETUP in scope[0] and not any(scope[1:])
                   for scope in scopes):
            raise _Refused(SessionCloseCode.UNAUTHORIZED,
                           "authorization token does not permit session setup")
