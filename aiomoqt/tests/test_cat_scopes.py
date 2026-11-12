"""CAT scopes: grants, per-request checks, and viewers through the relay."""
import asyncio
import contextlib
import dataclasses
import itertools
import time
from types import SimpleNamespace

import pytest

from aiomoqt import cat
from aiomoqt.cat import CatAuth, CatRefused
from aiomoqt.client import MOQTClient
from aiomoqt.messages import (AuthToken, Fetch, Publish, PublishNamespace,
                              SubgroupHeader, Subscribe, SubscribeNamespace,
                              SubscribeTracks, TrackStatus)
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import (FetchType, MOQTRequestError, ParamType,
                           SessionCloseCode, SetupParamType)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

AUTH = CatAuth("cat-dev", "golden-secret-0123456789")
NS = (b"ab",)


def _issued(tail):
    # moqx-issuer v0.3.7-23-gdaab1270 output, expiring in 2090.
    return AuthToken(bytes.fromhex(
        "845819a3011b000000000000000504676361742d6465761063434154a0" + tail), 1)


# client_setup + subscribe on ab/catalog
SESSION = _issued(
    "5834a21b00000000000000041b00000000e227b4411b000000000000fde85782818100"
    "838104814600000002616247636174616c6f675820961a00561daf06c75b963bedcff9"
    "a3a69faccdcbf8b88251a60c83967b02b65f")
# subscribe on ab/video
VIDEO = _issued(
    "582fa21b00000000000000041b00000000e227b4421b000000000000fde85281838104"
    "814600000002616245766964656f5820b2f3ad33e815832effc0409fd315e203fd30f8"
    "6c02078360a6db823419cc7d11")
# subscribe on ab/audio
AUDIO = _issued(
    "582fa21b00000000000000041b00000000e227b4431b000000000000fde85281838104"
    "814600000002616245617564696f582045e3b6fc5d4bd88b67bb9634d1d55377ee9430"
    "1a74e56f41debe797d7b12ad68")
# client_setup, subscribe, publish_namespace and publish anywhere in ab
NAMESPACE_WIDE = _issued(
    "5839a21b00000000000000041b00000000e227b4431b000000000000fde8581b838181"
    "00828102814600000002616282820406814600000002616258200871927997732"
    "7b30daa5682bdf9baee8fc555699dc520f2ad2b3b247eeafccf")

UNAUTHORIZED = 1


def _signed(scopes):
    """A correctly MACed token with these encoded scopes."""
    protected = cat._map([(cat._HDR_ALG, cat._head(0, cat._ALG_HS256)),
                          (cat._HDR_KID, cat._tstr(AUTH.key_id))])
    payload = cat._map([(cat._CLAIM_MOQT, cat._bstr(scopes))])
    return AuthToken(
        cat._array([cat._bstr(protected), cat._map([]), cat._bstr(payload),
                    cat._bstr(AUTH._mac(protected, payload))]), 1)


def _scopes(token):
    claims = cat._decode(cat._decode(bytes(token))[2])
    return cat._decode(claims[cat._CLAIM_MOQT])


def test_issued_session_token_covers_only_its_track():
    grants = AUTH.admit(SESSION)
    assert grants.allows(cat.ACTION_SUBSCRIBE, NS, b"catalog")
    assert not grants.allows(cat.ACTION_SUBSCRIBE, NS, b"video")
    assert not grants.allows(cat.ACTION_FETCH, NS, b"catalog")
    assert not grants.allows(cat.ACTION_SUBSCRIBE, (b"other",), b"catalog")


def test_track_token_cannot_admit_a_session():
    with pytest.raises(CatRefused) as err:
        AUTH.admit(VIDEO)
    assert err.value.code == SessionCloseCode.UNAUTHORIZED


