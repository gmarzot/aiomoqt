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
from aiomoqt.types import MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 17500
_NS = "status/live"
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


def _write_platform_proc(root, step):
    """A /proc tree for the platform detail; `step` 0 then 1 is 2 s apart."""
    k = step
    (root / "net").mkdir(parents=True, exist_ok=True)
    (root / "self/fd").mkdir(parents=True, exist_ok=True)
    (root / "pressure").mkdir(exist_ok=True)
    for i in range(3):
        (root / f"self/fd/{i}").write_text("")
    user, system, idle, iowait, irq, softirq = (
        100 + 200 * k, 50 + 100 * k, 800 + 600 * k, 20 + 50 * k,
        5 + 25 * k, 5 + 25 * k)
    (root / "stat").write_text(
        f"cpu {user} 10 {system} {idle} {iowait} {irq} {softirq} 0 0 0\n"
        f"cpu0 {user} 10 {system} {idle} {iowait} {irq} {softirq} 0 0 0\n"
        f"intr {500 + 1000 * k} 1 2 3\nctxt {1000 + 2000 * k}\n"
        f"processes {10 + 10 * k}\nprocs_running 3\nprocs_blocked 1\n")
    (root / "pressure/cpu").write_text(
        "some avg10=1.50 avg60=0.00 avg300=0.00 total=10\n"
        "full avg10=0.25 avg60=0.00 avg300=0.00 total=1\n")
    (root / "pressure/io").write_text(
        "some avg10=0.10 avg60=0.00 avg300=0.00 total=1\n")
    (root / "diskstats").write_text(
        f"   8       0 sda {100 + 200 * k} 0 {2000 + 20480 * k} 0 "
        f"{50 + 100 * k} 0 {4000 + 4096 * k} 0 0 {100 + 1000 * k} 0\n"
        "   8       1 sda1 9 0 9 0 9 0 9 0 0 9 0\n"
        "   7       0 loop0 9 0 9 0 9 0 9 0 0 9 0\n")
    (root / "net/dev").write_text(
        "Inter-|   Receive\n face |bytes\n"
        f"    lo: 9 9 0 0 0 0 0 0 9 9 0 0 0 0 0 0\n"
        f"  eth0: {1000 + 250000 * k} {10 + 200 * k} {2 * k} 0 0 0 0 0 "
        f"{2000 + 125000 * k} {20 + 100 * k} 0 {4 * k} 0 0 0 0\n"
        f"docker0: 9 9 0 0 0 0 0 0 9 9 0 0 0 0 0 0\n")
    (root / "net/snmp").write_text(
        "Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors\n"
        f"Udp: {100 + 200 * k} 0 {10 + 20 * k} {80 + 200 * k} {4 + 6 * k} 0\n")
    (root / "net/snmp6").write_text(
        f"Udp6InDatagrams {50 + 100 * k}\nUdp6OutDatagrams 0\n")
    (root / "net/udp").write_text(
        "   sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode ref pointer drops\n"
        "    1: 00000000:115B 00000000:0000 07 00000010:00000200 00:00000000 "
        f"00000000     0        0 12345 2 0000000000000000 {7 + 20 * k}\n"
        "    2: 00000000:0035 00000000:0000 07 00000000:00000000 00:00000000 "
        "00000000     0        0 12346 2 0000000000000000 99\n")
    (root / "meminfo").write_text(
        "MemTotal: 1000 kB\nMemFree: 200 kB\nMemAvailable: 500 kB\n"
        "Buffers: 50 kB\nCached: 150 kB\nSReclaimable: 50 kB\n"
        "SwapTotal: 100 kB\nSwapFree: 40 kB\n")
    fields = ["0"] * 40
    fields[0] = "S"
    fields[11], fields[12] = str(100 + 200 * k), str(50 + 100 * k)
    fields[17], fields[21] = "4", "100"
    (root / "self/stat").write_text("77 (relay) " + " ".join(fields))
    (root / "self/status").write_text(
        f"voluntary_ctxt_switches:\t{100 + 200 * k}\n"
        f"nonvoluntary_ctxt_switches:\t{10 + 20 * k}\n")


