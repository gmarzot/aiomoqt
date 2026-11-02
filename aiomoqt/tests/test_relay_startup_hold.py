"""Relay --startup-hold: for a window after start, a SUBSCRIBE for a track
no publisher serves waits for one, and is answered once it appears; at
the window's end, outside it, or with the hold off, it is refused
DOES_NOT_EXIST at once. Two tracks on one session, raw QUIC and
WebTransport."""
import asyncio
import contextlib
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 29850
_NS = "relay/hold"
_SIZES = {"video": 300, "audio": 100}
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()
    relay._hold_until = None


async def _until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return cond()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, hold):
    """Yields connect() against a relay that started `hold` seconds of
    startup hold just now."""
    _reset()
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic,
                                       draft=(22, 18)).serve()
    relay._begin_startup_hold(hold)
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=(22,))
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            yield connect
    finally:
        handle.close()
        _reset()


async def _answers(rx):
    """(error code or None, seconds) of a SUBSCRIBE per track."""
    async def one(name):
        t0 = time.monotonic()
        try:
            await rx.subscribe(_NS, name, forward=1, wait_response=True)
            code = None
        except MOQTRequestError as e:
            code = int(e.error_code)
        return code, time.monotonic() - t0
    return dict(zip(_SIZES, await asyncio.gather(*map(one, _SIZES))))


@_TRANSPORTS
async def test_a_subscriber_before_its_publisher_gets_the_track(use_quic):
    async with _relay(_PORT + (not use_quic), use_quic, 2.0) as connect:
        rx = await connect()
        got = {name: 0 for name in _SIZES}
        sizes = {size: name for name, size in _SIZES.items()}

        def on_object(m, *_):
            name = sizes.get(len(m.payload))
            if name is not None:
                got[name] += 1
        rx.on_object_received = on_object
        answers = asyncio.create_task(_answers(rx))
        await asyncio.sleep(0.3)
        assert not answers.done()
        pub = await connect()
        for name, size in _SIZES.items():
            PublishedTrack(pub, namespace=_NS, trackname=name,
                           object_size=size, group_size=5,
                           rate=100).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        for name, (code, waited) in (await answers).items():
            assert code is None, (name, code)
            assert 0.3 <= waited < 2.0, (name, waited)
        assert await _until(lambda: all(n > 2 for n in got.values())), got
        assert rx._close_err is None


@_TRANSPORTS
@pytest.mark.parametrize("case", ["off", "after", "expiry"])
async def test_without_a_publisher_the_subscribe_is_refused(use_quic, case):
    hold = {"off": 0, "after": 0.2, "expiry": 0.6}[case]
    port = _PORT + 2 + 2 * ["off", "after", "expiry"].index(case)
    async with _relay(port + (not use_quic), use_quic, hold) as connect:
        rx = await connect()
        if case == "after":
            await asyncio.sleep(0.4)
        for name, (code, waited) in (await _answers(rx)).items():
            assert code == RequestErrorCode.DOES_NOT_EXIST, (name, code)
            if case == "expiry":
                assert 0.3 <= waited < 1.5, (name, waited)
            else:
                assert waited < 0.3, (name, waited)
        assert rx._close_err is None