@pytest.mark.parametrize("kwargs, issued", [
    (dict(actions=[0, 4], namespace="ab", track="catalog"), SESSION),
    (dict(actions=[4], namespace="ab", track="video"), VIDEO),
    (dict(actions=[0, 2, 4, 6], namespace="ab"), NAMESPACE_WIDE),
], ids=["session", "track", "namespace"])
def test_minted_scopes_match_moqx_issuer(kwargs, issued):
    assert _scopes(AUTH.mint(**kwargs)) == _scopes(issued)


@pytest.mark.parametrize("token, expected", [
    (VIDEO, True),
    (AUDIO, False),
    (None, False),
    (AuthToken(bytes(VIDEO), 0), False),
    (AuthToken(b"\x01", 1), False),
    (CatAuth("cat-dev", "other").mint(), False),
], ids=["covering", "other-track", "none", "wrong-type", "garbage", "wrong-key"])
def test_request_token_counts_only_when_valid_and_covering(token, expected):
    assert AUTH.permits(cat.ACTION_SUBSCRIBE, NS, b"video", token,
                        AUTH.admit(SESSION)) is expected


def test_session_grants_alone_permit():
    assert AUTH.permits(cat.ACTION_SUBSCRIBE, NS, b"catalog", None,
                        AUTH.admit(SESSION))


def test_session_grants_stop_at_expiry(monkeypatch):
    grants = AUTH.admit(AUTH.mint(exp=int(time.time()) + 60))
    assert grants.allows(cat.ACTION_SUBSCRIBE, NS, b"video")
    later = time.time() + 120
    monkeypatch.setattr(cat.time, "time", lambda: later)
    assert not grants.allows(cat.ACTION_SUBSCRIBE, NS, b"video")


def test_namespace_match_covers_the_whole_namespace():
    grants = AUTH.admit(AUTH.mint(namespace=("a", "b")))
    assert grants.allows(cat.ACTION_SUBSCRIBE, (b"a", b"b"), b"t")
    assert not grants.allows(cat.ACTION_SUBSCRIBE, (b"ab",), b"t")
    assert not grants.allows(cat.ACTION_SUBSCRIBE, (b"a",), b"t")
    assert not grants.allows(cat.ACTION_SUBSCRIBE, (b"a", b"b", b"c"), b"t")


def test_namespace_action_fails_a_track_match():
    token = _signed(cat._array([cat._encode_scope([3], b"", b"video")]))
    assert not AUTH._verify(token).allows(3, ())


@pytest.mark.parametrize("kind, pattern, value, expected", [
    (cat._MATCH_PREFIX, b"vid", b"video", True),
    (cat._MATCH_PREFIX, b"deo", b"video", False),
    (cat._MATCH_SUFFIX, b"deo", b"video", True),
    (cat._MATCH_SUFFIX, b"vid", b"video", False),
    (cat._MATCH_CONTAINS, b"ide", b"video", True),
    (cat._MATCH_CONTAINS, b"xyz", b"video", False),
])
def test_track_match_kinds(kind, pattern, value, expected):
    rule = cat._array([cat._head(0, kind), cat._bstr(pattern)])
    scope = cat._array([cat._array([cat._head(0, 4)]), cat._array([]), rule])
    grants = AUTH._verify(_signed(cat._array([scope])))
    assert grants.allows(4, (), value) is expected


@pytest.mark.parametrize("scopes", [
    cat._array([cat._head(0, 1)]),
    cat._array([cat._array([])]),
    cat._array([cat._array([cat._tstr("x")])]),
    cat._array([cat._array([cat._array([cat._head(0, 4)]), cat._head(0, 1)])]),
    cat._array([cat._array([cat._array([cat._head(0, 4)]),
                            cat._array([cat._head(0, 7)])])]),
], ids=["scope-not-array", "empty-scope", "actions-not-array",
        "namespace-not-array", "bad-rule"])
def test_malformed_scopes(scopes):
    with pytest.raises(CatRefused) as err:
        AUTH._verify(_signed(scopes))
    assert err.value.code == SessionCloseCode.MALFORMED_AUTH_TOKEN


