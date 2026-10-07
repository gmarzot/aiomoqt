"""The relay's terminal for a track it subscribed upstream: PUBLISH_DONE
follows the last object even when the upstream's streams end before the
relay registers to count them, and a track that ends while its first
subscriber attaches still reaches that subscriber. moqtest_client fails
a request whose PUBLISH_DONE misses the track's expected end by more
than 1 s."""
import asyncio
import contextlib
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.track import SubscribedTrack
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import moqtest_origin as origin

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16100
_SLACK_S = 1.0


def _moq_test_namespace(fp: int, markers: bool) -> str:
    # 3 groups x 5 objects, 10 ms apart.
    fields = ["moq-test-00", fp, 0, 0, 2, 4, 5, 64, 32, 10, 1, 1,
              int(markers), 0, 0, 0]
    return "/".join(str(f) for f in fields)


def _reset_relay():
    relay._announced.clear()
    relay._tracks.clear()
    relay._upstreams.clear()
    relay._upstream_urls.clear()


@contextlib.asynccontextmanager
async def _origin_relay_subscriber(port, draft):
    """moqtest_origin upstream of the relay; yields a session to the relay."""
    _reset_relay()
    up = await origin._build_server("localhost", port, CERT, KEY,
                                    True, draft).serve()
    down = await relay._build_server("localhost", port + 1, CERT, KEY,
                                     True, draft).serve()
    dial = asyncio.create_task(relay._dial_upstream(
        f"moqt://localhost:{port}/", draft))
    try:
        for _ in range(100):
            if relay._upstreams:
                break
            await asyncio.sleep(0.02)
        assert relay._upstreams, "relay never reached the origin"
        client = MOQTClient("localhost", port + 1, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        dial.cancel()
        down.close()
        up.close()
        _reset_relay()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [16, 18])
async def test_publish_done_follows_the_last_object(draft, monkeypatch):
    # The relay's stream-end handler lands after the whole 150 ms track.
    register = _MOQTSessionMixin.register_stream_end_handler

    def late(self, alias, callback):
        asyncio.get_running_loop().call_later(0.2, register, self, alias,
                                              callback)

    monkeypatch.setattr(_MOQTSessionMixin, "register_stream_end_handler",
                        late)
    port = _BASE_PORT + 4 * (draft - 16)
    async with _origin_relay_subscriber(port, draft) as session:
        for fp, markers in ((0, True), (1, False), (2, False)):
            loop = asyncio.get_running_loop()
            arrivals, done = [], loop.create_future()
            track = SubscribedTrack(
                session, _moq_test_namespace(fp, markers), "test",
                on_object=lambda *a: arrivals.append(time.monotonic()),
                on_done=lambda d: done.done() or done.set_result(
                    time.monotonic()))
            track._quiet = True
            await track.subscribe(timeout=5.0)
            done_at = await asyncio.wait_for(done, 5.0)
            assert len(arrivals) == 15, (fp, len(arrivals))
            lag = done_at - max(arrivals)
            assert lag < _SLACK_S, (
                f"fp={fp}: PUBLISH_DONE {lag:.2f}s after the last object")


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [16, 18])
async def test_a_track_that_ends_while_its_subscriber_attaches_reaches_it(
        draft, monkeypatch):
    # The subscriber attaches after the whole 150 ms track has ended.
    establish = relay._establish_upstream

    async def late(ns, track_name):
        track = await establish(ns, track_name)
        await asyncio.sleep(0.2)
        return track

    monkeypatch.setattr(relay, "_establish_upstream", late)
    finished = []
    finish = relay._RelayedTrack.finish

    def spy(self, *args, **kw):
        finished.append((len(self.downstream), self._objects_out))
        return finish(self, *args, **kw)

    monkeypatch.setattr(relay._RelayedTrack, "finish", spy)
    port = _BASE_PORT + 4 * (draft - 16) + 2
    async with _origin_relay_subscriber(port, draft) as session:
        track = SubscribedTrack(session, _moq_test_namespace(0, False),
                                "test", on_object=lambda *a: None)
        track._quiet = True
        await track.subscribe(timeout=5.0)
        for _ in range(150):
            if finished:
                break
            await asyncio.sleep(0.02)
    assert len(finished) == 1 and finished[0][0] == 1, (
        f"(subscribers, items forwarded) at the terminal: {finished}")
    assert finished[0][1] >= 15, finished