def test_host_sampler_platform_detail(tmp_path):
    proc = tmp_path / "proc"
    _write_platform_proc(proc, 0)
    h = relay_status.HostSampler(proc=str(proc), os_release=[], udp_port=4443)
    first = h.sample(10.0)
    assert first["udp_socket"] == {"port": 4443, "tx_queue": 16,
                                   "rx_queue": 512, "drops": 7,
                                   "drops_per_s": None}
    assert first["psi"] == {"cpu": {"some": 1.5, "full": 0.25},
                            "io": {"some": 0.1}, "memory": {}}
    assert first["mem"]["used"] == 550 * 1024
    assert first["mem"]["cached"] == 200 * 1024
    assert first["mem"]["swap_used"] == 60 * 1024
    assert (first["procs_running"], first["procs_blocked"]) == (3, 1)
    assert first["proc"]["fds"] == 3

    _write_platform_proc(proc, 1)
    s = h.sample(12.0)
    assert s["cpu_modes"] == {"user": 20.0, "nice": 0.0, "system": 10.0,
                              "idle": 60.0, "iowait": 5.0, "irq": 2.5,
                              "softirq": 2.5, "steal": 0.0}
    assert (s["ctxt_per_s"], s["intr_per_s"], s["forks_per_s"]) == (
        1000.0, 500.0, 5.0)
    # Whole disks only: the partition and the loop device are left out.
    assert s["disks"] == [{"name": "sda", "read_iops": 100.0,
                           "read_Bps": 5242880.0, "write_iops": 50.0,
                           "write_Bps": 1048576.0, "busy_pct": 50.0}]
    assert [i["name"] for i in s["ifaces"]] == ["eth0"]
    eth0 = s["ifaces"][0]
    assert (eth0["rx_bps"], eth0["tx_bps"]) == (1_000_000, 500_000)
    assert (eth0["rx_pps"], eth0["tx_pps"]) == (100, 50)
    assert (eth0["errs_per_s"], eth0["drops_per_s"]) == (1, 2)
    # IPv4 and IPv6 UDP together.
    assert s["udp"] == {"InDatagrams": 150.0, "OutDatagrams": 100.0,
                        "InErrors": 10.0, "RcvbufErrors": 3.0,
                        "SndbufErrors": 0.0, "NoPorts": 0.0}
    assert s["udp_socket"]["drops_per_s"] == 10.0
    hz = os.sysconf("SC_CLK_TCK")
    assert s["proc"]["user_pct"] == round(100 * 200 / hz / 2, 1)
    assert s["proc"]["system_pct"] == round(100 * 100 / hz / 2, 1)
    assert (s["proc"]["ctx_vol_per_s"], s["proc"]["ctx_invol_per_s"]) == (
        100.0, 10.0)


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


@pytest.mark.parametrize("addr,shown", [
    (("203.0.113.7", 51234), "203.0.113.7"),
    ("198.51.100.255", "198.51.100.255"),
    (("::ffff:203.0.113.7", 443), "203.0.113.7"),
    (("2001:db8:abcd:12::1", 4433), "2001:db8:abcd:12::1"),
    ("2001:0db8:0000:0001::", "2001:db8:0:1::"),
    ("fe80::1%eth0", "fe80::1"),
    (None, None), ("", None), ("not-an-ip", None),
])
def test_display_addr(addr, shown):
    assert relay_status.display_addr(addr) == shown


