"""Ersatz-relay dashboard status: host metrics from /proc, MSF catalog
classification, and the relay's status snapshot end to end."""
import asyncio
import contextlib
import json
import os
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media.broadcast import CatalogTrackPublisher
from aiomoqt.media.catalog import Catalog, CatalogTrack
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import relay_status
from aiomoqt.track import PublishedTrack, SubscribedTrack

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 17500
_NS = "status/live"
_PLAIN_NS = "status/plain"
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


# -- host metrics --------------------------------------------------------

_ARM_CPUINFO = """processor\t: 0
BogoMIPS\t: 50.00
CPU implementer\t: 0x41
CPU architecture: 8
CPU part\t: 0xd0c

processor\t: 1
CPU implementer\t: 0x41
CPU part\t: 0xd0c
"""

_NET_DEV = """Inter-|   Receive                            |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes
    lo: {lo} 1 0 0 0 0 0 0 {lo} 1 0 0 0 0 0 0
  eth0: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0
docker0: {lo} 1 0 0 0 0 0 0 {lo} 1 0 0 0 0 0 0
veth12ab: {lo} 1 0 0 0 0 0 0 {lo} 1 0 0 0 0 0 0
"""


def _write_proc(root, *, cpu, cores, rx, tx, lo, udp, ticks):
    """A /proc tree: `cpu` and each of `cores` are (busy, idle) jiffies,
    `udp` is (InErrors, RcvbufErrors), `ticks` is (utime, stime)."""
    (root / "net").mkdir(parents=True, exist_ok=True)
    (root / "self").mkdir(exist_ok=True)
    stat = [f"cpu {cpu[0]} 0 0 {cpu[1]} 0 0 0 0 0 0"]
    stat += [f"cpu{i} {b} 0 0 {i_} 0 0 0 0 0 0"
             for i, (b, i_) in enumerate(cores)]
    (root / "stat").write_text("\n".join(stat) + "\nintr 1 2 3\n")
    (root / "net/dev").write_text(_NET_DEV.format(rx=rx, tx=tx, lo=lo))
    (root / "net/snmp").write_text(
        "Ip: Forwarding DefaultTTL\nIp: 1 64\n"
        "Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors\n"
        f"Udp: 5 0 {udp[0]} 5 {udp[1]}\n")
    (root / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
    (root / "loadavg").write_text("0.50 0.25 0.10 1/100 1234\n")
    (root / "uptime").write_text("12345.6 1000.0\n")
    (root / "cpuinfo").write_text(_ARM_CPUINFO)
    fields = ["0"] * 40
    fields[0] = "S"
    fields[11], fields[12] = str(ticks[0]), str(ticks[1])
    fields[17], fields[21] = "7", "256"
    (root / "self/stat").write_text("4321 (py thon) " + " ".join(fields))


def test_host_sampler_rates_between_samples(tmp_path):
    proc = tmp_path / "proc"
    release = tmp_path / "os-release"
    release.write_text('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 22.04.5 LTS"\n')
    _write_proc(proc, cpu=(100, 900), cores=[(50, 450), (50, 450)],
                rx=1000, tx=2000, lo=10, udp=(10, 4), ticks=(100, 50))
    h = relay_status.HostSampler(
        proc=str(proc), os_release=[str(tmp_path / "missing"), str(release)])
    assert h.static["cpu_model"] == "Neoverse-N1"
    assert h.static["cores"] == 2
    assert h.static["os"] == "Ubuntu 22.04.5 LTS"

    first = h.sample(10.0)
    assert first["cpu"] is None and first["rx_bps"] is None
    assert first["mem_used"] == 750 * 1024
    assert first["load"] == [0.5, 0.25, 0.1]
    assert first["proc_threads"] == 7
    assert first["proc_rss"] == 256 * os.sysconf("SC_PAGE_SIZE")

    # Two seconds on: loopback, docker and veth traffic must not count.
    _write_proc(proc, cpu=(300, 1100), cores=[(200, 500), (100, 600)],
                rx=251_000, tx=127_000, lo=10**9, udp=(30, 10),
                ticks=(250, 100))
    s = h.sample(12.0)
    assert s["cpu"] == 50.0
    assert s["cpu_cores"] == [75.0, 25.0]
    assert s["rx_bps"] == 1_000_000
    assert s["tx_bps"] == 500_000
    assert (s["udp_in_errors"], s["udp_rcvbuf_errors"]) == (10, 3)
    hz = os.sysconf("SC_CLK_TCK")
    assert s["proc_cpu"] == round(100 * 200 / hz / 2, 1)
    assert s["disk_total"] >= s["disk_used"] > 0


@pytest.mark.parametrize("cpuinfo,model", [
    ("processor\t: 0\nmodel name\t: AMD EPYC 7J13\n", "AMD EPYC 7J13"),
    ("CPU implementer\t: 0x41\nCPU part\t: 0xfff\n", "ARM 0x41/0xfff"),
])
def test_cpu_model(cpuinfo, model):
    assert relay_status.parse_cpu_model(cpuinfo) == model


@pytest.mark.parametrize("packagings,label", [
    (["loc", "loc"], "MSF"),
    (["cmaf", "cmaf"], "CMSF"),
    (["locmaf"], "LOCMAF"),
    (["cmaf", "loc"], "CMSF+MSF"),
    (["mediatimeline"], None),
])
def test_classify_catalog(packagings, label):
    doc = {"version": 1,
           "tracks": [{"name": f"t{i}", "packaging": p}
                      for i, p in enumerate(packagings)]}
    assert relay_status.classify_catalog(json.dumps(doc).encode()) == label


@pytest.mark.parametrize("payload", [
    b"\x00\x01binary", b"[1, 2]", b'{"version": 1}',
    b'{"deltaUpdate": [{"op": "add"}], "tracks": [{"packaging": "loc"}]}',
])
def test_classify_catalog_rejects_non_catalogs(payload):
    assert relay_status.classify_catalog(payload) is None


def test_history_series_stay_aligned():
    h = relay_status.History(("t", "a"), 3)
    for i in range(4):
        h.add({"t": i, "a": i * 10} if i != 2 else {"t": i})
    assert h.to_json() == {"t": [1, 2, 3], "a": [10, None, 30]}


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert relay_status.percentile(values, 50) == 50
    assert relay_status.percentile(values, 99) == 99
    assert relay_status.percentile(values, 100) == 100
    assert relay_status.percentile([], 50) is None


def test_write_json_atomic(tmp_path):
    path = tmp_path / "status.json"
    relay_status.write_json_atomic(str(path), {"a": [1, 2]})
    relay_status.write_json_atomic(str(path), {"b": None})
    assert json.loads(path.read_text()) == {"b": None}
    assert os.listdir(tmp_path) == ["status.json"]
    assert path.stat().st_mode & 0o777 == 0o644


def test_versions_names_the_stack():
    v = relay_status.versions()
    assert set(v) == {"aiomoqt", "aiopquic", "picoquic", "python"}


# -- relay status, end to end -------------------------------------------

def _catalog(packaging):
    return Catalog(tracks=[CatalogTrack(
        name="video", packaging=packaging, isLive=True,
        codec="avc1.42001f", width=640, height=360, framerate=30)])


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._catalogs):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture
def relay_tables():
    _clear_relay()
    yield
    _clear_relay()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft):
    """A relay; yields a coroutine that connects one more session."""
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            yield connect
    finally:
        handle.close()


