"""Common Access Token (CAT) credentials for MoQT session admission.

Tokens follow the profile moqx verifies, which differs from
draft-ietf-moq-c4m-01: the HMAC covers a COSE "Signature1" structure, the
moqt claim (key 65000) is a CBOR byte string, the HMAC key is derived from
the shared secret with HKDF, and a namespace match applies to the whole
length-prefixed namespace rather than field by field.
"""
import hashlib
import hmac
import time
from typing import Any, FrozenSet, Iterable, List, Optional, Tuple, Union

from .messages import AuthToken
from .types import SessionCloseCode

DEFAULT_TOKEN_TYPE = 1
ACTION_CLIENT_SETUP = 0
ACTION_PUBLISH_NAMESPACE = 2
ACTION_SUBSCRIBE_NAMESPACE = 3
ACTION_SUBSCRIBE = 4
ACTION_PUBLISH = 6
ACTION_FETCH = 7
ACTION_TRACK_STATUS = 8
ALL_ACTIONS = tuple(range(9))
_NAMESPACE_ACTIONS = frozenset({1, ACTION_PUBLISH_NAMESPACE,
                                ACTION_SUBSCRIBE_NAMESPACE})

_ALG_HS256 = 5
_HDR_ALG, _HDR_KID, _HDR_TYP = 1, 4, 16
_CLAIM_EXP, _CLAIM_MOQT = 4, 65000
_HKDF_SALT = b"moqx-catapult-v1"
_HKDF_INFO = b"moqx-catapult-hmac-token-verify"
_MAX_TOKEN_BYTES = 16384
_MAX_DEPTH = 8
_MATCH_EXACT, _MATCH_PREFIX, _MATCH_SUFFIX, _MATCH_CONTAINS = range(4)

Namespace = Union[str, Iterable[Union[str, bytes]]]
_Rule = Tuple[int, bytes]
_Scope = Tuple[FrozenSet[int], Tuple[_Rule, ...], Tuple[_Rule, ...]]


