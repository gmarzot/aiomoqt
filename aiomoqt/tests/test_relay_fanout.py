"""aiomoqt-relay fanout: subscribers in step share one aiopquic fanout()
per variant per shard for a subgroup's objects; the rest take their lanes.

What each subscriber receives must be the same bytes with fanout on and
off: every subgroup stream's header, objects, properties, End of Group
marker, FIN or reset, captured from the transport."""
import asyncio
import contextlib
import inspect
import logging
import sys
import time
from types import SimpleNamespace

import pytest
from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.server import serve as aiopquic_serve
from aiopquic.quic.events import StreamDataReceived, StreamReset

from aiomoqt.client import MOQTClient
from aiomoqt.messages.data import SubgroupHeader
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import ObjectStatus, StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._relay_wait import until_published

pytestmark = requires_certs

_HAS_FANOUT = relay._DestSet is not None
_HAS_WORKERS = (sys.platform.startswith("linux")
                and "workers" in inspect.signature(aiopquic_serve).parameters)
needs_fanout = pytest.mark.skipif(not _HAS_FANOUT,
                                  reason="aiopquic without DestSet")

_BASE_PORT = 26400
_NS = "fanout/ns"
_KEYS = {n: ((b"fanout", b"ns"), n.encode()) for n in ("video", "audio")}
_VIDEO_GROUPS = 6
_VIDEO_OBJECTS = 8
_AUDIO_GROUPS = 12
# Video group the publisher resets after this many objects.
_RESET_GROUP, _RESET_AFTER = 3, 5
_GAP_S = 0.004


