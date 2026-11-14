"""aiomoqt-relay admission: at --max-subscribers a session's
first SUBSCRIBE is refused with EXCESSIVE_LOAD (INTERNAL_ERROR at d16) and a
Retry Interval, leaving no state. Sessions already subscribed keep receiving and
may add tracks; the watchdog's probe is never refused."""
import asyncio
import contextlib
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import MOQTRequestError
from aiomoqt.utils.workers import retry_wait

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_peer_request_state import settled, sizes

_BASE_PORT = 30400
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_DRAFTS = pytest.mark.parametrize("draft", [16, 18, 22])
_NS = "admit/live"
# EXCESSIVE_LOAD's wire value; d16 has no such code.
_REFUSED = {16: 0x0, 18: 0x9, 22: 0x9}
_DRAFT_INDEX = {16: 0, 18: 1, 22: 2}


def _port(case: int, draft: int, use_quic: bool) -> int:
    return _BASE_PORT + 8 * case + 2 * _DRAFT_INDEX[draft] + (not use_quic)


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._subscribing):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()
    relay._announce_subs.clear()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(relay, "_admission_refused", 0)
    monkeypatch.setattr(relay, "MAX_SUBSCRIBERS", 0)
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
            yield connect
    finally:
        handle.close()


async def _publisher(connect, names):
    pub = await connect()
    for name in names:
        PublishedTrack(pub, namespace=_NS, trackname=name, object_size=100,
                       group_size=10, rate=50).attach()
    await pub.publish_namespace(namespace=_NS, wait_response=True)
    return pub


async def _counted(session, name):
    """A subscription and the list its objects land in."""
    got = []
    track = SubscribedTrack(session, _NS, trackname=name,
                            on_object=lambda *a, **k: got.append(1))
    await track.subscribe()
    return track, got


async def _grows(got, timeout=5.0):
    start = len(got)
    async with asyncio.timeout(timeout):
        while len(got) <= start:
            await asyncio.sleep(0.02)


def _assert_refused(exc, draft):
    assert exc.error_code == _REFUSED[draft]
    lo, hi = relay.ADMISSION_RETRY_MS
    assert lo + 1 <= exc.retry_interval <= hi + 1


# -- on the wire -----------------------------------------------------

@requires_certs
@_TRANSPORTS
@_DRAFTS
async def test_new_session_refused_and_leaves_no_state(monkeypatch, draft,
                                                       use_quic):
    monkeypatch.setattr(relay, "MAX_SUBSCRIBERS", 1)
    async with _relay(_port(0, draft, use_quic), draft, use_quic) as connect:
        await _publisher(connect, ["a"])
        viewer = await connect()
        await viewer.subscribe(_NS, "a", wait_response=True)
        sub = await connect()
        baseline = sizes(sub)
        for attempt in (1, 2):
            with pytest.raises(MOQTRequestError) as refused:
                await sub.subscribe(_NS, "a", wait_response=True)
            _assert_refused(refused.value, draft)
            assert relay._admission_refused == attempt
        assert await settled(sub, baseline) == {}
        assert relay._session_subscriptions(sub) == 0
        assert relay._subscriber_sessions() == 1
        assert sum(len(t.downstream) for t in relay._tracks.values()) == 1


@requires_certs
@_TRANSPORTS
@_DRAFTS
async def test_attached_session_keeps_service_and_adds_tracks(monkeypatch, draft,
                                                              use_quic):
    monkeypatch.setattr(relay, "MAX_SUBSCRIBERS", 1)
    async with _relay(_port(1, draft, use_quic), draft, use_quic) as connect:
        await _publisher(connect, ["a", "b"])
        viewer = await connect()
        _, got_a = await _counted(viewer, "a")
        await _grows(got_a)
        # At the limit: this viewer still gets its other track.
        _, got_b = await _counted(viewer, "b")
        await _grows(got_a)
        await _grows(got_b)
        late = await connect()
        with pytest.raises(MOQTRequestError) as refused:
            await late.subscribe(_NS, "a", wait_response=True)
        _assert_refused(refused.value, draft)
        await _grows(got_a)
        await _grows(got_b)