def test_peer_address_is_none_without_transport_support():
    from types import SimpleNamespace
    from aiomoqt.protocol import _MOQTSessionMixin
    prop = _MOQTSessionMixin.peer_address.fget
    old = SimpleNamespace(_is_wt=False, _quic=object())
    assert prop(old) is None
    wt = SimpleNamespace(_is_wt=True, _transport=object(), cnx_ptr=1)
    assert prop(wt) is None
    quic = SimpleNamespace(_is_wt=False, _quic=SimpleNamespace(
        peer_address=lambda: ("192.0.2.1", 4433)))
    assert prop(quic) == ("192.0.2.1", 4433)


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
            connect.handle = handle
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
            if (relay._catalogs.get(ns) and video is not None
                    and video._bytes_out):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("no catalog classified or video relayed")

        status = relay._Status(str(tmp_path / "status.json"),
                               {"started": time.time()}, [connect.handle])
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
    # sub1 on two tracks counts once as a subscriber, twice as subscriptions.
    assert (space["tracks"], space["subscribers"], space["subscriptions"]) == (
        2, 2, 3)
    assert space["announced"]
    assert space["peak"] == 2
    assert totals["subscribers_peak"] >= totals["subscribers"] == 2
    assert totals["subscriptions"] == 3
    assert snap["history"]["subscribers"][-1] == 2
    assert snap["history"]["subscriptions"][-1] == 3
    assert totals["groups_per_s"] > 0
    assert snap["history"]["groups_per_s"][-1] == totals["groups_per_s"]

    tracks = {r["name"]: r for r in snap["tracks"]}
    assert set(tracks) == {"catalog", "video"}
    assert tracks["catalog"]["subscribers"] == 1
    assert (tracks["video"]["subscribers"], tracks["video"]["peak"]) == (2, 2)
    assert tracks["video"]["bytes_out"] > tracks["video"]["bytes_in"] > 0
    assert tracks["video"]["state"] == "live"
    assert tracks["video"]["out_bps"] > 0
    video = tracks["video"]
    assert video["groups_in"] > 0
    assert video["groups_per_s"] > 0 and video["objects_per_s"] > 0
    assert None not in (video["rtt_up_ms"], video["rtt_down_ms"], video["latency_ms"])
    assert totals["objects_in_per_s"] > 0

    # Each video subscriber is listed with its session.
    assert [(s["transport"], s["draft"], s["forward"])
            for s in tracks["video"]["subscriber_list"]] == [
        ("quic" if use_quic else "wt", 18, True)] * 2
    ids = {s["role"]: [] for s in snap["sessions"]}
    for s in snap["sessions"]:
        ids[s["role"]].append(s["id"])
    assert len({s["id"] for s in snap["sessions"]}) == 3
    listed = {s["session"] for t in tracks.values()
              for s in t["subscriber_list"]}
    assert listed == set(ids["sub"])
    assert all(s["rtt_ms"] is not None for s in tracks["video"]["subscriber_list"])
    history = snap["track_history"][tracks["video"]["id"]]
    assert history["subscribers"][-1] == 2 and history["out_bps"][-1] > 0
    assert len(history["subscribers"]) == len(snap["history"]["t"])
    assert set(snap["host"]) >= {"cpu", "mem_used", "disk_used", "os"}
    platform = status.platform
    assert platform["history"]["t"] == snap["history"]["t"]
    assert platform["bandwidth"]["egress_bps"] == totals["egress_bps"]
    assert "cpu_modes" in platform["host"] and "ifaces" in platform["host"]
    (transport,) = platform["transport"]
    assert set(transport) == set(relay.TRANSPORT_COUNTERS)
    assert transport["loop_callbacks"] > 0

    # Peer addresses, never a port.
    local = {"127.0.0.1", "::1"}
    assert {s["addr"] for s in snap["sessions"]} <= local
    assert None not in {s["addr"] for s in snap["sessions"]}
    assert {video["origin_addr"], space["origin_addr"]} <= local
    assert {s["addr"] for s in video["subscriber_list"]} <= local

    relay_status.write_json_atomic(status.path, snap)
    with open(status.path) as f:
        assert json.load(f)["totals"]["sessions"] == 3


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@_TRANSPORTS
async def test_status_loop_sends_publishers_no_requests(
        draft, use_quic, relay_tables, monkeypatch, tmp_path):
    # A namespace the dashboard lists stays unclassified until a catalog
    # object is relayed; the relay never asks its publisher for one.
    monkeypatch.setattr(relay, "STATUS_INTERVAL_S", 0.1)
    port = _PORT + 2 + 2 * (draft == 22) + (not use_quic)
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        asked = []

        async def record(session, msg):
            asked.append(msg)
        for t in (MOQTMessageType.TRACK_STATUS, MOQTMessageType.FETCH,
                  MOQTMessageType.SUBSCRIBE):
            pub.register_handler(t, record)
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        ns = relay._ns_tuple(_NS)

        background = set(relay._background)
        status = relay._Status(str(tmp_path / "status.json"),
                               {"started": time.time()}, [connect.handle])
        runner = asyncio.create_task(status.run())
        try:
            await asyncio.sleep(1.0)
        finally:
            runner.cancel()
            for task in set(relay._background) - background:
                task.cancel()
                relay._background.discard(task)
        assert asked == []
        with open(status.path) as f:
            (space,) = json.load(f)["namespaces"]
        assert space["namespace"] == _NS and space["format"] is None
        assert ns not in relay._catalogs
        assert pub._close_err is None
