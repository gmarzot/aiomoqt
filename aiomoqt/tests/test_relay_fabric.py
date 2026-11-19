"""aiomoqt-relay as a moq-fabric node: fabric tracks to the relay's MoQT
subscribers at d18, d20 and d22 over raw QUIC and WebTransport, two
tracks on one session; local publishers (PUBLISH and namespace-only) to a
fabric subscriber; the fabric subscribe withdrawn at --pause-after and
re-advertised; a fabric withdraw ending the track downstream; a dropped
peer redialed; a stuck fabric hop not stalling local viewers; transit
to a far node beside the relay's own viewers, each leaving without loss
to the other; GET /fabric and POST /fabric/peer. Every case counts
objects and duplicates."""
import asyncio
import contextlib
import json

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import ObjectStatus
from aiomoqt.peering import FabricNode
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._relay_wait import until_published

pytestmark = requires_certs

_PORT = 34100
_FABRIC_PORT = 34400
NS = "fab/live"
NS_T = (b"fab", b"live")
NAMES = ("video", "audio")
TIMEOUT = 10.0
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._establishing.clear()
    relay._track_subs.clear()
    relay._catalogs.clear()


async def _until(cond, timeout=TIMEOUT):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _relay_knobs(monkeypatch):
    monkeypatch.setattr(relay, "_pause_after", relay.PAUSE_AFTER_S)
    monkeypatch.setattr(relay, "_lag_limit", relay.LAG_LIMIT_S)
    monkeypatch.setattr(relay, "FABRIC_WITHDRAW_GRACE_S", 2.0)
    yield
    assert relay._fabric is None


@contextlib.asynccontextmanager
async def _relay(port, use_quic=True, draft=18, *, fabric_port=0,
                 peers=()):
    """An in-process relay with its fabric node; yields (fabric,
    connect), connect() opening a MoQT session to the relay."""
    _reset()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        fabric = await relay._start_fabric(
            peers=peers, port=fabric_port, bind="127.0.0.1", cert=CERT,
            key=KEY, redial_interval=0.2)
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            try:
                yield fabric, connect
            finally:
                await relay._stop_fabric()
    finally:
        handle.close()
        _reset()


def _payload(name, g, o) -> bytes:
    return f"{name}:{g}:{o}".encode().ljust(200, b".")


class _Feeder:
    """Writes groups of `per` objects to fabric tracks until stopped."""

    def __init__(self, tracks, per=5, period=0.004):
        self.tracks = tracks
        self.groups = 0
        self.task = asyncio.create_task(self._run(per, period))

    async def _run(self, per, period):
        g = 0
        while True:
            for o in range(per):
                for name, t in self.tracks.items():
                    await t.write(g, 0, o, _payload(name, g, o))
                await asyncio.sleep(period)
            for t in self.tracks.values():
                await t.end_subgroup(g, 0, end_of_group=per)
            g += 1
            self.groups = g

    async def stop(self):
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


class _Viewer:
    """MoQT subscriptions to fabric tracks on one session: objects per
    track as (group, object), parsed from the payload."""

    def __init__(self, session, names=NAMES):
        self.session = session
        self.got = {n: [] for n in names}
        self.done = {n: None for n in names}
        self.subs = {}

    def _on_object(self, msg, *_a):
        payload = bytes(msg.payload or b"")
        if not payload:
            return
        name, g, o = payload.rstrip(b".").decode().split(":")
        self.got[name].append((int(g), int(o)))

    async def subscribe(self, name):
        def done(d, n=name):
            self.done[n] = d
        sub = SubscribedTrack(self.session, NS, name,
                              on_object=self._on_object, on_done=done)
        sub._quiet = True
        await sub.subscribe(timeout=5.0)
        self.subs[name] = sub
        return sub


def _complete(got, per=5):
    """Non-zero, no duplicates, nothing missing from the first object."""
    assert got, "no objects"
    assert len(got) == len(set(got)), "duplicates"
    first, last = min(got), max(got)
    want = {(g, o) for g in range(first[0], last[0] + 1)
            for o in range(per) if first <= (g, o) <= last}
    missing = sorted(want - set(got))
    assert not missing, f"missing {missing[:10]} of {len(want)}"


class _Sink:
    """A fabric pull: (group, object) of each normal object."""

    def __init__(self):
        self.got = []

    def on_object(self, where, obj):
        if obj.status == ObjectStatus.NORMAL:
            self.got.append((obj.group_id, obj.object_id))

    def on_stream_end(self, session_id, stream_id, how):
        pass


