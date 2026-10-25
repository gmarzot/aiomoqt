"""--catalog-probe: the relay subscribes to a namespace's catalog when the
namespace is announced (PUBLISH_NAMESPACE) or its catalog is pushed
(PUBLISH), so the dashboard knows the MSF format before any viewer. The
catalog is published before the relay subscribes. Each relay carries two
namespaces, one with an MSF catalog and one without; raw QUIC and
WebTransport."""
import asyncio
import contextlib
import json

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 20900
_MSF = "probe/msf"
_BARE = "probe/bare"
_CATALOG = json.dumps({"version": 1, "tracks": [
    {"name": "video", "packaging": "loc"}]}).encode()
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_FLOWS = pytest.mark.parametrize("flow", ["announce", "publish"])


def _ns(name):
    return tuple(p.encode() for p in name.split("/"))


def _port(case, use_quic):
    return _PORT + 2 * case + (not use_quic)


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()
    relay._catalogs.clear()


class _Groups(PublishedTrack):
    """One object per group, each on its own stream; a subscription gets
    the published objects its window admits, then new ones."""

    def __init__(self, *args, payload, **kwargs):
        super().__init__(*args, **kwargs)
        self.payload = payload

    def write(self, group_id):
        self._remember(group_id, 0, self.payload)

    async def generate(self, session, track_alias):
        sub = self._sub_for(session)
        sent = set()
        while True:
            last = -1 if self._largest is None else self._largest[0]
            for g in range(last + 1):
                if g in sent or not sub.passes(g, 0):
                    continue
                sent.add(g)
                sid = await session.open_uni_stream()
                hdr = SubgroupHeader(track_alias=track_alias, group_id=g,
                                     subgroup_id=0, publisher_priority=128,
                                     extensions_present=True,
                                     prof=session._profile)
                session.stream_write(sid, hdr.serialize().data)
                await session.stream_write_drain(sid, hdr.next_object(
                    payload=self.payload, extensions=None,
                    object_id=0).data)
                session.stream_fin(sid)
            await asyncio.sleep(0.01)


async def _until(cond, timeout=6.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return cond()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, probe, flow):
    """Yields connect(): a relay with the probe on or off, and a publisher
    that published an MSF catalog under _MSF and a non-catalog track
    under _BARE before announcing (or pushing) them."""
    _reset()
    relay._catalog_probe = probe
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=(22, 18))
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=(22,))
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            pub = await connect()
            catalog = _Groups(pub, namespace=_MSF, trackname="catalog",
                              payload=_CATALOG, fetch_history_bytes=1 << 20)
            video = _Groups(pub, namespace=_BARE, trackname="video",
                            payload=b"frame", fetch_history_bytes=1 << 20)
            for t in (catalog, video):
                t.attach()
                t.write(0)
            if flow == "announce":
                for ns in (_MSF, _BARE):
                    await pub.publish_namespace(namespace=ns,
                                                wait_response=True)
            else:
                for t in (catalog, video):
                    await t.publish()
            yield connect
    finally:
        relay._catalog_probe = False
        handle.close()
        _reset()


@_TRANSPORTS
@_FLOWS
async def test_probe_learns_the_catalog_format_before_any_viewer(
        use_quic, flow):
    case = 0 if flow == "announce" else 1
    async with _relay(_port(case, use_quic), use_quic, True,
                      flow) as connect:
        assert await _until(lambda: _ns(_MSF) in relay._catalogs)
        assert relay._catalogs[_ns(_MSF)] == "MSF"
        assert _ns(_BARE) not in relay._catalogs
        # The first viewer gets the catalog the probe already holds.
        viewer = await connect()
        got = []
        await viewer.subscribe(
            _MSF, "catalog", forward=1, wait_response=True,
            filter_type=None,
            on_object=lambda m, _s, _t, g, _sg: got.append(
                (g, m.object_id, bytes(m.payload))))
        assert await _until(lambda: got, timeout=3.0)
        assert got[0] == (0, 0, _CATALOG)
        assert viewer._close_err is None


@_TRANSPORTS
@_FLOWS
async def test_without_the_flag_nothing_is_probed(use_quic, flow):
    case = 2 if flow == "announce" else 3
    async with _relay(_port(case, use_quic), use_quic, False, flow):
        await asyncio.sleep(3.0)
        assert _ns(_MSF) not in relay._catalogs
        track = relay._tracks.get((_ns(_MSF), b"catalog"))
        if flow == "announce":
            assert track is None
        else:
            assert track is not None and track.parked is not None
