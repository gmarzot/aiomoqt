"""sub_media's FETCH repair: two mpeg2ts tracks on one session, their
subgroups reset by a delivery timeout, filled from the publisher's
history and released in order."""
import asyncio
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media.msfts import MsftsTrackSubscriber, TS_PACKET
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, StreamResetCode
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_delivery_timeout import _stall_once

_DRAFTS = [18, 20, 22]
_GROUPS = 6
_PER_GROUP = 10
_NS = "repair"


def _payload(group_id: int, object_id: int) -> bytes:
    return b'\x47' + bytes([group_id, object_id]) + bytes(TS_PACKET - 3)


class _Paced(PublishedTrack):
    async def produce(self, out):
        for group_id in range(_GROUPS):
            for object_id in range(_PER_GROUP):
                await out.write(group_id, object_id,
                                _payload(group_id, object_id),
                                group_start=object_id == 0)
                await asyncio.sleep(0.01)


def test_a_reset_group_and_a_skipped_group_are_fetched():
    sent = []

    class Session:
        def fetch(self, namespace, track, **kw):
            sent.append((track, kw['start_group'], kw['start_object'],
                         kw['end_group'], kw['end_object'],
                         kw['subscriber_priority']))
            return type('Fetch', (), {'request_id': len(sent)})()

        async def await_fetch_done(self, request_id, timeout=None):
            await asyncio.sleep(3600)

    async def run():
        got = []
        sub = MsftsTrackSubscriber(Session(), _NS, 't', repair_s=0.5,
                                   on_frame=lambda f, g, o: got.append((g, o)))
        obj = type('Obj', (), {'status': None, 'extensions': None})

        def arrive(g, o):
            m = obj()
            m.group_id, m.object_id, m.payload = g, o, _payload(g, o)
            sub._on_object(m, TS_PACKET, 0, g, 0)
        arrive(0, 0)
        arrive(0, 1)
        sub._stream_ended(0, 0, clean=False,
                          reset_code=StreamResetCode.DELIVERY_TIMEOUT)
        arrive(2, 0)
        assert sent == [('t', 0, 2, 0, 0, 0), ('t', 1, 0, 1, 0, 0)]
        assert got == [(0, 0), (0, 1)] and sub.repairs == 2
        m = obj()
        m.group_id, m.object_id, m.payload = 0, 2, _payload(0, 2)
        m.subgroup_id = 0
        sub._on_fetch_object(m, TS_PACKET, 0, 1)
        assert got[-1] == (0, 2) and sub.repaired == 1
        for t in asyncio.all_tasks() - {asyncio.current_task()}:
            t.cancel()
    asyncio.run(run())


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_reset_subgroups_are_repaired_in_order(draft, use_quic):
    port = 17050 + _DRAFTS.index(draft) * 2 + (0 if use_quic else 1)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    rx = {}

    async def on_publish_namespace(session, msg):
        rx["session"] = session
        session.publish_namespace_ok(msg)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            on_publish_namespace)
    quic_server = await server.serve()
    names = ("a", "b")
    got = {n: [] for n in names}
    subs = {}
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            tracks = [_Paced(session, _NS, n, object_size=TS_PACKET,
                             fetch_history_bytes=1 << 20) for n in names]
            for track in tracks:
                track.attach()
            await session.publish_namespace(namespace=_NS,
                                            wait_response=True)
            _stall_once(session, after=12, seconds=0.3)
            for n in names:
                subs[n] = MsftsTrackSubscriber(
                    rx["session"], _NS, n, repair_s=2.0,
                    on_frame=lambda f, g, o, n=n: got[n].append(
                        (g, o, bytes(f.payload))))
                await subs[n].subscribe(parameters={0x02: 100})
            total = _GROUPS * _PER_GROUP
            deadline = time.monotonic() + 30
            while (any(len(v) < total for v in got.values())
                   and time.monotonic() < deadline):
                await asyncio.sleep(0.02)
    finally:
        quic_server.close()

    want = [(g, o, _payload(g, o)) for g in range(_GROUPS)
            for o in range(_PER_GROUP)]
    for n in names:
        s = subs[n]
        state = dict(track=n, got=len(got[n]), repairs=s.repairs,
                     repaired=s.repaired, expired=s.repairs_expired,
                     pending=sorted(g for gs, _ in s._repairing.values()
                                    for g in gs),
                     held=s.order.held, late=s.order.late,
                     skipped=s.order.skipped)
        assert s.repairs >= 1 and s.repaired >= 1, state
        assert got[n] == want, state
