"""chain scheduling: each track's streams to a session go in a transport
stream chain, ranked by group, at the track's even band. Two tracks at one
priority through the relay, raw QUIC and WT: every data stream the
publisher and the relay open carries the track's chain and its group, the
transport accepts each chain, and both tracks arrive in group order."""
import asyncio

import pytest

from aiomoqt import protocol
from aiomoqt.client import MOQTClient
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import to_stream_priority
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 15640
_NS = "prio/chain"
_GROUPS = 3
_PER_GROUP = 3


def test_chain_band_is_round_robin():
    assert to_stream_priority(128, discipline="chain") == 128
    assert to_stream_priority(64, discipline="chain") == 64


class _Prod(PublishedTrack):
    async def produce(self, out):
        for g in range(_GROUPS):
            for o in range(_PER_GROUP):
                await out.write(g, o, b"x" * 50, group_start=o == 0)
            await asyncio.sleep(0.05)


def _counters(session):
    tp = getattr(session, "_transport", None)
    return getattr(tp, "counters", None)


@requires_certs
@pytest.mark.skipif(not protocol._STREAM_CHAINS,
                    reason="aiopquic without stream chains")
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_tracks_open_streams_in_their_chains(use_quic, monkeypatch):
    _reset_relay_state()
    monkeypatch.setattr(relay, "STREAM_SCHEDULING", "chain")
    opened = []      # (session, priority, chain, order)
    orig_open = protocol._MOQTSessionMixin.open_uni_stream

    async def spy_open(self, initial=b"", priority=None, end_stream=False,
                       chain=None, order=0):
        opened.append((self, priority, chain, order))
        return await orig_open(self, initial=initial, priority=priority,
                               end_stream=end_stream, chain=chain,
                               order=order)
    monkeypatch.setattr(protocol._MOQTSessionMixin, "open_uni_stream", spy_open)

    port = _BASE_PORT + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    got = {"video": [], "audio": []}
    want = _GROUPS * _PER_GROUP
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=18)
        pub_client.stream_scheduling = "chain"
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            for n in got:
                PublishedTrack.attach(_Prod(pub, _NS, n, priority=128))
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
            async with sub_client.connect() as session:
                await session.client_session_init()
                for name in got:
                    def on_object(m, s, t, g, sg, name=name):
                        got[name].append(g)
                    await SubscribedTrack(session, _NS, name,
                                          on_object=on_object).subscribe(
                                              timeout=8.0)
                async with asyncio.timeout(5.0):
                    while min(len(v) for v in got.values()) < want:
                        await asyncio.sleep(0.02)
                counters = {id(s): dict(_counters(s) or {})
                            for s, *_ in opened}
    finally:
        handle.close()
    data = [o for o in opened if o[2] is not None]
    # Publisher upstream and relay downstream: two tracks, each a group a
    # stream.
    assert len(data) == 2 * 2 * _GROUPS, opened
    for s, band, chain, order in data:
        assert band == 128 and chain >= 1 and 0 <= order < _GROUPS, \
            (band, chain, order)
    by_session = {}
    for s, _, chain, order in data:
        by_session.setdefault(id(s), {}).setdefault(chain, []).append(order)
    assert len(by_session) == 2, by_session
    for chains in by_session.values():
        assert len(chains) == 2, chains
        for orders in chains.values():
            assert orders == list(range(_GROUPS)), chains
    for sid, c in counters.items():
        if sid in by_session and c:
            assert c["set_chain_applied"] >= len(by_session[sid]), c
            assert c["set_chain_rejected"] == 0, c
    assert got["video"] == sorted(got["video"]), got
    assert got["audio"] == sorted(got["audio"]), got
