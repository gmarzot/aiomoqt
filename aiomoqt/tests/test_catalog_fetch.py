"""MediaPublisher answers a catalog FETCH from what it has published: the
catalog objects in range at their own Locations, and a FETCH_OK End that
covers them, so a FETCH starting after group 0 neither trips the End
before Start close nor loses its objects."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import Catalog, CatalogTrack, MediaPublisher
from aiomoqt.messages import LocationFilter
from aiomoqt.server import MOQTServer
from aiomoqt.types import FetchType, MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17300
_NS = "catalog/fetch"
_DRAFTS = [16, 18, 20]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _catalog(n):
    return Catalog(generatedAt=n, tracks=[
        CatalogTrack(name="video", packaging="loc", isLive=True,
                     codec="avc1.640028")])


def _port(case, draft, use_quic):
    return _BASE_PORT + 6 * case + 2 * _DRAFTS.index(draft) + (not use_quic)


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, pubs):
    def _pub(session):
        if id(session) not in pubs:
            pubs[id(session)] = MediaPublisher(session, _NS, _catalog(0))
        return pubs[id(session)]

    async def on_subscribe(session, msg):
        await _pub(session)._demux_subscribe(session, msg)

    async def on_fetch(session, msg):
        await _pub(session)._demux_fetch(session, msg)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    server.register_handler(MOQTMessageType.FETCH, on_fetch)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        handle.close()


async def _until(pred, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


async def _three_groups(session, pubs):
    """Subscribe to the catalog and publish two more: groups 0..2. Returns
    the subscription's Request ID."""
    live = []
    session.on_object_received = (
        lambda obj, size, ts, gid, sgid: live.append(gid))
    ok = await session.subscribe(_NS, "catalog", wait_response=True)
    pub, = pubs.values()
    for n in (1, 2):
        await pub.catalog_track.publish_catalog(_catalog(n))
    await _until(lambda: 2 in live)
    return ok.request_id


@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_absolute_catalog_fetch_after_group_0(draft, use_quic):
    pubs, got = {}, []
    async with _loopback(_port(0, draft, use_quic), draft, use_quic,
                         pubs) as session:
        await _three_groups(session, pubs)
        if draft >= 20:
            msg = session.fetch(_NS, "catalog",
                                location_filter=LocationFilter.absolute(1, 0))
        else:
            msg = session.fetch(_NS, "catalog", start_group=1,
                                start_object=0, end_group=2, end_object=0)
        session.register_fetch_object_handler(
            msg.request_id, lambda obj, *_: got.append(
                (obj.group_id, obj.object_id, bytes(obj.payload))))
        ok = await session._await_response(msg.request_id)
        assert await session.await_fetch_done(msg.request_id, timeout=3.0)
        assert (ok.largest_group_id, ok.largest_object_id) == (2, 1)
        assert [(g, o) for g, o, _ in got] == [(1, 0), (2, 0)]
        assert [Catalog.from_json(p).generatedAt for *_, p in got] == [1, 2]
        assert session._close_err is None


@_TRANSPORTS
@pytest.mark.parametrize("draft", [16, 18])
async def test_relative_joining_catalog_fetch(draft, use_quic):
    pubs, got = {}, []
    async with _loopback(_port(1, draft, use_quic), draft, use_quic,
                         pubs) as session:
        session.unsubscribe(await _three_groups(session, pubs))
        await asyncio.sleep(0.1)
        session.on_fetch_object = lambda obj, *_: got.append(
            (obj.group_id, obj.object_id))
        # Joining Location (2, 0): one group back is groups 1 and 2.
        _, fetch = await session.join(_NS, "catalog", joining_start=1,
                                      fetch_type=FetchType.RELATIVE_JOINING,
                                      wait_response=True)
        assert await session.await_fetch_done(fetch.request_id, timeout=3.0)
        assert got == [(1, 0), (2, 0)]
        assert session._close_err is None