def _no_dups(got):
    assert got, "no objects"
    assert len(got) == len(set(got)), "duplicates"


def _pub_node(fabric_port):
    return FabricNode(label="pub", peers=[("127.0.0.1", fabric_port)],
                      redial_interval=0.2)


# -- fabric publisher -> relay -> MoQT subscribers -----------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 20, 22])
@_TRANSPORTS
async def test_fabric_tracks_reach_moqt_subscribers(draft, use_quic):
    case = [18, 20, 22].index(draft) * 2 + (not use_quic)
    port, fport = _PORT + case, _FABRIC_PORT + case
    async with _relay(port, use_quic, draft, fabric_port=fport) as (
            fabric, connect):
        pub = _pub_node(fport)
        async with pub:
            await _until(lambda: pub.connected() and fabric.node.connected())
            tracks = {n: pub.publish(NS, n) for n in NAMES}
            await _until(lambda: all(fabric.covers(NS_T, n.encode())
                                     for n in NAMES))
            feeder = _Feeder(tracks)
            viewer = _Viewer(await connect())
            for n in NAMES:
                await viewer.subscribe(n)
            await _until(lambda: all(t.subscribed for t in tracks.values()))
            g0 = feeder.groups
            await _until(lambda: feeder.groups >= g0 + 6)
            await feeder.stop()
            await asyncio.sleep(0.2)
            for n in NAMES:
                _complete(viewer.got[n])
                track = relay._tracks[(NS_T, n.encode())]
                assert relay._fabric_fed(track) and track._multi
            assert relay._fabric_fed(relay._tracks[(NS_T, b"video")])


# -- local publishers -> fabric subscriber -------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["publish", "announce"])
@pytest.mark.parametrize("draft", [18, 20])
@_TRANSPORTS
async def test_local_publishers_reach_a_fabric_subscriber(flow, draft,
                                                          use_quic):
    case = (20 + 4 * (flow == "announce") + 2 * (draft == 20)
            + (not use_quic))
    port, fport = _PORT + case, _FABRIC_PORT + case
    async with _relay(port, use_quic, draft, fabric_port=fport) as (
            fabric, connect):
        pub = await connect()
        for n in NAMES:
            t = PublishedTrack(pub, namespace=NS, trackname=n,
                               object_size=300, group_size=5, rate=200)
            if flow == "publish":
                await t.publish(announce_namespace=False, publish_track=True)
            else:
                t.attach()
        if flow == "publish":
            await until_published(NS, NAMES)
        else:
            await pub.publish_namespace(namespace=NS, wait_response=True)
        rx = _pub_node(fport)
        async with rx:
            kind = "namespace" if flow == "announce" else "track"
            await _until(lambda: any(
                (a.is_namespace if kind == "namespace" else a.name == b"video")
                for a in rx.remote_announces()))
            sinks = {n: _Sink() for n in NAMES}
            for n in NAMES:
                rx.pull(NS, n, sinks[n])
            await _until(lambda: all(len(s.got) > 20 for s in sinks.values()))
            for n in NAMES:
                _no_dups(sinks[n].got)
                key = (NS_T, n.encode())
                assert key in fabric.egress and fabric.egress[key].hops
                assert not relay._fabric_fed(relay._tracks[key])
            rows = {r["name"]: r for r in fabric.report()["announces"]}
            assert rows[("video" if kind == "track" else "")]["kind"] == kind
            for n in NAMES:
                rx.unpull(NS, n)
            await _until(lambda: not fabric.egress)


# -- last viewer withdraws at --pause-after, then resumes ---------------

