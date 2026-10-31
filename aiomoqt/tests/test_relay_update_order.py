"""A REQUEST_UPDATE sent right after its SUBSCRIBE, while the relay still
waits on the upstream, is answered after the SUBSCRIBE: at d18 both
replies share the request stream, so an early answer would be read as
the SUBSCRIBE's."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import RequestOk, SubscribeOk
from aiomoqt.types import MOQTRequestError, RequestErrorCode
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _Pub, _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15470
_NS = "upd/order"
_TRACKS = ("video", "audio")


def _slow_upstream(monkeypatch):
    establish = relay._establish_upstream

    async def late(ns, track_name, **kw):
        await asyncio.sleep(0.3)
        return await establish(ns, track_name, **kw)
    monkeypatch.setattr(relay, "_establish_upstream", late)


async def _subscribe_then_update(session, name, order):
    msg = session.subscribe(_NS, name, wait_response=False)
    sub = asyncio.ensure_future(session._await_response(msg.request_id))
    # Over WT the request stream opens asynchronously; the update rides it.
    while msg.request_id not in session._bidi_streams:
        await asyncio.sleep(0.005)
    upd = asyncio.ensure_future(session.request_update(
        msg.request_id, forward=0, wait_response=True))
    sub.add_done_callback(lambda _: order.append((name, "subscribe")))
    upd.add_done_callback(lambda _: order.append((name, "update")))
    return await asyncio.gather(sub, upd, return_exceptions=True)


async def _run(port, use_quic, names, check):
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
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
                order = []
                async with asyncio.timeout(8.0):
                    results = await asyncio.gather(*(
                        _subscribe_then_update(session, n, order)
                        for n in names))
                check(dict(zip(names, results)), order)
                assert session._close_err is None
    finally:
        handle.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_update_is_answered_after_its_pending_subscribe(
        use_quic, monkeypatch):
    _slow_upstream(monkeypatch)

    def check(results, order):
        for name, (sub, upd) in results.items():
            assert isinstance(sub, SubscribeOk), (name, sub)
            assert isinstance(upd, RequestOk), (name, upd)
            assert (order.index((name, "subscribe"))
                    < order.index((name, "update"))), order
        downstream = [d for t in relay._tracks.values()
                      for d in t.downstream]
        assert len(downstream) == len(_TRACKS)
        assert not any(d.forward for d in downstream)   # FORWARD=0 applied

    await _run(_BASE_PORT + (not use_quic), use_quic, _TRACKS, check)


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_update_to_a_refused_subscribe_never_overtakes_it(
        use_quic, monkeypatch):
    _slow_upstream(monkeypatch)

    def check(results, order):
        sub, upd = results["missing"]
        assert isinstance(sub, MOQTRequestError), sub
        assert sub.error_code != RequestErrorCode.NOT_SUPPORTED, sub
        assert isinstance(upd, MOQTRequestError), upd

    await _run(_BASE_PORT + 2 + (not use_quic), use_quic,
               ("missing",), check)
