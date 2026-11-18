"""chain scheduling, the default: each track's streams to a session go in
one transport stream chain, ranked by group, at the track's even band. Two
tracks at one priority through the relay, raw QUIC and WT: every data
stream the publisher and the relay open carries its track's chain and
group, and both tracks arrive in group order. Without stream chains in
aiopquic, chain runs as fifo."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from aiomoqt import protocol
from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTPeer, _MOQTSessionMixin
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import to_stream_priority
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 15640
_NS = "prio/chain"
_GROUPS = 3
_PER_GROUP = 3
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


class _Prod(PublishedTrack):
    async def produce(self, out):
        for g in range(_GROUPS):
            for o in range(_PER_GROUP):
                await out.write(g, o, b"x" * 50, group_start=o == 0)
            await asyncio.sleep(0.05)


def test_chain_band_is_round_robin():
    assert to_stream_priority(128, discipline="chain") == 128
    assert to_stream_priority(64, discipline="chain") == 64


def test_chain_is_the_default():
    assert MOQTPeer().stream_scheduling == "chain"
    assert relay.STREAM_SCHEDULING == "chain"
    assert relay.parse_args([]).stream_scheduling == "chain"


@pytest.mark.parametrize("chains", [True, False])
def test_chain_runs_as_fifo_without_stream_chains(chains, monkeypatch):
    monkeypatch.setattr(protocol, "_STREAM_CHAINS", chains)
    session = NS(_sched=None, _session=NS(stream_scheduling="chain"))
    sched = _MOQTSessionMixin.stream_scheduling.fget(session)
    assert sched == ("chain" if chains else "fifo")
    for track_sched, peer_sched in (("chain", "fifo"), (None, sched)):
        peer = NS(stream_scheduling=peer_sched, stream_chains=chains)
        track = _Prod(peer, _NS, "video", priority=128,
                      stream_scheduling=track_sched)
        chained = chains and "chain" in (track_sched, peer_sched)
        assert track._stream_band(peer) == (128 if chained else 129)
        assert track._chain(peer, 4) == (5 if chained else None)
    track = _Prod(NS(), _NS, "video", priority=128,
                  stream_scheduling="chain")
    track.stream_priority = 130
    assert track._chain(NS(stream_chains=chains), 4) is None


def _counters(session):
    return getattr(getattr(session, "_transport", None), "counters", None)


async def _run(port, use_quic, monkeypatch):
    """Two tracks at one priority through the relay, default scheduling:
    every stream open as (session, band, chain, order), groups received
    per track, and each opening session's transport counters."""
    _reset_relay_state()
    opened = []
    orig_open = _MOQTSessionMixin.open_uni_stream

    async def spy_open(self, initial=b"", priority=None, end_stream=False,
                       chain=None, order=0):
        opened.append((self, priority, chain, order))
        return await orig_open(self, initial=initial, priority=priority,
                               end_stream=end_stream, chain=chain,
                               order=order)
    monkeypatch.setattr(_MOQTSessionMixin, "open_uni_stream", spy_open)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    got = {"video": [], "audio": []}
    want = _GROUPS * _PER_GROUP
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=18)
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            for n in got:
                _Prod(pub, _NS, n, priority=128).attach()
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
    assert all(len(v) >= want for v in got.values()), got
    assert got["video"] == sorted(got["video"]), got
    assert got["audio"] == sorted(got["audio"]), got
    return opened, counters


@requires_certs
@pytest.mark.skipif(not protocol._STREAM_CHAINS,
                    reason="aiopquic without stream chains")
@_TRANSPORTS
async def test_tracks_open_streams_in_their_chains(use_quic, monkeypatch):
    port = _BASE_PORT + (not use_quic)
    opened, counters = await _run(port, use_quic, monkeypatch)
    data = [o for o in opened if o[2] is not None]
    # Publisher upstream and relay downstream: two tracks, a stream a group.
    assert len(data) == 2 * 2 * _GROUPS, opened
    by_session = {}
    for s, band, chain, order in data:
        assert band == 128 and chain >= 1, (band, chain, order)
        by_session.setdefault(id(s), {}).setdefault(chain, []).append(order)
    assert len(by_session) == 2, by_session
    for chains in by_session.values():
        assert len(chains) == 2, chains
        for orders in chains.values():
            assert orders == list(range(_GROUPS)), chains
    for sid, c in counters.items():
        if sid in by_session and "set_chain_applied" in c:
            assert c["set_chain_applied"] >= _GROUPS * 2, c
            assert c["set_chain_rejected"] == 0, c


@requires_certs
@_TRANSPORTS
async def test_without_stream_chains_tracks_run_fifo(use_quic, monkeypatch):
    monkeypatch.setattr(protocol, "_STREAM_CHAINS", False)
    port = _BASE_PORT + 2 + (not use_quic)
    opened, _ = await _run(port, use_quic, monkeypatch)
    data = [o for o in opened if o[1] is not None]
    assert len(data) == 2 * 2 * _GROUPS, opened
    assert all(band == 129 and chain is None
               for _s, band, chain, _o in data), data