class CatRefused(Exception):
    """A token that does not admit: `code` is the session close code."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _malformed() -> CatRefused:
    return CatRefused(SessionCloseCode.MALFORMED_AUTH_TOKEN,
                    "malformed authorization token")


def _bad_signature() -> CatRefused:
    return CatRefused(SessionCloseCode.UNAUTHORIZED,
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


def _as_bytes(value: Union[str, bytes]) -> bytes:
    return value.encode() if isinstance(value, str) else bytes(value)


def _canonical_namespace(namespace: Optional[Namespace]) -> bytes:
    """Each field length-prefixed: what a namespace match applies to. A
    string is split on "/"."""
    if not namespace:
        return b""
    fields = namespace.split('/') if isinstance(namespace, str) else namespace
    return b''.join(len(f).to_bytes(4, 'big') + f
                    for f in map(_as_bytes, fields))


def _parse_rule(item: Any) -> Optional[_Rule]:
    if item is None:
        return None
    if isinstance(item, bytes):
        return _MATCH_EXACT, item
    if (isinstance(item, list) and len(item) == 2
            and item[0] in (_MATCH_EXACT, _MATCH_PREFIX, _MATCH_SUFFIX,
                            _MATCH_CONTAINS)
            and isinstance(item[1], bytes)):
        return item[0], item[1]
    raise _malformed()


def _parse_scope(raw: Any) -> _Scope:
    if not (isinstance(raw, list) and raw and isinstance(raw[0], list)):
        raise _malformed()
    ns_items: List[Any] = []
    if len(raw) > 1 and raw[1] is not None:
        if not isinstance(raw[1], list):
            raise _malformed()
        ns_items = raw[1]
    track_items: List[Any] = []
    if len(raw) > 2:
        track = raw[2]
        conditions = (isinstance(track, list) and track
                      and isinstance(track[0], list))
        track_items = track if conditions else [track]
    return (frozenset(a for a in raw[0] if isinstance(a, int)),
            tuple(r for r in map(_parse_rule, ns_items) if r),
            tuple(r for r in map(_parse_rule, track_items) if r))


def _matches(value: bytes, rule: _Rule) -> bool:
    kind, pattern = rule
    if kind == _MATCH_EXACT:
        return value == pattern
    if kind == _MATCH_PREFIX:
        return value.startswith(pattern)
    if kind == _MATCH_SUFFIX:
        return value.endswith(pattern)
    return pattern in value


def _encode_scope(actions: Iterable[int], namespace: Optional[bytes] = None,
                  track: Optional[bytes] = None) -> bytes:
    parts = [_array(_head(0, a) for a in actions)]
    if namespace is not None or track is not None:
        parts.append(_array([] if namespace is None else [_bstr(namespace)]))
    if track is not None:
        parts.append(_bstr(track))
    return _array(parts)


class Grants:
    """What one verified token allows."""

    def __init__(self, exp: Optional[int], scopes: Tuple[_Scope, ...]):
        self.exp = exp
        self.scopes = scopes

    def allows(self, action: int, namespace: Optional[Namespace] = None,
               track: Optional[Union[str, bytes]] = None) -> bool:
        """Whether `action` is granted now. A namespace-level action leaves
        `track` None, which a track match sees as empty."""
        if self.exp is not None and self.exp <= time.time():
            return False
        ns = _canonical_namespace(namespace)
        name = b"" if track is None else _as_bytes(track)
        return any(action in actions
                   and all(_matches(ns, r) for r in ns_rules)
                   and all(_matches(name, r) for r in track_rules)
                   for actions, ns_rules, track_rules in self.scopes)


class CatAuth:
    """A shared HMAC key: a client mints its SETUP token with it, a server
    verifies tokens with it."""

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
             exp: Optional[int] = None,
             namespace: Optional[Namespace] = None,
             track: Optional[Union[str, bytes]] = None) -> AuthToken:
        """A token granting `actions`, limited to exactly `namespace` and
        `track` when given; without `exp` it does not expire. CLIENT_SETUP is
        never limited, and namespace-level actions ignore `track`."""
        actions = list(actions)
        protected = _map([(_HDR_ALG, _head(0, _ALG_HS256)),
                          (_HDR_KID, _tstr(self.key_id)),
                          (_HDR_TYP, _tstr("CAT"))])
        if namespace is None and track is None:
            scopes = _array([_encode_scope(actions)])
        else:
            ns = None if namespace is None else _canonical_namespace(namespace)
            name = None if track is None else _as_bytes(track)
            ns_level = [a for a in actions if a in _NAMESPACE_ACTIONS]
            track_level = [a for a in actions if a != ACTION_CLIENT_SETUP
                           and a not in _NAMESPACE_ACTIONS]
            scopes = _array(filter(None, (
                _encode_scope([ACTION_CLIENT_SETUP])
                if ACTION_CLIENT_SETUP in actions else None,
                _encode_scope(ns_level, ns) if ns_level else None,
                _encode_scope(track_level, ns, name) if track_level else None,
            )))
        claims = [] if exp is None else [(_CLAIM_EXP, _head(0, exp))]
        claims.append((_CLAIM_MOQT, _bstr(scopes)))
        payload = _map(claims)
        return AuthToken(
            _array([_bstr(protected), _map([]), _bstr(payload),
                    _bstr(self._mac(protected, payload))]),
            self.token_type)

    def admit(self, token: Optional[bytes]) -> Grants:
        """The grants of a SETUP token admitting a session; raises CatRefused
        with the close code otherwise."""
        grants = self._verify(token)
        if not grants.allows(ACTION_CLIENT_SETUP):
            raise CatRefused(SessionCloseCode.UNAUTHORIZED,
                             "authorization token does not permit session setup")
        return grants

    def permits(self, action: int, namespace: Optional[Namespace],
                track: Optional[Union[str, bytes]], token: Optional[bytes],
                session_grants: Optional[Grants]) -> bool:
        """Whether the session's grants or the token on the request cover
        `action`. An invalid request token counts for nothing."""
        if (session_grants is not None
                and session_grants.allows(action, namespace, track)):
            return True
        if token is None:
            return False
        try:
            return self._verify(token).allows(action, namespace, track)
        except CatRefused:
            return False

    def _verify(self, token: Optional[bytes]) -> Grants:
        if token is None:
            raise CatRefused(SessionCloseCode.UNAUTHORIZED,
                             "missing authorization token")
        if getattr(token, 'token_type', None) != self.token_type:
            raise CatRefused(SessionCloseCode.UNAUTHORIZED,
                             "wrong authorization token type")
        raw = bytes(token)
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
                raise CatRefused(SessionCloseCode.EXPIRED_AUTH_TOKEN,
                               "expired authorization token")
        moqt = claims.get(_CLAIM_MOQT)
        if moqt is not None and not isinstance(moqt, bytes):
            raise _malformed()
        scopes = _decode(moqt) if moqt is not None else []
        if not isinstance(scopes, list):
            raise _malformed()
        return Grants(exp, tuple(map(_parse_scope, scopes)))