def _payload(name, gid, oid, size=None):
    seed = f"{name}:{gid}:{oid}:".encode()
    size = size or 200 + 97 * ((gid * 7 + oid) % 13)
    return (seed * (size // len(seed) + 1))[:size]


def _props(gid, oid):
    # Even type: varint value; odd type: bytes.
    return {0x3E: gid * 100 + oid, 0x3F: b"p%d" % oid} if oid % 3 == 0 \
        else None


class _Source(PublishedTrack):
    """Deterministic subgroup streams, from when `go` is set: video groups
    ending alternately in an End of Group marker and a bare FIN, one of
    them reset; audio one object per stream."""

    def __init__(self, session, name, go, shape):
        super().__init__(session, _NS, name)
        self.go = go
        self.name = name
        self.shape = shape

    async def generate(self, session, track_alias):
        await self.go.wait()
        if self.name == "video":
            await self._video(session, track_alias)
        else:
            await self._audio(session, track_alias)

    def _header(self, session, alias, gid):
        return SubgroupHeader(track_alias=alias, group_id=gid, subgroup_id=0,
                              publisher_priority=100, extensions_present=True,
                              prof=session._profile)

    async def _video(self, session, alias):
        shape = self.shape
        for gid in range(shape.groups):
            sid = await session.open_uni_stream()
            hdr = self._header(session, alias, gid)
            session.stream_write(sid, hdr.serialize().data)
            for oid in range(shape.objects):
                size = shape.sizes.get((gid, oid))
                buf = hdr.next_object(payload=_payload("video", gid, oid,
                                                       size),
                                      extensions=_props(gid, oid),
                                      object_id=oid)
                await session.stream_write_drain(sid, buf.data)
                await asyncio.sleep(shape.gap)
                if gid == shape.reset and oid + 1 == _RESET_AFTER:
                    break
            if gid == shape.reset:
                await asyncio.sleep(0.05)
                session.stream_reset(sid, StreamResetCode.INTERNAL_ERROR)
            elif gid % 2:
                buf = hdr.next_object(payload=b"",
                                      status=ObjectStatus.END_OF_GROUP,
                                      object_id=shape.objects)
                session.stream_write(sid, buf.data, end_stream=True)
            else:
                session.stream_write(sid, b"", end_stream=True)
            await asyncio.sleep(shape.gap)

    async def _audio(self, session, alias):
        for gid in range(_AUDIO_GROUPS):
            sid = await session.open_uni_stream()
            hdr = self._header(session, alias, gid)
            data = hdr.serialize().data + hdr.next_object(
                payload=_payload("audio", gid, 0), object_id=0).data
            await session.stream_write_drain(sid, data, end_stream=True)
            await asyncio.sleep(2 * _GAP_S)


def _shape(groups=_VIDEO_GROUPS, objects=_VIDEO_OBJECTS, gap=_GAP_S,
           reset=_RESET_GROUP, sizes=None):
    return SimpleNamespace(groups=groups, objects=objects, gap=gap,
                           reset=reset, sizes=sizes or {})


class _Capture:
    """Every data stream a session receives, as it arrives: bytes, FIN,
    reset code; and the objects it parses."""

    def __init__(self, session):
        self.session = session
        self.streams: dict = {}
        self.objects: list = []
        real = session.quic_event_received

        def received(event):
            sid = getattr(event, "stream_id", None)
            if sid is not None and sid & 2:
                st = self.streams.setdefault(sid, [bytearray(), False, None])
                if isinstance(event, StreamDataReceived):
                    st[0] += bytes(event.data or b"")
                    st[1] = st[1] or event.end_stream
                elif isinstance(event, StreamReset):
                    st[2] = int(event.error_code)
            return real(event)
        session.quic_event_received = received
        session.on_object_received = (
            lambda msg, _s, _t, gid, _sg: self.objects.append(
                (bytes(msg.payload).split(b":", 1)[0].decode(), gid,
                 msg.object_id, bytes(msg.payload))))

    def data_streams(self):
        """Subgroup streams: their first byte is a SUBGROUP_HEADER type."""
        control = getattr(self.session, "_d18_control_read_sid", None)
        return [st for sid, st in sorted(self.streams.items())
                if sid != control and st[0] and 0x10 <= st[0][0] <= 0x7D]

    def ended(self, n) -> bool:
        streams = self.data_streams()
        return len(streams) >= n and all(st[1] or st[2] is not None
                                         for st in streams)

    def view(self):
        """Streams as compared: whole bytes and FIN, or for a reset the code
        and its header (the bytes before a reset may be cut short)."""
        out = []
        for data, fin, code in self.data_streams():
            if code is None:
                out.append(("fin" if fin else "open", bytes(data)))
            else:
                out.append(("reset", code, bytes(data[:4])))
        return sorted(out)


def _worker_of(session):
    for obj in (session, getattr(session, "_quic", None)):
        transport = getattr(obj, "_transport", None)
        if isinstance(transport, TransportContext):
            return id(transport)
    return None


def _counter(handle, name) -> int:
    return sum(getattr(t, name, 0) for t in relay._transports_of(handle))


async def _session(stack, port, use_quic, draft):
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    s = await stack.enter_async_context(client.connect())
    await s.client_session_init()
    return s


def _downstream_of(track, i):
    """The relay's subscription for the i-th viewer to subscribe."""
    return track.downstream[i] if track is not None and \
        i < len(track.downstream) else None


async def _relay_run(port, use_quic, drafts, workers=1, shape=None,
                     viewers=3, transport=None, during=None, complete=None):
    """Publish video and audio through a relay to `viewers` viewers, the
    i-th at drafts[i % len(drafts)]; with workers, more until every worker
    has one. Returns (run, stats): run.caps and run.subs per viewer.
    `during(run)` runs while objects flow; the viewers in `complete`
    (default all) must get every stream."""
    shape = shape or _shape()
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=[22, 18, 16],
                                 workers=workers, **(transport or {}))
    handle = await server.serve()
    run = SimpleNamespace(caps=[], subs=[], stack=None, tracks={},
                          server=server, handle=handle, port=port,
                          use_quic=use_quic)
    try:
        async with contextlib.AsyncExitStack() as stack:
            run.stack = stack
            pub = await _session(stack, port, use_quic, 18)
            go = asyncio.Event()
            for name in ("video", "audio"):
                await _Source(pub, name, go, shape).publish(
                    announce_namespace=False, publish_track=True)
            await until_published(_NS, ["video", "audio"])
            shards = {id(t) for t in relay._transports_of(handle)}
            while True:
                i = len(run.caps)
                rx = await _session(stack, port, use_quic,
                                    drafts[i % len(drafts)])
                run.caps.append(_Capture(rx))
                run.subs.append({name: await rx.subscribe(
                    _NS, name, forward=1, wait_response=True)
                    for name in ("video", "audio")})
                on = {_worker_of(s) for s in server._live}
                if len(run.caps) >= viewers and (on >= shards
                                                 or len(run.caps) >= 16):
                    break
            assert {_worker_of(s) for s in server._live} >= shards
            run.tracks = {n: relay._tracks.get(k) for n, k in _KEYS.items()}
            n = len(run.caps)
            for _ in range(100):
                if all(t is not None and len(t.downstream) == n
                       for t in run.tracks.values()):
                    break
                await asyncio.sleep(0.02)
            go.set()
            if during is not None:
                await during(run)
            want = shape.groups + _AUDIO_GROUPS
            done = range(n) if complete is None else complete
            for _ in range(500):
                if all(run.caps[i].ended(want) for i in done):
                    break
                await asyncio.sleep(0.02)
            stats = {"fanout_writes": _counter(handle, "fanout_writes"),
                     "fanout_refusals": _counter(handle, "fanout_refusals"),
                     "viewers": n,
                     "objects_out": {k: t._objects_out
                                     for k, t in run.tracks.items()},
                     "bytes_out": {k: t._bytes_out
                                   for k, t in run.tracks.items()},
                     "forwarding": all(t.task is not None
                                       and not t.task.done()
                                       for t in run.tracks.values())}
            run.views = [c.view() for c in run.caps]
            for i in done:
                assert len(run.views[i]) == want, (i, run.views[i])
                assert run.caps[i].objects, i
            return run, stats
    finally:
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


async def _on_and_off(port, use_quic, drafts, monkeypatch, **kw):
    on = await _relay_run(port, use_quic, drafts, **kw)
    with monkeypatch.context() as m:
        m.setattr(relay, "_DestSet", None)
        off = await _relay_run(port + 1, use_quic, drafts, **kw)
    return on, off


def _per_viewer(stats):
    n = stats["viewers"]
    return ({k: v / n for k, v in stats["objects_out"].items()},
            {k: v / n for k, v in stats["bytes_out"].items()})


def _check_on_off(on, off, drafts):
    """Byte identity: viewers of one draft get the same streams, on and
    off; counters per viewer match."""
    (ron, son), (roff, soff) = on, off
    assert son["fanout_writes"] > 0, son
    assert soff["fanout_writes"] == 0, soff
    assert son["forwarding"] and soff["forwarding"]
    for k, draft in enumerate(drafts):
        mine_on = [v for i, v in enumerate(ron.views) if i % len(drafts) == k]
        mine_off = [v for i, v in enumerate(roff.views)
                    if i % len(drafts) == k]
        assert mine_on and mine_off
        assert all(v == mine_on[0] for v in mine_on + mine_off), draft
    assert _per_viewer(son) == _per_viewer(soff)


_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_WORKERS = pytest.mark.parametrize(
    "workers", [1, pytest.param(2, marks=pytest.mark.skipif(
        not _HAS_WORKERS, reason="needs aiopquic workers="))])


def _port(case, use_quic):
    return _BASE_PORT + 4 * case + 2 * (not use_quic)


@needs_fanout
@pytest.mark.asyncio
@_TRANSPORTS
@_WORKERS
@pytest.mark.parametrize("draft", [16, 18, 22])
async def test_viewers_get_the_same_bytes_with_fanout_on_and_off(
        draft, workers, use_quic, monkeypatch):
    case = [16, 18, 22].index(draft) * 2 + workers - 1
    on, off = await _on_and_off(_port(case, use_quic), use_quic, [draft],
                                monkeypatch, workers=workers)
    _check_on_off(on, off, [draft])
    view = on[0].views[0]
    assert sum(1 for s in view if s[0] == "fin") == \
        _VIDEO_GROUPS - 1 + _AUDIO_GROUPS
    assert sum(1 for s in view if s[0] == "reset") == 1
    # At most all of video but each subgroup's first object went by
    # fanout; a viewer whose open is slow joins an object or two later.
    video = (_VIDEO_GROUPS - 1) * (_VIDEO_OBJECTS - 1) + _RESET_AFTER - 1
    most = video * on[1]["viewers"]
    assert most // 2 <= on[1]["fanout_writes"] <= most, on[1]


@needs_fanout
@pytest.mark.asyncio
@_TRANSPORTS
async def test_viewers_at_three_drafts_never_share_a_variant(
        use_quic, monkeypatch):
    drafts = [16, 18, 22]
    seen = []
    real = relay._RelayedTrack._fanout_add

    def add(self, fo, d, lane, skey, entry):
        ok = real(self, fo, d, lane, skey, entry)
        if ok:
            for v in fo.variants.values():
                members = [m for _ds, mem in v.dests.values()
                           for m in mem.values()]
                profs = {id(m.d.session._profile) for m in members}
                states = {m.header.encoding_state() for m in members}
                assert profs <= {id(v.header.prof)}, profs
                assert len(states) <= 1, states
            seen.append(len([v for v in fo.variants.values() if v.n]))
        return ok
    monkeypatch.setattr(relay._RelayedTrack, "_fanout_add", add)
    on, off = await _on_and_off(_port(6, use_quic), use_quic, drafts,
                                monkeypatch, viewers=6)
    assert max(seen) == 3, seen
    _check_on_off(on, off, drafts)
    # d16's bytes differ from d18's and d22's, which match.
    assert len({tuple(v) for v in on[0].views}) == 2


@needs_fanout
@pytest.mark.asyncio
@_TRANSPORTS
async def test_an_object_too_large_for_the_ring_goes_by_lane(
        use_quic, monkeypatch):
    # Object 3 of group 2 exceeds the stream ring: every member is refused
    # it and takes it on its lane, then rejoins at group 4.
    shape = _shape(sizes={(2, 3): 100_000})
    transport = {"stream_ring_cap": 64 << 10,
                 "tx_max_inflight_bytes": 32 << 10}
    on, off = await _on_and_off(_port(7, use_quic), use_quic, [18],
                                monkeypatch, shape=shape,
                                transport=transport)
    assert on[1]["fanout_refusals"] == on[1]["viewers"], on[1]
    _check_on_off(on, off, [18])
    big = [o for o in on[0].caps[0].objects if o[1] == 2 and o[2] == 3]
    assert big and len(big[0][3]) == 100_000


@needs_fanout
@pytest.mark.asyncio
@_TRANSPORTS
async def test_a_viewer_joining_mid_subgroup_takes_its_lane_until_the_next(
        use_quic, monkeypatch):
    adds = []
    real = relay._RelayedTrack._fanout_add

    def add(self, fo, d, lane, skey, entry):
        ok = real(self, fo, d, lane, skey, entry)
        if ok and self.key == _KEYS["video"]:
            adds.append((d.session, skey[1]))
        return ok
    monkeypatch.setattr(relay._RelayedTrack, "_fanout_add", add)
    shape = _shape(groups=4, objects=20, gap=0.01, reset=None)
    late = {}

    async def join(run):
        video = run.tracks["video"]
        for _ in range(300):
            if video.largest is not None and video.largest >= (1, 6):
                break
            await asyncio.sleep(0.005)
        rx = await _session(run.stack, run.port, run.use_quic, 18)
        late["cap"] = _Capture(rx)
        for name in ("video", "audio"):
            await rx.subscribe(_NS, name, forward=1, wait_response=True)
        late["d"] = video.downstream[-1]
        late["group"] = video.largest[0]
    run, stats = await _relay_run(_port(8, use_quic), use_quic, [18],
                                  shape=shape, during=join)
    for _ in range(200):
        if late["cap"].ended(shape.groups - late["group"]):
            break
        await asyncio.sleep(0.02)
    session, joined = late["d"].session, late["group"]
    mine = sorted(g for s, g in adds if s is session)
    assert mine and min(mine) > joined, (joined, mine)
    # Its own lane served the rest of that subgroup, in order, whole.
    got = [(g, o, p) for n, g, o, p in late["cap"].objects if n == "video"]
    assert got and got[0][0] == joined, got[:3]
    assert [(g, o) for g, o, _p in got] == sorted(set(
        (g, o) for g, o, _p in got))
    assert all(p == _payload("video", g, o) for g, o, p in got)
    assert {g for g, _o, _p in got} == set(range(joined, shape.groups))
    # From the next group on, the same bytes as a viewer there from the
    # start.
    later = [v for v in late["cap"].view() if v[0] == "fin"]
    assert set(later[1:]) <= set(run.views[0])


_ACTIONS = ("forward0", "update", "unsubscribe", "stop_sending", "close")


@needs_fanout
@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("action", _ACTIONS)
async def test_a_member_leaves_on(action, use_quic, monkeypatch, caplog):
    """Forward State 0, REQUEST_UPDATE, UNSUBSCRIBE, STOP_SENDING and
    session close each take the viewer out of the fanout at once: nothing
    more is written to that stream by fanout, nothing at all unless its
    subscription goes on, and the others are unaffected."""
    caplog.set_level(logging.WARNING)
    shape = _shape(groups=4, objects=30, gap=0.01, reset=None)
    st = SimpleNamespace(left=None, writes=[], adds=[])
    real_add = relay._RelayedTrack._fanout_add

    def add(self, fo, d, lane, skey, entry):
        ok = real_add(self, fo, d, lane, skey, entry)
        if ok and self.key == _KEYS["video"]:
            st.adds.append((d.session, skey[1]))
        return ok
    monkeypatch.setattr(relay._RelayedTrack, "_fanout_add", add)

    async def act(run):
        video = run.tracks["video"]
        d = video.downstream[0]
        sk = id(d.session)
        for _ in range(400):
            m = next((m for k, m in video._fanout_members.items()
                      if k[0] == sk and k[1] >= 1), None)
            if m is not None and m.variant.bytes - m.base > 3000:
                break
            await asyncio.sleep(0.005)
        assert m is not None, "viewer 0 never joined"
        skey, sid = m.skey, video._streams[m.skey][0]
        real_leave = video._fanout_leave

        def leave(member, last, total):
            real_leave(member, last, total)
            if member.skey == skey and st.left is None:
                st.left = (last, total)
        video._fanout_leave = leave
        session = d.session

        def spy(name):
            fn = getattr(session, name)

            def wrapped(stream_id, data=b"", *a, **kw):
                out = fn(stream_id, data, *a, **kw)
                if st.left is not None and stream_id == sid and data:
                    st.writes.append((name, len(data)))
                return out
            setattr(session, name, wrapped)
        for name in ("stream_write", "stream_write_nowait"):
            spy(name)
        rx = run.caps[0].session
        rid = run.subs[0]["video"].request_id
        if action == "forward0":
            await rx.request_update(rid, forward=0, wait_response=True)
        elif action == "update":
            await rx.request_update(rid, forward=1, wait_response=True)
        elif action == "unsubscribe":
            rx.unsubscribe(rid)
        elif action == "stop_sending":
            rx.stream_stop_sending(sid, StreamResetCode.CANCELLED)
        else:
            rx.close()
        for _ in range(200):
            if st.left is not None:
                break
            await asyncio.sleep(0.005)
        assert st.left is not None, action
        assert skey not in video._fanout_members
        st.skey, st.session = skey, session
    run, stats = await _relay_run(_port(9 + _ACTIONS.index(action), use_quic),
                                  use_quic, [18], shape=shape, during=act,
                                  complete=[1, 2])
    assert stats["forwarding"], stats
    assert run.views[1] == run.views[2]
    if action == "update":
        # The rest of the subgroup from its lane; the next one by fanout.
        assert st.writes, st.writes
        assert run.caps[0].ended(shape.groups + _AUDIO_GROUPS)
        assert run.views[0] == run.views[1]
        assert any(s is st.session and g > st.skey[1] for s, g in st.adds)
    elif action == "stop_sending":
        # The transport refused the stopped stream: nothing committed.
        assert stats["fanout_refusals"] >= 1, stats
        assert not st.writes, st.writes
        assert st.skey in run.tracks["video"]._stopped or not any(
            k == st.skey for k in run.tracks["video"]._streams)
        assert any(s is st.session and g > st.skey[1] for s, g in st.adds)
    else:
        assert not st.writes, st.writes
        assert not any(s is st.session and g > st.skey[1]
                       for s, g in st.adds)
    bad = [r.getMessage() for r in caplog.records
           if r.name.startswith("aiomoqt.relay") and r.levelno >= logging.ERROR]
    assert not bad, bad
# -- the relay's fanout over a fake transport: exact writes per viewer --

_FPS = 30
_RING = 1 << 20
_BUDGET = 512 << 10


class _FakeDestSet:
    """aiopquic DestSet over _FakeTransport: generation-checked indexes."""

    def __init__(self, transport, datagram=False):
        self.transport = transport
        self.ents: dict = {}
        self._next = 0

    def add(self, conn, stream_id):
        dest = conn._fanout_dest(stream_id)
        if dest is None:
            return None
        if dest[0] is not self.transport:
            raise ValueError("destination belongs to another transport")
        self._next += 1
        i = 1 << 32 | self._next
        self.ents[i] = dest
        return i

    def remove(self, index):
        del self.ents[index]

    def stream_id(self, index):
        return self.ents[index][3]

    def __len__(self):
        return len(self.ents)


class _FakeTransport:
    def __init__(self):
        self.calls = []          # [(stream ids written, refused), ...]

    def fanout(self, ds, data, end_stream=False):
        refused, wrote = [], []
        for i, (_t, _k, viewer, sid, _p, _c, _o) in list(ds.ents.items()):
            code = viewer.fanout_code(sid, data)
            if code:
                refused.append((i, code))
            else:
                viewer.put(sid, data, end_stream)
                wrote.append((id(viewer), sid))
        self.calls.append((wrote, refused))
        return refused or None


class _FakeViewer:
    """A downstream session: every byte written per stream, FINs and
    resets, and what its transport still holds. `reads` is the share of
    what it holds the peer reads per tick; `refuse` maps the n-th fanout
    write to a refusal code."""

    def __init__(self, transport, reads=1.0, refuse=None, draft=18):
        from aiomoqt.context import profile_for
        self._profile = profile_for(draft)
        self.transport = transport
        self._next = 3
        self.reads = reads
        self.refuse = refuse or {}
        self.fanned = 0
        self.data: dict = {}
        self.ring: dict = {}
        self.fins, self.resets = [], {}
        self._quic = SimpleNamespace(
            tx_data_ring_used=lambda sid: self.ring.get(sid, 0),
            stream_released=lambda sid: not self.ring.get(sid, 0),
            transport=transport, _fanout_dest=self.fanout_dest)

    def fanout_dest(self, sid):
        if sid not in self.data or sid in self.fins or sid in self.resets:
            return None
        return (self.transport, 0, self, sid, sid, _RING, None)

    def fanout_code(self, sid, data):
        self.fanned += 1
        code = self.refuse.get(self.fanned)
        if code is None and self.ring.get(sid, 0) + len(data) > _RING:
            code = 2
        return code

    def put(self, sid, data, end_stream):
        assert sid not in self.fins and sid not in self.resets, sid
        self.data[sid] = self.data.get(sid, b"") + data
        self.ring[sid] = self.ring.get(sid, 0) + len(data)
        if end_stream:
            self.fins.append(sid)

    def set_stream_priority(self, _sid, _band):
        return True

    async def open_uni_stream(self, initial=b"", priority=None,
                              end_stream=False, **_kw):
        await asyncio.sleep(0)
        sid = self._next
        self._next += 4
        self.data[sid] = b""
        self.put(sid, initial, end_stream)
        return sid

    def open_uni_stream_nowait(self, initial, **_kw):
        return False

    def stream_write_nowait(self, sid, data, end_stream=False, **_kw):
        if self.ring.get(sid, 0) > _BUDGET:
            return False
        self.put(sid, data, end_stream)
        return True

    async def stream_write_drain(self, sid, data, end_stream=False):
        while self.ring.get(sid, 0) > _BUDGET:
            await asyncio.sleep(0.005)
        self.put(sid, data, end_stream)

    def stream_write(self, sid, data, end_stream=False):
        self.put(sid, data, end_stream)

    def stream_reset(self, sid, code):
        self.ring.pop(sid, None)
        self.resets[sid] = int(code)

    def subscribe_done(self, *_a, **_kw):
        pass

    def tick(self):
        """The peer reads its share of what the transport holds, oldest
        stream first."""
        n = int(sum(self.ring.values()) * self.reads) if self.reads < 1 \
            else None
        for sid in sorted(self.ring):
            if n is not None and n < self.ring[sid]:
                self.ring[sid] -= n
                return
            if n is not None:
                n -= self.ring[sid]
            self.ring[sid] = 0

    def streams(self):
        """(bytes, ended as) per stream, in opening order."""
        return [(self.data[sid], self.resets.get(
                    sid, "fin" if sid in self.fins else "open"))
                for sid in sorted(self.data)]


def _msg(gid, oid, payload, status=None, first=False):
    return SimpleNamespace(group_id=gid, object_id=oid, payload=payload,
                           extensions=_props(gid, oid),
                           publisher_priority=100, status=status,
                           stream_flags=(first, False, False))


@pytest.fixture
def fake_relay(monkeypatch):
    """A relayed track feeding fake viewers, all with alias 0, isolated
    from the relay's tables."""
    monkeypatch.setattr(relay, "_lanes", set())
    monkeypatch.setattr(relay, "_lane_due", [])
    monkeypatch.setattr(relay, "_lanes_over_budget", False)
    tracks = []

    def make(viewers, fanout=True, lag=5.0):
        monkeypatch.setattr(relay, "_DestSet",
                            _FakeDestSet if fanout else None)
        monkeypatch.setattr(relay, "_lag_limit", lag)
        track = relay._RelayedTrack(((b"ns",), b"video"))
        tracks.append(track)
        for i, v in enumerate(viewers):
            track.add_downstream(v, 0, i)
        return track
    yield make
    for track in tracks:
        track.close()


async def _settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


async def _feed(track, viewers, groups, objects, size=3000, gap=0.0,
                tick=None, between=None):
    """Objects in arrival order, each group's stream ended by a FIN; the
    viewers read and the lane timer runs after each object."""
    for gid in range(groups):
        for oid in range(objects):
            track.on_object(_msg(gid, oid, _payload("v", gid, oid, size),
                                 first=oid == 0), size, 0, gid, 0)
            await _settle()
            if between is not None:
                between(gid, oid)
            relay._time_lanes(time.monotonic())
            for v in viewers:
                v.tick()
            if gap:
                await asyncio.sleep(gap)
        track.on_stream_end(gid, 0)
        await _settle()
    await _settle(50)


@needs_fanout
async def test_a_refused_member_loses_and_repeats_nothing(fake_relay):
    """Ring full, event ring full and gone: the lane takes the object, and
    the viewer rejoins at the next subgroup."""
    runs = {}
    for fanout in (True, False):
        t = _FakeTransport()
        viewers = [_FakeViewer(t), _FakeViewer(t, refuse={3: 2}),
                   _FakeViewer(t, refuse={2: 1, 5: 2})]
        track = fake_relay(viewers, fanout=fanout)
        await _feed(track, viewers, groups=3, objects=6)
        runs[fanout] = [v.streams() for v in viewers]
        if fanout:
            assert any(refused for _w, refused in t.calls), t.calls
            # The refused viewers were written by fanout again later.
            for v in viewers[1:]:
                assert v.fanned > max(v.refuse), v.fanned
            assert track._objects_out == 3 * 3 * 6
        else:
            assert not t.calls
    on, off = runs[True], runs[False]
    assert on == off
    # One alias, one draft: every viewer gets the same bytes, whole.
    assert on[0] == on[1] == on[2]
    assert [end for _b, end in on[0]] == ["fin"] * 3


@needs_fanout
async def test_a_slow_viewer_is_shed_as_without_fanout(fake_relay):
    """A viewer whose transport holds what it is sent sheds whole groups
    once its oldest unsent object is past the deadline, at the same time
    as without fanout; the others are unaffected."""
    lag = 0.4
    out = {}
    for fanout in (True, False):
        t = _FakeTransport()
        slow = _FakeViewer(t, reads=0.0)
        viewers = [_FakeViewer(t), _FakeViewer(t), slow]
        track = fake_relay(viewers, fanout=fanout, lag=lag)
        shed = {}

        def watch(gid, oid, lane_of=lambda: slow_lane()):
            lane = lane_of()
            if lane is not None and lane.sheds and "at" not in shed:
                shed["at"] = time.monotonic() - shed["t0"]
                shed["obj"] = (gid, oid)

        def slow_lane():
            d = next(d for d in track.downstream if d.session is slow)
            return d.lane
        shed["t0"] = time.monotonic()
        await _feed(track, viewers, groups=4, objects=_FPS // 2, gap=1 / _FPS,
                    between=watch)
        lane = slow_lane()
        out[fanout] = dict(shed, sheds=lane.sheds, floor=lane.floor,
                           resets=set(slow.resets.values()),
                           fast=[v.streams() for v in viewers[:2]],
                           fanned=slow.fanned)
    on, off = out[True], out[False]
    assert on["fanned"] > 0, on
    assert on["sheds"] >= 1 and off["sheds"] >= 1, (on, off)
    assert on["resets"] == off["resets"] == {
        int(StreamResetCode.DELIVERY_TIMEOUT)}
    # Within a lane mark and a frame or two of each other.
    assert abs(on["at"] - off["at"]) <= relay.LANE_MARK_S + 3 / _FPS, (
        on["at"], off["at"])
    assert on["at"] > lag and off["at"] > lag
    assert on["fast"] == off["fast"]
    for streams in on["fast"]:
        assert [end for _b, end in streams] == ["fin"] * 4


@needs_fanout
async def test_a_subgroup_delivery_timeout_still_resets(fake_relay):
    """§8 SUBGROUP_DELIVERY_TIMEOUT: a stream its viewer's transport has
    not released that long after its FIN is reset, fanout or not."""
    runs = {}
    for fanout in (True, False):
        t = _FakeTransport()
        stuck = _FakeViewer(t, reads=0.0)
        viewers = [_FakeViewer(t), _FakeViewer(t), stuck]
        track = fake_relay(viewers, fanout=fanout)
        for d in track.downstream:
            assert d.take_timeout({relay.SUBGROUP_DELIVERY_TIMEOUT: 30})
            track.time_lane(d)
        await _feed(track, viewers, groups=3, objects=6)
        await asyncio.sleep(0.1)
        runs[fanout] = [v.streams() for v in viewers]
        assert (stuck.fanned > 0) is fanout
    assert runs[True] == runs[False]
    on = runs[True]
    assert [end for _b, end in on[2]] == [
        int(StreamResetCode.DELIVERY_TIMEOUT)] * 3
    assert [end for _b, end in on[0]] == ["fin"] * 3