@requires_certs
@_TRANSPORTS
async def test_max_subscribers_counts_sessions(monkeypatch, use_quic):
    draft = 22
    monkeypatch.setattr(relay, "MAX_SUBSCRIBERS", 1)
    async with _relay(_port(3, draft, use_quic), draft, use_quic) as connect:
        await _publisher(connect, ["a", "b"])
        first = await connect()
        await first.subscribe(_NS, "a", wait_response=True)
        # Two tracks on one session count once.
        await first.subscribe(_NS, "b", wait_response=True)
        assert relay._subscriber_sessions() == 1
        other = await connect()
        with pytest.raises(MOQTRequestError) as refused:
            await other.subscribe(_NS, "a", wait_response=True)
        _assert_refused(refused.value, draft)
        first.close()
        async with asyncio.timeout(10):
            while relay._subscriber_sessions():
                await asyncio.sleep(0.05)
        await other.subscribe(_NS, "a", wait_response=True)


@requires_certs
@_TRANSPORTS
async def test_watchdog_probe_is_never_refused(monkeypatch, use_quic):
    draft = 22
    monkeypatch.setattr(relay, "MAX_SUBSCRIBERS", 1)
    ns = relay.WATCHDOG_NS.decode() if isinstance(relay.WATCHDOG_NS, bytes) \
        else relay.WATCHDOG_NS
    async with _relay(_port(4, draft, use_quic), draft, use_quic) as connect:
        await _publisher(connect, ["a"])
        viewer = await connect()
        await viewer.subscribe(_NS, "a", wait_response=True)
        pub = await connect()
        PublishedTrack(pub, namespace=ns, trackname="probe", object_size=100,
                       group_size=10, rate=50).attach()
        await pub.publish_namespace(namespace=ns, wait_response=True)
        sub = await connect()
        await sub.subscribe(ns, "probe", wait_response=True)
        assert relay._admission_refused == 0
        assert relay._subscriber_sessions() == 1


# -- reporting and flags ---------------------------------------------

def test_lane_delay_is_backlog_at_the_track_rate():
    lane = SimpleNamespace(bytes=1000, tx_bytes=250)
    assert relay._lane_delay_ms(lane, 10_000) == 1000.0


def test_flags_from_the_config_file(tmp_path):
    path = tmp_path / "relay.toml"
    path.write_text("max-subscribers = 300\n")
    assert relay.parse_args(["--config", str(path)]).max_subscribers == 300
    assert relay.parse_args([]).max_subscribers == 0


@requires_certs
def test_a_negative_limit_stops_the_relay():
    proc = subprocess.run(
        [sys.executable, "-m", "aiomoqt.tools.moq_interop_relay", "--port",
         "30399", "--cert", CERT, "--key", KEY, "--max-subscribers", "-1"],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert "--max-subscribers must be 0 or more" in proc.stderr


# -- the bench client ------------------------------------------------

async def test_retry_wait_honours_the_interval():
    stop = threading.Event()
    assert not await retry_wait(MOQTRequestError(9, "x", retry_interval=0), stop)
    assert not await retry_wait(ValueError("x"), stop)
    t0 = time.monotonic()
    assert await retry_wait(MOQTRequestError(9, "x", retry_interval=1), stop)
    assert time.monotonic() - t0 < 0.05
    t0 = time.monotonic()
    assert await retry_wait(MOQTRequestError(9, "x", retry_interval=201), stop)
    assert time.monotonic() - t0 >= 0.19


async def test_retry_wait_stops_early():
    stop = threading.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    t0 = time.monotonic()
    assert await retry_wait(MOQTRequestError(9, "x", retry_interval=30000), stop)
    assert time.monotonic() - t0 < 2.0