_TRACK = {"track_namespace": NS, "track_name": b"video"}
_REQUESTS = [
    (Subscribe, cat.ACTION_SUBSCRIBE, _TRACK),
    (Fetch, cat.ACTION_FETCH, {"namespace": NS, "track_name": b"video",
                               "fetch_type": FetchType.STANDALONE}),
    (SubscribeNamespace, cat.ACTION_SUBSCRIBE_NAMESPACE,
     {"namespace_prefix": NS}),
    (SubscribeTracks, cat.ACTION_SUBSCRIBE_NAMESPACE, {"namespace_prefix": NS}),
    (TrackStatus, cat.ACTION_TRACK_STATUS, _TRACK),
    (Publish, cat.ACTION_PUBLISH, _TRACK),
    (PublishNamespace, cat.ACTION_PUBLISH_NAMESPACE, {"namespace": NS}),
]


def _session(grants, draft=18, is_client=False):
    s = object.__new__(_MOQTSessionMixin)
    s._session = SimpleNamespace(auth=AUTH)
    s.is_client = is_client
    s.negotiated_draft = draft
    s._auth_grants = grants
    s._peer_requests = {}
    return s


def _request(cls, attrs, params=None):
    assert set(attrs) <= {f.name for f in dataclasses.fields(cls)}
    msg = object.__new__(cls)
    for name, value in {**attrs, "request_id": 2,
                        "parameters": params or {}}.items():
        setattr(msg, name, value)
    return msg


@pytest.mark.parametrize("cls, action, attrs", _REQUESTS,
                         ids=[r[0].__name__ for r in _REQUESTS])
def test_each_request_kind_needs_its_action(cls, action, attrs):
    setup_only = AUTH.admit(AUTH.mint(actions=[0]))
    assert _session(setup_only)._authorize_request(
        _request(cls, attrs)) is not None
    wrong = [a for a in range(2, 9) if a != action]
    assert _session(setup_only)._authorize_request(_request(
        cls, attrs, {ParamType.AUTH_TOKEN: AUTH.mint(actions=wrong)}
    )) is not None
    assert _session(setup_only)._authorize_request(_request(
        cls, attrs, {ParamType.AUTH_TOKEN: AUTH.mint(actions=[action])}
    )) is None


def test_joining_fetch_is_checked_against_its_subscription():
    attrs = {"fetch_type": FetchType.RELATIVE_JOINING, "joining_request_id": 4}
    session = _session(AUTH.admit(AUTH.mint(actions=[0])))
    session._peer_requests[4] = (Subscribe, (NS, b"video"))
    grant = AUTH.mint(actions=[cat.ACTION_FETCH], namespace="ab", track="video")
    other = AUTH.mint(actions=[cat.ACTION_FETCH], namespace="ab", track="audio")
    assert session._authorize_request(_request(
        Fetch, attrs, {ParamType.AUTH_TOKEN: grant})) is None
    assert session._authorize_request(_request(
        Fetch, attrs, {ParamType.AUTH_TOKEN: other})) is not None


def test_unresolved_joining_fetch_fails_closed():
    attrs = {"fetch_type": FetchType.RELATIVE_JOINING, "joining_request_id": 4}
    narrow = AUTH.mint(actions=[cat.ACTION_FETCH], namespace="ab", track="video")
    assert _session(None)._authorize_request(_request(
        Fetch, attrs, {ParamType.AUTH_TOKEN: narrow})) is not None


@pytest.mark.parametrize("draft, is_client", [(14, False), (18, True)],
                         ids=["d14", "client-side"])
def test_requests_not_checked(draft, is_client):
    assert _session(None, draft, is_client)._authorize_request(
        _request(Subscribe, _TRACK)) is None


_BASE_PORT = 20800
_DRAFTS = [16, 18, 20, 22]


