"""Every session, client and server, raw QUIC and WT, starts data streams
in an even band, which picoquic serves round-robin, so a per-object stream
(audio) is not held behind a group's long stream (video) as picoquic's
default FIFO band does. Through the relay, both legs inherit it."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.track import SubscribedTrack
from aiomoqt.types import STREAM_BAND_DATA_FLOOR
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _Pub, _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15490
_NS = "prio/rr"
_RR = _MOQTSessionMixin.DEFAULT_DATA_STREAM_PRIORITY
_TRACKS = ("video", "audio")


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_sessions_default_to_the_round_robin_band(
        use_quic, monkeypatch):
    calls = []
    orig = _MOQTSessionMixin.set_default_stream_priority

    def spy(self, priority):
        ok = orig(self, priority)
        calls.append((priority, ok))
        return ok
    monkeypatch.setattr(_MOQTSessionMixin, "set_default_stream_priority", spy)

    _reset_relay_state()
    port = _BASE_PORT + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    got = {name: 0 for name in _TRACKS}
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=18)
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            for name in _TRACKS:
                _Pub(pub, _NS, name).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
            async with sub_client.connect() as session:
                await session.client_session_init()
                for name in _TRACKS:
                    def on_object(m, s, t, g, sg, name=name):
                        got[name] += 1
                    await SubscribedTrack(session, _NS, name,
                                          on_object=on_object).subscribe(
                                              timeout=8.0)
                async with asyncio.timeout(5.0):
                    while min(got.values()) < len(_Pub.frames):
                        await asyncio.sleep(0.02)
    finally:
        handle.close()

    assert all(n >= len(_Pub.frames) for n in got.values()), got
    # Publisher, subscriber and the relay's two sessions: one call each.
    assert len(calls) == 4, calls
    assert {p for p, _ in calls} == {_RR}
    assert all(ok for _, ok in calls), calls          # transport took it
    assert _RR % 2 == 0                               # round-robin band
    assert _RR >= STREAM_BAND_DATA_FLOOR