@requires_certs
@_TRANSPORTS
async def test_snapshot_reports_namespaces_tracks_and_sessions(
        use_quic, relay_tables, tmp_path):
    # Two tracks on one publisher, two subscribers, and a session that
    # used only the watchdog's namespace, which the dashboard leaves out.
    port = _PORT + (not use_quic)
    async with _relay(port, use_quic, 18) as connect:
        pub = await connect()
        CatalogTrackPublisher(pub, _NS, _catalog("loc")).attach()
        PublishedTrack(pub, namespace=_NS, trackname="video",
                       object_size=200, group_size=10, rate=200).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        probe = await connect()
        await probe.publish_namespace(namespace="aiomoqt-watchdog/1-0",
                                      wait_response=True)
        sub1, sub2 = await connect(), await connect()
        await SubscribedTrack(sub1, _NS, "catalog").subscribe(timeout=5)
        await SubscribedTrack(sub1, _NS, "video").subscribe(timeout=5)
        await SubscribedTrack(sub2, _NS, "video").subscribe(timeout=5)

        ns = relay._ns_tuple(_NS)
        for _ in range(150):
            video = relay._tracks.get((ns, b"video"))
            if (relay._catalogs.get(ns, [None])[0] and video is not None
                    and video._bytes_out):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("no catalog classified or video relayed")

        status = relay._Status(str(tmp_path / "status.json"),
                               {"started": time.time()})
        loop = asyncio.get_running_loop()
        status.snapshot(loop.time(), time.time())
        await asyncio.sleep(0.3)
        snap = status.snapshot(loop.time(), time.time())

    totals = snap["totals"]
    assert totals["sessions"] == 3
    assert totals["by_draft"] == {"d18": 3}
    assert totals["by_transport"] == {("quic" if use_quic else "wt"): 3}
    assert totals["publishers"] == 1
    assert totals["ingress_bps"] > 0 and totals["egress_bps"] > 0
    assert totals["rtt_p50_ms"] is not None
    assert sorted(s["role"] for s in snap["sessions"]) == [
        "pub", "sub", "sub"]

    assert [r["namespace"] for r in snap["namespaces"]] == [_NS]
    space = snap["namespaces"][0]
    assert space["format"] == "MSF"
    assert (space["tracks"], space["subscribers"]) == (2, 3)
    assert space["announced"]

    tracks = {r["name"]: r for r in snap["tracks"]}
    assert set(tracks) == {"catalog", "video"}
    assert tracks["catalog"]["subscribers"] == 1
    assert (tracks["video"]["subscribers"], tracks["video"]["peak"]) == (2, 2)
    assert tracks["video"]["bytes_out"] > tracks["video"]["bytes_in"] > 0
    assert tracks["video"]["state"] == "live"
    assert tracks["video"]["out_bps"] > 0

    relay_status.write_json_atomic(status.path, snap)
    with open(status.path) as f:
        assert json.load(f)["totals"]["sessions"] == 3