class _Looping(PublishedTrack):
    """One single-object group every 20 ms until stopped."""

    stopped = False

    async def generate(self, session, track_alias):
        for group in itertools.count():
            if self.stopped:
                return
            sid = await session.open_uni_stream()
            hdr = SubgroupHeader(
                track_alias=track_alias, group_id=group, subgroup_id=0,
                publisher_priority=128, extensions_present=True,
                prof=session._profile)
            session.stream_write(sid, hdr.serialize().data)
            buf = hdr.next_object(payload=b"x", extensions=None, object_id=0)
            session.stream_write(sid, buf.data, end_stream=True)
            self._largest = (group, 0)
            await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def _relay(port, draft, use_quic):
    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic, draft=draft,
                                       auth=AUTH).serve()
    try:
        yield
    finally:
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


@contextlib.asynccontextmanager
async def _connected(port, draft, use_quic, setup_token):
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    async with client.connect() as session:
        await session.client_session_init(
            parameters={SetupParamType.AUTH_TOKEN: setup_token})
        yield session


async def _subscribe(session, track, token=None):
    got = []
    await SubscribedTrack(
        session, "ab", track, on_object=lambda *a: got.append(1),
        auth_token=token).subscribe(timeout=8.0)
    return got


async def _objects_arrive(got):
    target = len(got) + 1
    for _ in range(300):
        if len(got) >= target:
            return
        await asyncio.sleep(0.02)
    pytest.fail("no objects delivered")


async def _refused(session, track, token=None):
    with pytest.raises(MOQTRequestError) as err:
        await _subscribe(session, track, token)
    assert err.value.error_code == UNAUTHORIZED


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_viewers_through_the_relay(draft, use_quic):
    port = _BASE_PORT + 2 * _DRAFTS.index(draft) + int(use_quic)
    args = (port, draft, use_quic)
    async with _relay(*args), _connected(*args, AUTH.mint()) as pub:
        video = _Looping(pub, "ab", "video")
        catalog = _Looping(pub, "ab", "catalog")
        video.attach()
        catalog.attach()
        await pub.publish_namespace(namespace="ab", wait_response=True)
        try:
            async with _connected(*args, SESSION) as first:
                await _objects_arrive(await _subscribe(first, "catalog"))
                await _refused(first, "video")
                await _refused(first, "video", AUDIO)
                flow = await _subscribe(first, "video", VIDEO)
                await _objects_arrive(flow)

                async with _connected(*args, SESSION) as second:
                    await _refused(second, "video")
                    await _refused(second, "video", AUDIO)
                    await _objects_arrive(
                        await _subscribe(second, "video", VIDEO))
                async with _connected(*args, NAMESPACE_WIDE) as wide:
                    await _objects_arrive(await _subscribe(wide, "video"))
                await _objects_arrive(flow)
        finally:
            video.stopped = catalog.stopped = True


async def _result(awaitable):
    try:
        await awaitable
    except MOQTRequestError as e:
        return e.error_code
    return None


@requires_certs
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_request_kinds_on_the_wire(draft):
    port = _BASE_PORT + 20 + _DRAFTS.index(draft)
    args = (port, draft, True)

    def requests(session, params):
        return (
            lambda: session.publish_namespace(
                "ab", parameters=params, wait_response=True),
            lambda: session.subscribe_namespace(
                "ab", parameters=params, wait_response=True),
            lambda: session.track_status(
                "ab", "video", parameters=params, wait_response=True),
            lambda: session.fetch(
                "ab", "video", end_group=1, parameters=params,
                wait_response=True),
        )

    async with _relay(*args):
        async with _connected(*args, AUTH.mint(actions=[0])) as session:
            for call in requests(session, None):
                assert await _result(call()) == UNAUTHORIZED
        grant = {ParamType.AUTH_TOKEN: AUTH.mint(namespace="ab")}
        async with _connected(*args, AUTH.mint(actions=[0])) as session:
            for call in requests(session, grant):
                assert await _result(call()) != UNAUTHORIZED