@pytest.mark.asyncio
async def test_last_viewer_withdraws_at_pause_after_then_resumes(
        monkeypatch):
    monkeypatch.setattr(relay, "_pause_after", 0.3)
    port, fport = _PORT + 40, _FABRIC_PORT + 40
    async with _relay(port, fabric_port=fport) as (fabric, connect):
        pub = _pub_node(fport)
        async with pub:
            await _until(lambda: pub.connected() and fabric.node.connected())
            tracks = {n: pub.publish(NS, n) for n in NAMES}
            await _until(lambda: all(fabric.covers(NS_T, n.encode())
                                     for n in NAMES))
            feeder = _Feeder(tracks)
            first = _Viewer(await connect())
            for n in NAMES:
                await first.subscribe(n)
            await _until(lambda: all(len(v) > 10 for v in first.got.values()))
            first.subs["video"].unsubscribe()
            video = relay._tracks[(NS_T, b"video")]
            await _until(lambda: video.paused)
            await _until(lambda: not tracks["video"].subscribed)
            assert tracks["audio"].subscribed
            assert {r["name"]: r["fabric"]
                    for r in fabric.report()["tracks"]}["video"] == "paused"
            second = _Viewer(await connect(), names=("video",))
            await second.subscribe("video")
            await _until(lambda: tracks["video"].subscribed)
            await _until(lambda: len(second.got["video"]) > 10)
            await feeder.stop()
            await asyncio.sleep(0.2)
            _complete(second.got["video"])
            _complete(first.got["audio"])
            _no_dups(first.got["video"])


# -- fabric withdraw -> PUBLISH_DONE ------------------------------------

@pytest.mark.asyncio
async def test_a_fabric_withdraw_ends_the_track_downstream(monkeypatch):
    monkeypatch.setattr(relay, "FABRIC_WITHDRAW_GRACE_S", 0.2)
    port, fport = _PORT + 42, _FABRIC_PORT + 42
    async with _relay(port, fabric_port=fport) as (fabric, connect):
        pub = _pub_node(fport)
        async with pub:
            await _until(lambda: pub.connected() and fabric.node.connected())
            tracks = {n: pub.publish(NS, n) for n in NAMES}
            await _until(lambda: all(fabric.covers(NS_T, n.encode())
                                     for n in NAMES))
            feeder = _Feeder(tracks)
            viewer = _Viewer(await connect())
            for n in NAMES:
                await viewer.subscribe(n)
            await _until(lambda: all(len(v) > 10 for v in viewer.got.values()))
            pub.unpublish(tracks["video"])
            await _until(lambda: viewer.done["video"] is not None)
            assert (NS_T, b"video") not in relay._tracks
            assert viewer.done["audio"] is None
            await feeder.stop()
            for n in NAMES:
                _no_dups(viewer.got[n])
            _complete(viewer.got["audio"])


# -- peer drop and redial -----------------------------------------------

@pytest.mark.asyncio
async def test_a_dropped_peer_is_redialed_and_the_feed_recovers():
    port, pport = _PORT + 44, _FABRIC_PORT + 44
    pub = FabricNode(label="pub", bind="127.0.0.1", port=pport, cert=CERT,
                     key=KEY)
    async with pub:
        async with _relay(port, peers=[("127.0.0.1", pport)]) as (
                fabric, connect):
            await _until(lambda: pub.connected() and fabric.node.connected())
            tracks = {n: pub.publish(NS, n) for n in NAMES}
            await _until(lambda: all(fabric.covers(NS_T, n.encode())
                                     for n in NAMES))
            feeder = _Feeder(tracks)
            viewer = _Viewer(await connect())
            for n in NAMES:
                await viewer.subscribe(n)
            await _until(lambda: all(len(v) > 10 for v in viewer.got.values()))
            before = {n: len(v) for n, v in viewer.got.items()}
            first = fabric.node.connected()[0]
            for proto in list(pub._protocols.values()):
                proto.close()
            await _until(lambda: first.state == "closed")
            await _until(lambda: any(s is not first
                                     for s in fabric.node.connected()))
            await _until(lambda: all(len(viewer.got[n]) > before[n] + 10
                                     for n in NAMES))
            await feeder.stop()
            for n in NAMES:
                _no_dups(viewer.got[n])
                assert viewer.done[n] is None


# -- a stuck fabric hop does not stall local viewers --------------------

