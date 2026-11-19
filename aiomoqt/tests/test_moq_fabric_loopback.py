"""Two in-process FabricNodes over localhost raw QUIC (moq-fabric has no
WebTransport mapping): CONNECT, node learning, announces and subscribes
both ways on two tracks each, withdraws, re-dial after a close, and the
moq-fabric-probe tool end to end."""
import asyncio
import json
import os
import signal

import pytest

from aiomoqt.moq_fabric import (FabricNode, build_report, track_hashes,
                             write_report)
from aiomoqt.moq_fabric.wire import MsgType
from aiomoqt.tools import moq_fabric_probe

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 33460
TIMEOUT = 10


def _listener(port, label="A", **kw):
    return FabricNode(label=label, bind="127.0.0.1", port=port, cert=CERT,
                      key=KEY, **kw)


def _dialer(port, label="B", **kw):
    return FabricNode(label=label, peers=[("127.0.0.1", port)],
                      redial_interval=0.2, **kw)


def _tracks(node):
    return {(rec.body.namespace, rec.body.name): rec
            for _, rec in node.learned_announces()}


def _subs(node):
    return {(rec.body.namespace, rec.body.name): rec
            for _, rec in node.learned_subscribes()}


async def _up(a, b):
    await b.wait_until(lambda: a.connected() and b.connected(), TIMEOUT)


@pytest.mark.asyncio
async def test_connect_and_node_learning():
    port = _BASE_PORT
    async with _listener(port) as a, _dialer(port) as b:
        await _up(a, b)
        sa, sb = a.connected()[0], b.connected()[0]
        assert (sa.dialer, sb.dialer) == (False, True)
        assert sb.remote.node_id == a.node_id and sb.remote.contact == "A"
        assert sa.remote.node_id == b.node_id and sa.remote.contact == "B"
        assert sa.remote.metric == 1 << 32 and sa.remote.path == ()
        assert sb.tx_msgs[MsgType.CONNECT.name] == 1
        assert sb.rx_msgs[MsgType.CONNECT_RESPONSE.name] == 1
        assert sa.rx_msgs[MsgType.CONNECT.name] == 1
        assert sb.connect_ms > 0 and sa.connect_ms is not None
        assert [rec.body.node_id for _, rec in b.learned_nodes()] == [a.node_id]
        assert [rec.body.node_id for _, rec in a.learned_nodes()] == [b.node_id]


@pytest.mark.asyncio
async def test_announce_and_subscribe_both_ways_two_tracks():
    port = _BASE_PORT + 1
    a, b = _listener(port), _dialer(port)
    # Originated before the session exists: sent in the connect sync.
    b.announce("b/live", "video")
    b.subscribe("a/live", "video")
    async with a, b:
        await _up(a, b)
        a.announce("a/live", "video")
        a.announce("a/live", "audio")
        a.announce("a/ns")
        b.announce("b/live", "audio")
        b.subscribe("a/live", "audio")
        a.subscribe("b/live", "video")
        a.subscribe("b/live", "audio")
        await b.wait_until(lambda: len(_tracks(b)) == 3, TIMEOUT)
        await a.wait_until(lambda: len(_tracks(a)) == 2
                           and len(_subs(a)) == 2, TIMEOUT)
        await b.wait_until(lambda: len(_subs(b)) == 2, TIMEOUT)

        tb, ta = _tracks(b), _tracks(a)
        assert set(tb) == {((b"a", b"live"), b"video"),
                           ((b"a", b"live"), b"audio"), ((b"a", b"ns"), b"")}
        assert set(ta) == {((b"b", b"live"), b"video"),
                           ((b"b", b"live"), b"audio")}
        assert tb[((b"a", b"ns"), b"")].body.is_namespace
        assert all(rec.hash_ok for rec in [*tb.values(), *ta.values()])
        assert all(rec.body.source == a.node_id for rec in tb.values())
        sa, sb = _subs(a), _subs(b)
        assert set(sa) == {((b"a", b"live"), b"video"),
                           ((b"a", b"live"), b"audio")}
        assert set(sb) == {((b"b", b"live"), b"video"),
                           ((b"b", b"live"), b"audio")}
        assert all(rec.hash_ok for rec in [*sa.values(), *sb.values()])
        assert all(rec.body.seq == b._seq_base for rec in sa.values())
        assert all(rec.body.seq == a._seq_base for rec in sb.values())
        assert all(rec.body.source == b.node_id for rec in sa.values())

        # Withdraws remove one track's record on the far side, not both.
        a.withdraw_announce("a/live", "audio")
        b.unsubscribe("a/live", "video")
        await b.wait_until(lambda: len(_tracks(b)) == 2, TIMEOUT)
        await a.wait_until(lambda: len(_subs(a)) == 1, TIMEOUT)
        assert ((b"a", b"live"), b"audio") not in _tracks(b)
        assert set(_subs(a)) == {((b"a", b"live"), b"audio")}
        kinds_b = [e["kind"] for e in b.events]
        kinds_a = [e["kind"] for e in a.events]
        assert kinds_b.count("announce_withdrawn") == 1
        assert kinds_a.count("subscribe_withdrawn") == 1

        # An update goes out at the next seq.
        b.subscribe("a/live", "audio", forward=0)
        await a.wait_until(lambda: next(iter(_subs(a).values())).body.forward
                           == 0, TIMEOUT)
        assert next(iter(_subs(a).values())).body.seq == (
            b._seq_base + 1) & 0xFFFF
        assert sum(s.hash_mismatches for s in a.sessions.values()) == 0


