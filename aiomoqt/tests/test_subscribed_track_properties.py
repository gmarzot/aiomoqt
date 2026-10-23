"""SubscribedTrack keeps the Track Properties of its SUBSCRIBE_OK or
PUBLISH: top-level as received, Immutable Properties (0x0B) decoded, and
both merged; the first object already sees them."""
import asyncio

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages import MOQTMessage
from aiomoqt.messages.publish import Publish
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.utils.buffer import Buffer

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_immutable_properties import _loopback

pytestmark = requires_certs

_BASE_PORT = 17600
_DRAFTS = [16, 18, 20]
_NS = "rxprops"
_TOP = {"video": {0x2C7A51E0: 30000, 0x3F: b"v-top"},
        "audio": {0x2C7A51E0: 48000, 0x41: b"a-top"}}
_IMM = {"video": {0x3B: b"v-imm", 0x04: 7},
        "audio": {0x3D: b"a-imm", 0x06: 9}}


def _port(case, draft, use_quic):
    return _BASE_PORT + 10 * case + 2 * _DRAFTS.index(draft) + (not use_quic)


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_subscribed_tracks_keep_their_own_properties(draft, use_quic):
    publishes = asyncio.Queue()
    async with _loopback(_port(0, draft, use_quic), draft, use_quic,
                         publishes) as session:
        for name in _TOP:
            PublishedTrack(session, namespace=_NS, trackname=name,
                           object_size=64, group_size=5, rate=200,
                           track_properties=_TOP[name],
                           immutable_properties=_IMM[name]).attach()
        await session.publish_namespace(namespace=_NS, wait_response=True)
        peer, _ = await asyncio.wait_for(publishes.get(), 5.0)
        seen = {name: [] for name in _TOP}
        subs = {}
        for name in reversed(list(_TOP)):
            sub = SubscribedTrack(peer, _NS, name)
            sub.on_object = (lambda m, *a, _n=name, _s=sub:
                             seen[_n].append(dict(_s.properties)))
            subs[name] = sub
            await sub.subscribe()
        for _ in range(200):
            if all(seen.values()):
                break
            await asyncio.sleep(0.01)
        for name, sub in subs.items():
            merged = {**_TOP[name], **_IMM[name]}
            top = dict(sub.track_properties)
            assert top.pop(MOQTMessage.IMMUTABLE_PROPERTIES)
            assert top == _TOP[name], name
            assert sub.immutable_properties == _IMM[name], name
            assert sub.properties == merged, name
            assert sub.property_duplicates == frozenset()
            assert seen[name] and seen[name][0] == merged, name


def _kvps(props, vi64):
    buf = Buffer(capacity=256, vi64=vi64)
    MOQTMessage._extensions_encode(buf, props, with_length=False, delta=True)
    return bytes(buf.data_slice(0, buf.tell()))


@pytest.mark.parametrize("draft", [16, 18])
def test_publish_properties_are_kept_and_overlap_is_reported(draft):
    vi64 = profile_for(draft).vi64
    sub = SubscribedTrack(type("S", (), {"_profile": profile_for(draft)})(),
                          _NS, "video")
    sub.take_track_properties(Publish(track_extensions={
        0x2C7A51E0: 1000, 0x0B: _kvps({0x2C7A51E0: 1000, 0x3B: b"x"},
                                      vi64)}))
    assert sub.properties == {0x2C7A51E0: 1000, 0x3B: b"x"}
    assert sub.property_duplicates == frozenset({0x2C7A51E0})