@pytest.mark.asyncio
async def test_a_stuck_fabric_hop_does_not_stall_local_viewers():
    watch = asyncio.ensure_future(relay._watch_lanes())
    port, fport = _PORT + 46, _FABRIC_PORT + 46
    try:
        async with _relay(port, fabric_port=fport) as (fabric, connect):
            pub = await connect()
            t = PublishedTrack(pub, namespace=NS, trackname="video",
                               object_size=300, group_size=5, rate=200)
            await t.publish(announce_namespace=False, publish_track=True)
            await until_published(NS, ["video"])
            key = (NS_T, b"video")
            rx = _pub_node(fport)
            async with rx:
                sink = _Sink()
                # The hop's lane sheds at the subscribe's delivery timeout.
                rx.pull(NS, "video", sink, delivery_timeout_ms=300)
                await _until(lambda: len(sink.got) > 10)
                e = fabric.egress[key]
                hop = next(iter(e.hops.values()))
                never = asyncio.Event()

                async def stuck(*_a, **_kw):
                    await never.wait()
                hop.stream_write_nowait = lambda *_a, **_kw: False
                hop.stream_write_drain = stuck
                hop.open_uni_stream = stuck
                viewer = await connect()
                got = []
                sub = SubscribedTrack(viewer, NS, "video",
                                      on_object=lambda m, *a: got.append(
                                          (a[2], m.object_id)))
                sub._quiet = True
                await sub.subscribe(timeout=5.0)
                await _until(lambda: len(got) > 10)
                n0 = len(got)
                await asyncio.sleep(1.5)
                assert len(got) - n0 > 150, "local viewer stalled"
                _no_dups(got)
                _no_dups(sink.got)
                d = e.track.find(hop, None)
                assert d is not None and d.lane is not None
                assert d.lane.shed > 0
    finally:
        watch.cancel()


# -- control socket -----------------------------------------------------

async def _http(path, method, target):
    r, w = await asyncio.open_unix_connection(path)
    w.write(f"{method} {target} HTTP/1.1\r\nHost: relay\r\n\r\n".encode())
    await w.drain()
    data = await r.read()
    w.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


@pytest.mark.asyncio
async def test_control_get_fabric_and_post_peer(tmp_path):
    sock = str(tmp_path / "relay.sock")
    server = await asyncio.start_unix_server(relay._on_control, path=sock)
    port, fport, bport = _PORT + 48, _FABRIC_PORT + 48, _FABRIC_PORT + 49
    try:
        status, body = await _http(sock, "GET", "/fabric")
        assert status == 404
        b = FabricNode(label="b", bind="127.0.0.1", port=bport, cert=CERT,
                       key=KEY)
        async with _relay(port, fabric_port=fport) as (fabric, connect):
            pub = _pub_node(fport)
            async with pub, b:
                await _until(lambda: pub.connected()
                             and fabric.node.connected())
                tracks = {"video": pub.publish(NS, "video")}
                await _until(lambda: fabric.covers(NS_T, b"video"))
                feeder = _Feeder(tracks)
                viewer = _Viewer(await connect(), names=("video",))
                await viewer.subscribe("video")
                await _until(lambda: len(viewer.got["video"]) > 10)

                status, rep = await _http(sock, "GET", "/fabric")
                assert status == 200
                assert rep["node_id"] == relay.node_id_format(
                    fabric.node.node_id)
                assert rep["label"] == relay.FABRIC_LABEL
                assert rep["metric"] == relay.DEFAULT_NODE_METRIC
                assert rep["listen"] == f"127.0.0.1:{fport}"
                (row,) = rep["peers"]
                assert row["origin"] == "accepted"
                assert row["status"] == "connected" and row["identified"]
                assert row["contact"] == "pub"
                assert row["remote_node_id"] == relay.node_id_format(
                    pub.node_id)
                assert row["rx_streams"] > 0 and row["rx_bytes"] > 0
                assert row["subscribe_tracks"] == 0
                assert {"srtt_us", "metric", "node_metric",
                        "connect_time_us", "publishes", "active_streams",
                        "tx_streams", "tx_bytes", "tx_datagrams",
                        "rx_datagrams", "host", "port"} <= set(row)
                assert [n["contact"] for n in rep["nodes"]] == ["pub"]
                assert rep["announces"] == [{
                    "namespace": NS, "name": "video",
                    "source": relay.node_id_format(pub.node_id),
                    "kind": "track"}]
                (sub,) = rep["subscribes"]
                assert sub["source"] == rep["node_id"]
                assert sub["fullname_hash"].startswith("0x")
                (tr,) = rep["transit"]
                assert tr["local"] and tr["egress"] == []
                assert tr["fullname_hash"] == sub["fullname_hash"]
                assert tr["streams"] > 0 and tr["bytes"] > 0
                assert rep["tracks"] == [{
                    "namespace": NS, "name": "video", "local_viewers": 1,
                    "fabric": "subscribed"}]

                q = f"host=127.0.0.1&port={bport}"
                assert (await _http(sock, "POST", "/fabric/peer?op=add&"
                                    + q + "&metric=7"))[0] == 200
                await _until(lambda: len(b.connected()) == 1)

                def dialed():
                    return [r for r in fabric.report()["peers"]
                            if r["origin"] == "dialed"]
                await _until(lambda: dialed()
                             and dialed()[0]["status"] == "connected")
                assert dialed()[0]["metric"] == 7
                assert (await _http(sock, "POST", "/fabric/peer?op=metric&"
                                    + q + "&metric=9"))[0] == 200
                assert dialed()[0]["metric"] == 9
                assert (await _http(sock, "POST", "/fabric/peer?op=remove&"
                                    + q))[0] == 200
                await _until(lambda: not b.connected() and all(
                    r["status"] == "closed" for r in dialed()))
                assert (await _http(sock, "POST", "/fabric/peer?op=remove&"
                                    + q))[0] == 404
                assert (await _http(sock, "POST", "/fabric/peer?op=metric&"
                                    + q + "&metric=1"))[0] == 404
                for bad in ("op=restart&" + q, "op=add&host=x",
                            "op=metric&" + q, "op=add&host=x&port=0"):
                    assert (await _http(sock, "POST",
                                        "/fabric/peer?" + bad))[0] == 400
                await feeder.stop()
                _complete(viewer.got["video"])
    finally:
        server.close()


