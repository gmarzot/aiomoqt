"""d18 REQUEST_ERROR with Error Code REDIRECT carries a Redirect structure
(§10.6.1); pre-d18 has neither the code nor the structure on the wire."""
from aiomoqt.context import profile_for
from aiomoqt.messages.request import RequestError
from aiomoqt.types import RequestErrorCode
from aiopquic.buffer import Buffer


def _round_trip(msg, draft):
    prof = profile_for(draft)
    raw = bytes(msg.serialize(prof=prof).data)
    buf = Buffer(data=raw, vi64=prof.vi64)
    buf.pull_vint()                      # type
    length = buf.pull_uint16()
    end = buf.tell() + length
    out = RequestError.deserialize(buf, prof=prof, buf_end=end)
    assert buf.tell() == end
    return out


def test_d18_redirect_round_trips():
    msg = RequestError(request_id=None, error_code=RequestErrorCode.REDIRECT,
                       retry_interval=1, reason="moved",
                       redirect=(b"https://edge.example/moq", (b"live", b"cam1"),
                                 b"video"))
    out = _round_trip(msg, 18)
    assert out.error_code == RequestErrorCode.REDIRECT
    assert out.redirect == (b"https://edge.example/moq", (b"live", b"cam1"),
                            b"video")


def test_d18_redirect_with_empty_uri_means_this_session():
    msg = RequestError(error_code=RequestErrorCode.REDIRECT,
                       redirect=(b"", (b"live",), b"audio"))
    out = _round_trip(msg, 18)
    assert out.redirect == (b"", (b"live",), b"audio")


def test_d18_other_codes_carry_no_redirect():
    msg = RequestError(error_code=RequestErrorCode.DOES_NOT_EXIST,
                       reason="nope")
    out = _round_trip(msg, 18)
    assert out.redirect is None and out.reason == "nope"


def test_d16_never_emits_the_structure():
    msg = RequestError(request_id=7, error_code=RequestErrorCode.REDIRECT,
                       redirect=(b"", (b"live",), b"a"))
    out = _round_trip(msg, 16)
    assert out.request_id == 7 and out.redirect is None


# -- session.redirect() -------------------------------------------------

import asyncio  # noqa: E402

import pytest  # noqa: E402

from aiomoqt.client import MOQTClient  # noqa: E402
from aiomoqt.messages import SubscribeNamespace  # noqa: E402
from aiomoqt.server import MOQTServer  # noqa: E402
from aiomoqt.types import MOQTMessageType, MOQTRequestError  # noqa: E402
from aiomoqt.tests._certs import CERT, KEY, requires_certs  # noqa: E402

_DRAFTS = [18, 20, 22]


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_namespace_redirect_leaves_track_name_empty(draft, use_quic):
    """§10.6.1: Track Name MUST be empty for a namespace-scoped request."""
    port = 16600 + _DRAFTS.index(draft) + (0 if use_quic else 4)
    refused = []

    async def on_subscribe_namespace(session, msg):
        with pytest.raises(ValueError):
            session.redirect(msg, track_name=b"t")
        session.redirect(msg, namespace=msg.namespace_prefix)
        refused.append(msg.request_id)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE_NAMESPACE,
                            on_subscribe_namespace)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            for prefix in ("redir/a", "redir/b"):
                with pytest.raises(MOQTRequestError) as err:
                    await asyncio.wait_for(session.subscribe_namespace(
                        prefix, wait_response=True), 5.0)
                assert err.value.error_code == RequestErrorCode.REDIRECT
                assert err.value.response.redirect == (
                    b"", tuple(p.encode() for p in prefix.split("/")), b"")
    finally:
        quic_server.close()
    assert len(refused) == 2


def test_redirect_needs_d18():
    from aiomoqt.protocol import _MOQTSessionMixin

    class _S(_MOQTSessionMixin):
        def __init__(self):
            self._profile = profile_for(16)

    with pytest.raises(ValueError):
        _S().redirect(SubscribeNamespace(request_id=1,
                                         namespace_prefix=(b"a",)))


def test_redirect_wire_with_empty_track_name():
    prof = profile_for(18)
    msg = RequestError(error_code=RequestErrorCode.REDIRECT,
                       reason="", redirect=(b"", (b"ns",), b""))
    raw = bytes(msg.serialize(prof=prof).data)
    # type 0x05, length, code 0x34, retry 0, empty reason, empty URI,
    # one namespace field "ns", empty track name.
    assert raw == bytes([0x05, 0x00, 0x09, 0x34, 0x00, 0x00, 0x00, 0x01,
                         0x02]) + b"ns" + bytes([0x00])