@pytest.mark.asyncio
async def test_redial_after_close_resyncs():
    port = _BASE_PORT + 2
    b = _dialer(port)
    b.subscribe("a/live", "video")
    b.announce("b", "t0")
    a = _listener(port)
    await a.start()
    await b.start()
    try:
        await _up(a, b)
        a.announce("a/live", "video")
        await b.wait_until(lambda: len(_tracks(b)) == 1, TIMEOUT)
        first = b.connected()[0]
        await a.stop()
        await b.wait_until(lambda: not b.connected(), TIMEOUT)
        assert first.state == "closed" and _tracks(b) == {}
        assert _withdrawn_before_close(b, first.id) == (1, 0)

        a2 = _listener(port, label="A2")
        a2.announce("a/live", "video")
        await a2.start()
        try:
            await b.wait_until(lambda: len(_tracks(b)) == 1, TIMEOUT)
            await a2.wait_until(lambda: len(_subs(a2)) == 1
                                and len(_tracks(a2)) == 1, TIMEOUT)
            second = b.connected()[0]
            assert second.id != first.id and second.remote.contact == "A2"
            assert next(iter(_subs(a2).values())).body.seq == b._seq_base
            assert [e["kind"] for e in b.events].count("connected") == 2
        finally:
            await a2.stop()
    finally:
        await b.stop()
        await a.stop()


@pytest.mark.asyncio
async def test_report_json(tmp_path):
    port = _BASE_PORT + 3
    async with _listener(port) as a, _dialer(port) as b:
        await _up(a, b)
        a.announce("r/live", "video")
        b.subscribe("r/live", "video")
        await a.wait_until(lambda: len(_subs(a)) == 1, TIMEOUT)
        await b.wait_until(lambda: len(_tracks(b)) == 1, TIMEOUT)
        report = build_report(b)
        path = tmp_path / "r.json"
        write_report(str(path), report)
        loaded = json.loads(path.read_text())
    assert loaded["node"]["id_int"] == b.node_id
    assert loaded["node"]["contact"] == "B"
    assert loaded["clock"]["start_unix_ns"] > 0
    (sess,) = loaded["sessions"]
    assert sess["state"] == "connected" and sess["remote_node_int"] == a.node_id
    assert sess["contact"] == "A" and sess["origin"] == "dialed"
    assert sess["rx_msgs"]["ANNOUNCE_INFO_ADV"] == 1
    (ann,) = loaded["announces"]
    assert ann["ns"] == ["r", "live"] and ann["name"] == "video"
    want = track_hashes((b"r", b"live"), b"video").fullname
    assert ann["hash"] == f"0x{want:016x}"
    assert ann["hash_ok"] is True and ann["t_unix_ns"] > 0
    assert loaded["nodes"][0]["node_int"] == a.node_id
    assert loaded["originated"]["subscribes"][0]["name"] == "video"
    for ev in loaded["events"]:
        assert {"t_ms", "t_unix_ns", "kind"} <= set(ev)
    remote = [e for e in loaded["events"] if e["kind"] == "announce"]
    assert remote[0]["remote_contact"] == "A"
    assert remote[0]["remote_node"] == loaded["nodes"][0]["node"]


