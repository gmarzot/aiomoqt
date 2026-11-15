"""Request streams ride the control band. From d16 a request and its
replies travel on their own bidi stream; at a data band, queued data would
hold back SUBSCRIBE_OK and the rest."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import STREAM_BAND_CONTROL
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15550
_NS = "prio/requests"
_TRACKS = ("audio", "video")
_DRAFTS = (16, 18, 20, 22)


class _Prod(PublishedTrack):
    async def produce(self, out):
        for o in range(3):
            await out.write(0, o, b"x" * 50, group_start=o == 0)
        await asyncio.Event().wait()    # keep the subscription's stream open


def _spy(monkeypatch, name, calls):
    orig = getattr(_MOQTSessionMixin, name)

    def spy(self, *args):
        ok = orig(self, *args)
        calls.append((self, *args, ok))
        return ok
    monkeypatch.setattr(_MOQTSessionMixin, name, spy)


@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_request_streams_take_the_control_band(
        use_quic, draft, monkeypatch):
    sessions, bands = [], []
    _spy(monkeypatch, "set_default_stream_priority", sessions)
    _spy(monkeypatch, "set_stream_priority", bands)
    _reset_relay_state()
    port = _BASE_PORT + 2 * _DRAFTS.index(draft) + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    requests = {}
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=draft)
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            for name in _TRACKS:
                _Prod(pub, _NS, name).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
            async with sub_client.connect() as sub:
                await sub.client_session_init()
                # d16 puts only SUBSCRIBE_NAMESPACE on a request stream.
                await sub.subscribe_namespace(_NS, wait_response=True)
                for name in _TRACKS:
                    await SubscribedTrack(sub, _NS, name).subscribe(
                        timeout=8.0)
                await asyncio.sleep(0.2)
                for s, *_ in sessions:
                    requests[s] = set(s._bidi_stream_requests)
    finally:
        handle.close()
    control = {(s, sid) for s, sid, p, ok in bands
               if p == STREAM_BAND_CONTROL and ok}
    # Publisher, subscriber and the relay's session with each.
    assert len(requests) == 4, requests
    bound = {(s, sid) for s, sids in requests.items() for sid in sids}
    # d16: both ends of the namespace request; d18+: every SUBSCRIBE too.
    assert len(bound) >= (2 if draft == 16 else 8), requests
    assert bound <= control, bound - control