def test_redirect_prints():
    msg = RequestError(error_code=RequestErrorCode.REDIRECT, reason="",
                       redirect=(b"", (b"ns",), b""))
    assert "redirect=" in str(msg)


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_pub_bench_redirects_subscribe_namespace(use_quic, monkeypatch):
    from aiomoqt.tools import pub_bench
    port = 16610 + (0 if use_quic else 1)
    replies = []

    async def ask(session):
        for prefix in ("redir", "redir/x"):
            try:
                await asyncio.wait_for(session.subscribe_namespace(
                    prefix, wait_response=True), 5.0)
            except MOQTRequestError as e:
                replies.append((e.error_code, e.response.redirect))

    async def on_publish(session, msg):
        session.publish_ok(msg, forward=0)
        asyncio.ensure_future(ask(session))

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH, on_publish)
    quic_server = await server.serve()
    url = (f"moqt://localhost:{port}" if use_quic
           else f"https://localhost:{port}/")
    monkeypatch.setattr("sys.argv", [
        "pub_bench", url, "-k", "--draft", "18", "-N", "redir", "-T", "t",
        "--redirect", "-t", "1", "--no-stats"])
    try:
        await pub_bench.run(pub_bench.parse_args())
    finally:
        quic_server.close()
    assert replies == [
        (RequestErrorCode.REDIRECT, (b"", (b"redir",), b"")),
        (RequestErrorCode.REDIRECT, (b"", (b"redir", b"x"), b""))]


# -- receiving a Redirect for a namespace-scoped request -----------------

from aiomoqt.messages import RequestError as _RequestError  # noqa: E402
from aiomoqt.types import D18MessageType, SessionCloseCode  # noqa: E402


def _redirect_with(name):
    """Server handler: REDIRECT naming track `name`, sent past the
    redirect() guard."""
    async def handler(session, msg):
        session._send_reply(msg.request_id, _RequestError(
            request_id=msg.request_id, error_code=RequestErrorCode.REDIRECT,
            reason="", redirect=(b"", (b"other",), name)), fin=True)
    return handler


async def _namespace_redirect(session, kind, prefix):
    if kind == "publish_namespace":
        coro = session.publish_namespace(prefix, wait_response=True)
    elif kind == "subscribe_namespace":
        coro = session.subscribe_namespace(prefix, wait_response=True)
    else:
        coro = session.subscribe_tracks(prefix, wait_response=True)
    try:
        await asyncio.wait_for(coro, 3.0)
    except (MOQTRequestError, asyncio.TimeoutError, Exception):
        pass


_KINDS = {"publish_namespace": MOQTMessageType.PUBLISH_NAMESPACE,
          "subscribe_namespace": MOQTMessageType.SUBSCRIBE_NAMESPACE,
          "subscribe_tracks": D18MessageType.SUBSCRIBE_TRACKS}


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft, kind", [
    (18, "publish_namespace"), (18, "subscribe_namespace"),
    (20, "publish_namespace"), (20, "subscribe_tracks"),
    (22, "publish_namespace"), (22, "subscribe_namespace"),
])
async def test_namespace_redirect_with_track_name_closes(draft, kind,
                                                         use_quic):
    """§10.6.1: a non-empty Track Name in a Redirect for a namespace-
    scoped request closes with PROTOCOL_VIOLATION; an empty one does not."""
    port = (16640 + 4 * list(_KINDS).index(kind) + 2 * _DRAFTS.index(draft)
            + (0 if use_quic else 1))
    names = iter([b"", b"t"])

    async def handler(session, msg):
        await _redirect_with(next(names))(session, msg)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(_KINDS[kind], handler)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            await _namespace_redirect(session, kind, "redir/a")
            assert session._close_err is None
            await _namespace_redirect(session, kind, "redir/b")
            for _ in range(50):
                if session._close_err is not None:
                    break
                await asyncio.sleep(0.02)
            assert session._close_err is not None
            assert session._close_err[0] == SessionCloseCode.PROTOCOL_VIOLATION
    finally:
        quic_server.close()


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_track_redirect_with_track_name_is_kept(use_quic):
    port = 16690 + (0 if use_quic else 1)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.SUBSCRIBE, _redirect_with(b"t"))
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            for name in ("a", "b"):
                with pytest.raises(MOQTRequestError) as err:
                    await asyncio.wait_for(session.subscribe(
                        "redir", name, wait_response=True), 5.0)
                assert err.value.response.redirect == (b"", (b"other",), b"t")
            assert session._close_err is None
    finally:
        quic_server.close()