@pytest.mark.asyncio
async def test_moq_fabric_probe_tool(tmp_path, capsys):
    port = _BASE_PORT + 4
    out = tmp_path / "probe.json"
    listener = moq_fabric_probe.parse_args([
        "-p", str(port), "--bind", "127.0.0.1", "--cert", CERT, "--key", KEY,
        "--node-id", "9.9:1", "--announce", "t/a", "--announce", "t/b",
        "--exit", "idle:1", "-t", str(TIMEOUT), "-i", "0"])
    dialer = moq_fabric_probe.parse_args([
        "--peer", f"127.0.0.1:{port}", "-p", "0", "--subscribe", "t/a",
        "--exit", "announce:t/b", "-t", str(TIMEOUT), "-o", str(out),
        "-i", "0.2"])
    rc = await asyncio.gather(moq_fabric_probe.run(listener),
                              moq_fabric_probe.run(dialer))
    assert rc == [0, 0]
    report = json.loads(out.read_text())
    names = {(tuple(a["ns"]), a["name"]) for a in report["announces"]}
    assert names == {(("t",), "a"), (("t",), "b")}
    assert all(a["hash_ok"] and a["source"] == "9.9:0.1"
               for a in report["announces"])
    assert report["sessions"][0]["remote_node"] == "9.9:0.1"
    assert "exit: announce:t/b met" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_moq_fabric_probe_exit_not_met():
    port = _BASE_PORT + 5
    args = moq_fabric_probe.parse_args([
        "--peer", f"127.0.0.1:{port}", "-p", "0", "--exit", "peers-up",
        "-t", "1", "-i", "0", "--no-stats"])
    assert await moq_fabric_probe.run(args) == 1


def _withdrawn_before_close(node, session_id):
    kinds = [e["kind"] for e in node.events if e.get("session") == session_id]
    before = kinds[:kinds.index("closed")]
    return before.count("announce_withdrawn"), before.count(
        "subscribe_withdrawn")


@pytest.mark.asyncio
async def test_stop_withdraws_originated():
    port = _BASE_PORT + 6
    a, b = _listener(port), _dialer(port)
    await a.start()
    await b.start()
    try:
        await _up(a, b)
        for name in ("video", "audio"):
            a.announce("w/live", name)
            a.subscribe("w/peer", name)
        await b.wait_until(lambda: len(_tracks(b)) == 2
                           and len(_subs(b)) == 2, TIMEOUT)
        session = b.connected()[0].id
        await a.stop()
        await b.wait_until(lambda: not b.connected(), TIMEOUT)
        assert _withdrawn_before_close(b, session) == (2, 2)
        wd = [e for e in b.events if e["kind"] == "subscribe_withdrawn"]
        assert [e["seq"] for e in wd] == [(a._seq_base + 1) & 0xFFFF] * 2
        mine = [e["kind"] for e in a.events]
        assert mine.count("announce_withdrawn_originated") == 2
        assert mine.count("subscribe_withdrawn_originated") == 2
    finally:
        await b.stop()
        await a.stop()


@pytest.mark.asyncio
async def test_moq_fabric_probe_withdraws_on_signal(tmp_path):
    port = _BASE_PORT + 7
    out = tmp_path / "signal.json"
    async with _listener(port, label="L") as lst:
        args = moq_fabric_probe.parse_args([
            "--peer", f"127.0.0.1:{port}", "-p", "0", "--announce", "s/a",
            "--announce", "s/b", "--subscribe", "s/x", "--subscribe", "s/y",
            "-o", str(out), "-i", "0", "--no-stats"])
        probe = asyncio.ensure_future(moq_fabric_probe.run(args))
        await lst.wait_until(lambda: len(_tracks(lst)) == 2
                             and len(_subs(lst)) == 2, TIMEOUT)
        session = lst.connected()[0].id
        # Only with the probe's handler installed: SIGTERM would end pytest.
        assert signal.SIGTERM in asyncio.get_running_loop()._signal_handlers
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(probe, TIMEOUT) == 130
        await lst.wait_until(lambda: not lst.connected(), TIMEOUT)
        assert _withdrawn_before_close(lst, session) == (2, 2)
    report = json.loads(out.read_text())
    assert report["nodes"][0]["contact"] == "L"
    assert report["sessions"][0]["state"] == "connected"
    kinds = [e["kind"] for e in report["events"]]
    assert kinds.count("announce_withdrawn_originated") == 2
    assert kinds.count("subscribe_withdrawn_originated") == 2
    assert report["originated"] == {"announces": [], "subscribes": []}


@pytest.mark.parametrize("argv", [
    ["-p", "0"],
    ["--peer", "h", "--exit", "bogus"],
    ["--peer", "h", "--exit", "idle:0"],
    ["--peer", "h", "--exit", "peers-up:0"],
    ["--peer", "h", "--subscribe", "nameless"],
    ["--peer", "h:99999"],
])
def test_moq_fabric_probe_rejects(argv):
    with pytest.raises(SystemExit):
        moq_fabric_probe.parse_args(argv)
