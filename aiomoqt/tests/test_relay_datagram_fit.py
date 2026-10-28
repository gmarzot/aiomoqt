"""Relay datagram fit: an object larger than a subscriber session's
datagram limit is dropped for that subscriber, never sent on a stream
(d18 §11 / d22: an Object is sent per its Forwarding Preference; one too
large for the session is dropped without notification)."""
import asyncio
import contextlib
import logging
import time

import pytest

from aiopquic.asyncio.webtransport import WebTransportSession

from aiomoqt.client import MOQTClient
from aiomoqt.messages.data import ObjectDatagram
from aiomoqt.track import PublishedTrack
from aiomoqt.types import ForwardingPreference
from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = [requires_certs, pytest.mark.asyncio]

_PORT = 26600
_NS = "relay/dgfit"
_WARNING = "dropping datagram objects"


def _clear():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._dgram_drops):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture(autouse=True)
def relay_tables():
    _clear()
    yield
    _clear()


class _Manual(PublishedTrack):
    """A datagram track whose objects the test sends itself."""

    def __init__(self, *a, **kw):
        super().__init__(*a, forwarding=ForwardingPreference.DATAGRAM, **kw)
        self.alias = None
        self.ready = asyncio.Event()

    async def generate(self, session, track_alias):
        self.alias = track_alias
        self.ready.set()
        await session.async_closed()

    def send(self, gid, oid, size):
        payload = bytes([oid]) * size
        obj = ObjectDatagram(track_alias=self.alias, group_id=gid,
                             object_id=oid, payload=payload)
        assert self.session.send_dgram_message(
            obj.serialize(prof=self.session._profile)) > 0


@contextlib.asynccontextmanager
async def _relay(port, draft, names, streamed=()):
    """A dual-stack relay, a raw-QUIC publisher of the datagram tracks
    `names` (and the subgroup tracks `streamed`), and a WebTransport
    subscriber to all of them. Yields (tracks, got, downstream session),
    `got` mapping each name to the (group, object, size) received."""
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=draft)
    handle = await server.serve_dual()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def _session(use_quic):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                assert s._is_wt is (not use_quic)
                return s

            pub = await _session(True)
            tracks = {}
            for name in names:
                tracks[name] = _Manual(pub, namespace=_NS, trackname=name,
                                       object_size=1, group_size=10, rate=1)
            for name in streamed:
                tracks[name] = PublishedTrack(
                    pub, namespace=_NS, trackname=name, object_size=100,
                    group_size=5, rate=100,
                    forwarding=ForwardingPreference.SUBGROUP)
            for track in tracks.values():
                await track.publish(announce_namespace=False,
                                    publish_track=True)
            rx = await _session(False)
            got = {name: [] for name in tracks}
            for name in tracks:
                await rx.subscribe(
                    _NS, name, forward=1, wait_response=True,
                    on_object=lambda m, _s, _t, gid, _sg, n=name: got[n].append(
                        (gid, m.object_id, len(m.payload or b""))))
            for name in names:
                await asyncio.wait_for(tracks[name].ready.wait(), 5)
            key = (tuple(p.encode() for p in _NS.split("/")),
                   names[0].encode())
            ds = relay._tracks[key].downstream[0].session
            assert ds._is_wt
            yield tracks, got, ds
            assert rx._close_err is None
    finally:
        handle.close()


def _payload_limit(ds, alias, gid, oid):
    """Largest payload whose relayed datagram fits `ds`."""
    one = ObjectDatagram(track_alias=alias, group_id=gid, object_id=oid,
                         payload=b"x").serialize(prof=ds._profile)
    return ds.datagram_max_payload() - (len(one.data) - 1)


def _alias_on(ds, name):
    key = (tuple(p.encode() for p in _NS.split("/")), name.encode())
    return next(d.alias for d in relay._tracks[key].downstream
                if d.session is ds)


async def _until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)


def _row(ds):
    status = relay._Status("/nonexistent/status.json",
                           {"started": time.time()})
    snap = status.snapshot(asyncio.get_running_loop().time(), time.time())
    ids = [i["id"] for i in relay._sessions.values() if i["session"] is ds]
    return next(r for r in snap["sessions"] if r["id"] in ids)


def _warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and _WARNING in r.getMessage()]


@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("limit_api", ["fallback", "aiopquic"])
async def test_oversize_datagram_objects_are_dropped_per_subscriber(
        draft, limit_api, monkeypatch, caplog):
    if limit_api == "aiopquic":
        # aiopquic's own WT limit, below the transport's.
        monkeypatch.setattr(WebTransportSession, "max_datagram_payload",
                            lambda self: 600, raising=False)
    caplog.set_level(logging.WARNING)
    port = _PORT + 4 * (draft == 22) + 2 * (limit_api == "aiopquic")
    async with _relay(port, draft, ["big", "small"]) as (tracks, got, ds):
        if limit_api == "aiopquic":
            assert ds.datagram_max_payload() == 600
        big, small = tracks["big"], tracks["small"]
        fit = _payload_limit(ds, _alias_on(ds, "big"), 0, 1)
        assert fit > 100
        sends = [(0, 1, 100), (0, 2, fit), (0, 3, fit + 1), (0, 4, fit + 1),
                 (0, 5, 100), (1, 1, 100)]
        for i, (gid, oid, size) in enumerate(sends):
            big.send(gid, oid, size)
            small.send(gid, oid, 50)
            await asyncio.sleep(0.01)
        want_big = [(0, 1, 100), (0, 2, fit), (0, 5, 100), (1, 1, 100)]
        await _until(lambda: len(got["big"]) >= len(want_big)
                     and len(got["small"]) >= len(sends))
        await asyncio.sleep(0.2)
        assert sorted(got["big"]) == want_big
        assert sorted(got["small"]) == [(g, o, 50) for g, o, _ in sends]
        assert relay._dgram_drops[id(ds)] == 2
        assert _row(ds)["dgram_drops"] == 2
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("mode", ["no_limit", "not_implemented",
                                  "value_error"])
async def test_a_session_without_datagram_tx_drops_rather_than_streams(
        draft, mode, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    port = _PORT + 10 + 2 * (draft == 22) + 4 * ["no_limit", "not_implemented",
                                                  "value_error"].index(mode)
    async with _relay(port, draft, ["dg"], streamed=["st"]) as (
            tracks, got, ds):
        if mode == "no_limit":
            monkeypatch.setattr(ds, "datagram_max_payload", lambda: 0)
        else:
            exc = NotImplementedError if mode == "not_implemented" else ValueError

            def _refuse(buf):
                raise exc("no datagram TX")
            monkeypatch.setattr(ds, "send_dgram_message", _refuse)
        for oid in range(1, 6):
            tracks["dg"].send(0, oid, 100)
            await asyncio.sleep(0.01)
        await _until(lambda: relay._dgram_drops.get(id(ds), 0) >= 5
                     and len(got["st"]) >= 10)
        await asyncio.sleep(0.2)
        assert got["dg"] == []
        assert len(got["st"]) >= 10
        assert relay._dgram_drops[id(ds)] == 5
        assert _row(ds)["dgram_drops"] == 5
    assert len(_warnings(caplog)) == 1