@requires_certs
@pytest.mark.parametrize("draft", [16, 18])
@_TRANSPORTS
async def test_probe_classifies_a_catalog_nobody_subscribed_to(
        draft, use_quic, relay_tables, monkeypatch):
    # The probe asks the publisher directly (TRACK_STATUS, then a FETCH of
    # that group) and leaves no relayed track behind. A namespace with no
    # catalog stays unclassified and is not probed again until the retry.
    monkeypatch.setattr(relay, "CATALOG_PROBE_S", 2.0)
    port = _PORT + 2 + 2 * (draft == 18) + (not use_quic)
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        CatalogTrackPublisher(pub, _NS, _catalog("cmaf")).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        await pub.publish_namespace(namespace=_PLAIN_NS, wait_response=True)
        ns, plain = relay._ns_tuple(_NS), relay._ns_tuple(_PLAIN_NS)
        for _ in range(100):
            if ns in relay._announced and plain in relay._announced:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("namespaces not announced")

        loop = asyncio.get_running_loop()
        relay._probe_catalogs(loop.time())
        tasks = [relay._catalogs[n][2] for n in (ns, plain)]
        await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert relay._catalogs[ns][0] == "CMSF"
        assert relay._catalogs[plain][0] is None
        assert (ns, relay.CATALOG_TRACK) not in relay._tracks

        relay._probe_catalogs(loop.time())
        assert relay._catalogs[plain][2] is tasks[1]
        assert pub._close_err is None
