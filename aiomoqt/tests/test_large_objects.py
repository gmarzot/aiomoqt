"""Objects larger than a stream's TX ring.

aiopquic takes a write whole or not at all, up to the stream's ring, so a
larger write goes out in pieces: on the relay (1 MiB by default, below a 4K
keyframe) and on a publisher.
"""
import asyncio
from types import SimpleNamespace

import pytest
from aiopquic.quic.configuration import QuicConfiguration

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.utils.payload import PayloadCheck

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._relay_wait import until_published

_BASE_PORT = 31410
_NS = "big/objs"
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


class _Quic:
    closed = False

    def __init__(self, cap):
        self._configuration = SimpleNamespace(stream_ring_cap=cap)
        self.writes = []

    def _take(self, sid, data, end_stream):
        if len(data) > self._configuration.stream_ring_cap:
            raise ValueError("write exceeds the stream ring")
        self.writes.append((sid, bytes(data), end_stream))

    async def send_stream_data_drained(self, sid, data, end_stream=False):
        self._take(sid, data, end_stream)

    def send_stream_data(self, sid, data, end_stream=False):
        self._take(sid, data, end_stream)

    def tx_data_ring_used(self, sid):
        return 0

    def tx_event_ring_fill(self):
        return 0.0


def _session(cap):
    s = object.__new__(_MOQTSessionMixin)
    s._close_err = None
    s._stopped_streams = set()
    s._session = SimpleNamespace(tx_max_inflight_bytes=None)
    s._quic = _Quic(cap)
    return s


async def test_a_write_over_the_ring_goes_out_in_pieces():
    s = _session(1000)
    data = bytes(range(256)) * 10
    await s.stream_write_drain(3, data, end_stream=True)
    writes = s._quic.writes
    assert b"".join(d for _, d, _ in writes) == data
    assert all(sid == 3 and len(d) <= 500 for sid, d, _ in writes)
    assert [fin for *_, fin in writes] == [False] * (len(writes) - 1) + [True]


async def test_a_write_within_the_ring_is_one_write():
    s = _session(1000)
    await s.stream_write_drain(3, b"x" * 1000)
    assert s._quic.writes == [(3, b"x" * 1000, False)]


def test_nowait_declines_a_write_over_the_ring():
    s = _session(1000)
    assert s.stream_write_nowait(3, b"x" * 1001) is False
    assert s._quic.writes == []
    assert s.stream_write_nowait(3, b"x" * 1000) is True


def _small_ring_client():
    cfg = QuicConfiguration(is_client=True, max_data=2**24,
                            max_stream_data=2**24,
                            max_datagram_frame_size=64 * 1024,
                            stream_ring_cap=64 << 10)
    return {"configuration": cfg, "tx_max_inflight_bytes": 32 << 10}


# (relay transport settings, publisher client settings, object size)
_CASES = {
    "relay-ring": (lambda: {"stream_ring_cap": 64 << 10,
                            "tx_max_inflight_bytes": 32 << 10},
                   dict, 200_000),
    "publisher-ring": (dict, _small_ring_client, 200_000),
    "relay-default-4k-idr": (
        lambda: relay.transport_settings(relay.parse_args([])),
        dict, 1_600_000),
}


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_CASES))
@_TRANSPORTS
async def test_objects_over_the_ring_reach_the_subscriber(case, use_quic):
    """Two tracks on one session, objects past the ring both as a group's
    first object and mid-group: every one arrives intact and no
    subscriber is dropped."""
    relay_cfg, pub_cfg, size = _CASES[case]
    port = _BASE_PORT + 2 * list(_CASES).index(case) + (not use_quic)
    names = ("video", "alt")
    want = 6
    got = {name: [] for name in names}
    check = PayloadCheck()
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18, **relay_cfg())
    handle = await server.serve()
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=18, **pub_cfg())
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            for name in names:
                track = PublishedTrack(pub, namespace=_NS, trackname=name,
                                       object_size=size, group_size=3,
                                       rate=10)
                await track.publish(announce_namespace=False,
                                    publish_track=True)
            await until_published(_NS, names)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
            async with sub_client.connect() as sub:
                await sub.client_session_init()
                def _on_object(name, msg, group_id):
                    # End of Group markers carry no payload.
                    if msg.payload:
                        got[name].append((group_id, msg.object_id,
                                          bytes(msg.payload)))

                for name in names:
                    await SubscribedTrack(
                        sub, _NS, name,
                        on_object=lambda m, s, t, g, sg, n=name:
                            _on_object(n, m, g)
                    ).subscribe(timeout=8.0)
                for _ in range(750):
                    if all(len(got[n]) >= want for n in names):
                        break
                    await asyncio.sleep(0.02)
                assert sub._close_err is None
            drops = {name: relay._tracks[(relay._ns_tuple(_NS),
                                          name.encode())]._drops
                     for name in names}
    finally:
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()
    for name in names:
        objs = got[name]
        assert len(objs) >= want, (name, [(g, o, len(p)) for g, o, p in objs])
        assert all(len(p) == size and check.matches(g, o, p)
                   for g, o, p in objs), name
        assert {o for _, o, _ in objs} >= {0, 1, 2}, name
    assert drops == {name: 0 for name in names}