# -- transit beside the relay's own viewers -----------------------------

@contextlib.asynccontextmanager
async def _via(case):
    """pub -- relay -- far, the relay forwarding; yields (fabric,
    connect, pub, far, the publisher's video track, its feeder)."""
    port, fport = _PORT + case, _FABRIC_PORT + case
    async with _relay(port, fabric_port=fport) as (fabric, connect):
        pub = _pub_node(fport)
        far = FabricNode(label="far", peers=[("127.0.0.1", fport)],
                         redial_interval=0.2)
        async with pub, far:
            await _until(lambda: pub.connected() and far.connected()
                         and len(fabric.node.connected()) == 2)
            video = pub.publish(NS, "video")
            await _until(lambda: fabric.covers(NS_T, b"video") and any(
                a.name == b"video" for a in far.remote_announces()))
            feeder = _Feeder({"video": video})
            try:
                yield fabric, connect, pub, far, video, feeder
            finally:
                await feeder.stop()


def _transit_row(fabric):
    rows = fabric.report()["transit"]
    return rows[0] if len(rows) == 1 else None


@pytest.mark.asyncio
async def test_a_local_viewer_comes_and_goes_while_the_relay_transits(
        monkeypatch):
    monkeypatch.setattr(relay, "_pause_after", 0.3)
    async with _via(50) as (fabric, connect, pub, far, video, feeder):
        far_sink = _Sink()
        far.pull(NS, "video", far_sink)
        await _until(lambda: len(far_sink.got) > 20)
        row = _transit_row(fabric)
        assert row is not None and not row["local"]
        (eg,) = row["egress"]
        assert eg["nodes"] == [relay.node_id_format(far.node_id)]
        viewer = _Viewer(await connect(), names=("video",))
        sub = await viewer.subscribe("video")
        await _until(lambda: len(viewer.got["video"]) > 20)
        assert _transit_row(fabric)["local"]
        sub.unsubscribe()
        await _until(lambda: not fabric.node.subscribed(NS_T, b"video"))
        await _until(lambda: not _transit_row(fabric)["local"])
        n = len(far_sink.got)
        await _until(lambda: len(far_sink.got) > n + 20)
        _complete(viewer.got["video"])
        _complete(far_sink.got)


@pytest.mark.asyncio
async def test_a_far_subscriber_leaves_while_the_relay_keeps_its_feed():
    async with _via(52) as (fabric, connect, pub, far, video, feeder):
        viewer = _Viewer(await connect(), names=("video",))
        await viewer.subscribe("video")
        await _until(lambda: len(viewer.got["video"]) > 20)
        far_sink = _Sink()
        far.pull(NS, "video", far_sink)
        await _until(lambda: len(far_sink.got) > 20)
        row = _transit_row(fabric)
        assert row["local"] and len(row["egress"]) == 1
        far.unpull(NS, "video")
        await _until(lambda: not _transit_row(fabric)["egress"])
        n = len(viewer.got["video"])
        await _until(lambda: len(viewer.got["video"]) > n + 20)
        _complete(viewer.got["video"])
        _complete(far_sink.got)
        assert viewer.done["video"] is None
