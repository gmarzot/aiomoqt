"""Relay forwarding: publisher -> relay -> subscriber, three sessions.

The relay used to be control-plane only, so nothing here could be
tested: it acked SUBSCRIBE and no object ever moved. These exercise the
upstream subscription, the object fan-out, and the §2.4 prefix matching
that decides which publisher a SUBSCRIBE reaches.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15310
_FRAMES = [b"o0", b"o1", b"o2"]


class _Pub(PublishedTrack):
    """Emits _FRAMES as one group on a single subgroup, then stops."""

    async def generate(self, session, track_alias):
        from aiomoqt.messages import SubgroupHeader
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(
            track_alias=track_alias, group_id=0, subgroup_id=0,
            publisher_priority=200, extensions_present=True,
            prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        for i, payload in enumerate(_FRAMES):
            buf = hdr.next_object(payload=payload, extensions=None,
                                  object_id=i)
            await session.stream_write_drain(sid, buf.data)
        self._largest = (0, len(_FRAMES) - 1)


def _reset_relay_state():
    relay._announced.clear()
    relay._tracks.clear()


async def _run(port, pub_ns, sub_ns, draft=18):
    """Publisher announces pub_ns; subscriber subscribes to sub_ns."""
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=draft)
    handle = await server.serve()
    got = []
    try:
        pub_client = MOQTClient("localhost", port, path="/", use_quic=True,
                                verify_tls=False, supported_drafts=draft)
        async with pub_client.connect() as pub_session:
            await pub_session.client_session_init()
            track = _Pub(pub_session, pub_ns, "video")
            await track.publish(announce_namespace=True,
                                publish_track=False)
            await asyncio.sleep(0.1)

            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=True, verify_tls=False,
                                    supported_drafts=draft)
            async with sub_client.connect() as sub_session:
                await sub_session.client_session_init()
                sub = SubscribedTrack(
                    sub_session, sub_ns, "video",
                    on_object=lambda m, s, t, g, sg: got.append(
                        (bytes(m.payload), m.publisher_priority)))
                try:
                    await sub.subscribe(timeout=8.0)
                except Exception as e:
                    return None, e
                for _ in range(150):
                    if len(got) >= len(_FRAMES):
                        break
                    await asyncio.sleep(0.02)
        return got, None
    finally:
        handle.close()
        _reset_relay_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 16, 14])
async def test_objects_traverse_the_relay(draft):
    got, err = await _run(_BASE_PORT + 10 + draft, "relay/ns", "relay/ns",
                          draft=draft)
    assert err is None, f"d{draft} subscribe failed: {err}"
    assert [p for p, _ in got] == _FRAMES


@pytest.mark.asyncio
async def test_subscribe_reaches_a_prefix_publisher():
    # §2.4: announcing (relay) must serve a SUBSCRIBE for (relay, sub).
    got, err = await _run(_BASE_PORT + 1, "relay", "relay/sub")
    assert err is None, f"prefix subscribe failed: {err}"
    assert [p for p, _ in got] == _FRAMES


@pytest.mark.asyncio
async def test_unannounced_namespace_still_errors():
    got, err = await _run(_BASE_PORT + 2, "relay/ns", "other/ns")
    assert err is not None, "subscribe to an unannounced namespace was acked"


@pytest.mark.asyncio
async def test_publisher_priority_survives_the_relay():
    """A relay forwards the publisher's priority, not one of its own —
    a subscriber's scheduling depends on it."""
    got, err = await _run(_BASE_PORT + 30, "relay/ns", "relay/ns", draft=18)
    assert err is None, f"subscribe failed: {err}"
    assert [prio for _, prio in got] == [200] * len(_FRAMES), got
