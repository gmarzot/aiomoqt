"""aiomoqt-relay caps what one session holds open: namespaces and
subscriptions past --max-namespaces-per-session and
--max-subscriptions-per-session are refused with EXCESSIVE_LOAD
(INTERNAL_ERROR at d14/d16, which lack it), one WARNING per session per
cap, while other sessions are unaffected. Refused SUBSCRIBEs leave no
state in the relay's session."""
import asyncio
import contextlib
import logging

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTRequestError

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_peer_request_state import settled, sizes

pytestmark = requires_certs

_BASE_PORT = 26300
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_DRAFTS = pytest.mark.parametrize("draft", [16, 18])
_NS = "caps/live"
# EXCESSIVE_LOAD's wire value; d16 has no such code.
_TOO_MANY = {16: 0x0, 18: 0x9}


def _port(case: int, draft: int, use_quic: bool) -> int:
    return _BASE_PORT + 4 * case + 2 * (draft == 18) + (not use_quic)


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._subscribing):
        table.clear()
    relay._cap_warned.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()
    relay._announce_subs.clear()


@pytest.fixture(autouse=True)
def _small_caps(monkeypatch):
    monkeypatch.setattr(relay, "MAX_NAMESPACES_PER_SESSION", 2)
    monkeypatch.setattr(relay, "MAX_SUBSCRIPTIONS_PER_SESSION", 2)
    _clear_relay()
    yield
    _clear_relay()


@contextlib.asynccontextmanager
async def _relay(port, draft, use_quic):
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            connect.server = server
            yield connect
    finally:
        handle.close()


def _cap_warnings(caplog, what):
    return [r for r in caplog.records if r.levelno == logging.WARNING
            and f"{what} (cap 2)" in r.getMessage()]


@_TRANSPORTS
@_DRAFTS
async def test_subscriptions_past_the_cap_are_refused(draft, use_quic,
                                                      caplog):
    caplog.set_level(logging.INFO)
    async with _relay(_port(0, draft, use_quic), draft, use_quic) as connect:
        pub = await connect()
        for name in ("a", "b", "c"):
            PublishedTrack(pub, namespace=_NS, trackname=name,
                           object_size=100, group_size=10,
                           rate=50).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        sub = await connect()
        held = [await sub.subscribe(_NS, name, wait_response=True)
                for name in ("a", "b")]
        for _ in range(2):
            with pytest.raises(MOQTRequestError) as refused:
                await sub.subscribe(_NS, "c", wait_response=True)
            assert refused.value.error_code == _TOO_MANY[draft]
            assert refused.value.retry_interval == relay.CAP_RETRY_MS + 1
        assert len(_cap_warnings(caplog, "subscriptions")) == 1
        # The cap is per session.
        other = await connect()
        await other.subscribe(_NS, "c", wait_response=True)
        # Ending one subscription makes room again.
        sub.unsubscribe(held[0].request_id)
        for _ in range(100):
            try:
                await sub.subscribe(_NS, "c", wait_response=True)
                break
            except MOQTRequestError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("no room after an unsubscribe")
        assert len(_cap_warnings(caplog, "subscriptions")) == 1


@_TRANSPORTS
@_DRAFTS
async def test_namespaces_past_the_cap_are_refused(draft, use_quic, caplog):
    caplog.set_level(logging.INFO)
    async with _relay(_port(1, draft, use_quic), draft, use_quic) as connect:
        pub = await connect()
        for ns in ("caps/a", "caps/b"):
            await pub.publish_namespace(namespace=ns, wait_response=True)
        for ns in ("caps/c", "caps/d"):
            with pytest.raises(MOQTRequestError) as refused:
                await pub.publish_namespace(namespace=ns, wait_response=True)
            assert refused.value.error_code == _TOO_MANY[draft]
        assert len(_cap_warnings(caplog, "namespaces")) == 1
        other = await connect()
        await other.publish_namespace(namespace="caps/c", wait_response=True)
        assert set(relay._announced) == {
            (b"caps", b"a"), (b"caps", b"b"), (b"caps", b"c")}


@_TRANSPORTS
@_DRAFTS
async def test_refused_subscribes_leave_no_relay_state(draft, use_quic):
    # TRACK_DOES_NOT_EXIST, past the subscription cap many times over.
    async with _relay(_port(2, draft, use_quic), draft, use_quic) as connect:
        sub = await connect()
        (session,) = list(connect.server._live)
        before = sizes(session)
        for i in range(600):
            with pytest.raises(MOQTRequestError):
                await asyncio.wait_for(
                    sub.subscribe("nobody/here", f"t{i}", wait_response=True),
                    5)
        assert await settled(session, before) == {}
        assert not relay._subscribing
