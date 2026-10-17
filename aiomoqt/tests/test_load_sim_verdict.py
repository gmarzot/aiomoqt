"""load_sim's verdict: a group that subscribed but received nothing, or only a
trickle of what its subscribers should have seen, fails."""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tools.load_sim import (
    GroupRT, GroupSpec, PubRT, PubSpec, ShardRT, _accrue_expected, _verdict,
)


def _group(peak, objs, nbytes, subs=3, expected=0.0):
    pub = PubSpec({"namespace": "ns", "track": "t", "bitrate_kbps": 100}, 1.0)
    g = GroupRT(GroupSpec(0, {"subs": subs}, pub))
    shard = ShardRT(0, subs, None, None, None)
    shard.stats = {"total_objs": objs, "total_bytes": nbytes}
    g.shards.append(shard)
    g.max_live = peak
    g.expected_objs = expected
    prt = PubRT(pub)
    prt.published = True
    return {0: g}, {pub.key: prt}


def test_subscribed_but_zero_bytes_fails():
    lines, failed = _verdict(*_group(peak=3, objs=0, nbytes=0))
    text = "\n".join(lines)
    assert failed
    assert "<< NO OBJECTS" in text
    assert "Total objects: 0" in text
    assert "Total bytes: 0" in text
    assert "RESULT: FAIL" in text


def test_delivery_passes_with_exact_counts():
    # 300 KB rounds to rx=0MB; the exact counts must still show it.
    lines, failed = _verdict(*_group(peak=3, objs=293, nbytes=300032))
    text = "\n".join(lines)
    assert not failed
    assert "NO OBJECTS" not in text
    assert "Total objects: 293" in text
    assert "Total bytes: 300032" in text
    assert "RESULT: OK" in text


def test_never_joined_is_not_relabelled():
    lines, failed = _verdict(*_group(peak=0, objs=0, nbytes=0))
    text = "\n".join(lines)
    assert failed
    assert "<< NEVER JOINED" in text
    assert "NO OBJECTS" not in text


def test_trickle_below_half_of_expected_fails():
    lines, failed = _verdict(*_group(peak=3, objs=5, nbytes=5120, expected=270))
    text = "\n".join(lines)
    assert failed
    assert "<< BELOW 50% OF EXPECTED" in text
    assert "objs=5 expected=270" in text
    assert "Total objects: 5" in text
    assert "Expected objects: 270" in text
    assert "RESULT: FAIL" in text


def test_most_of_expected_passes():
    lines, failed = _verdict(*_group(peak=3, objs=200, nbytes=204800, expected=270))
    text = "\n".join(lines)
    assert not failed
    assert "objs=200 expected=270" in text
    assert "RESULT: OK" in text


def test_expected_accrues_per_live_subscriber_second():
    groups, pubs = _group(peak=3, objs=0, nbytes=0)
    g = groups[0]
    g.shards[0].live = 3
    fps = g.spec.pub.fps
    _accrue_expected(groups, pubs, t=1.0, dt=1.0)
    assert g.expected_objs == 3 * fps
    # Nothing accrues before the publisher is confirmed or after it ends.
    pubs[g.spec.key].published = False
    _accrue_expected(groups, pubs, t=2.0, dt=1.0)
    pubs[g.spec.key].published = True
    pubs[g.spec.key].ended = True
    _accrue_expected(groups, pubs, t=3.0, dt=1.0)
    # Nor outside the publisher's timeline.
    pubs[g.spec.key].ended = False
    _accrue_expected(groups, pubs, t=g.spec.pub.duration + 1, dt=1.0)
    assert g.expected_objs == 3 * fps
    # Audience role: no local publisher, the timeline alone decides.
    _accrue_expected(groups, {}, t=4.0, dt=0.5)
    assert g.expected_objs == 3 * fps + 1.5 * fps


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listening(log_path, relay, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "Listening on" in log_path.read_text(errors="replace"):
            return
        if relay.poll() is not None:
            break
        time.sleep(0.1)
    raise AssertionError("relay did not start:\n"
                         + log_path.read_text(errors="replace"))


def run_loopback_load_sim(*extra):
    """A short load_sim against the interop relay on loopback; returns
    (exit code, stdout + stderr)."""
    port = _free_udp_port()
    relay_log = Path(tempfile.mkdtemp(prefix="load-sim-relay-")) / "relay.log"
    with open(relay_log, "w") as log:
        relay = subprocess.Popen(
            [sys.executable, "-m", "aiomoqt.tools.moq_interop_relay",
             "--bind", "127.0.0.1", "--port", str(port), "--quic",
             "--cert", CERT, "--key", KEY],
            stdout=subprocess.DEVNULL, stderr=log)
    try:
        _wait_listening(relay_log, relay)
        # --pub-ns: PUB_NS alone, no PUBLISH/SUBSCRIBE crossing at the relay.
        proc = subprocess.run(
            [sys.executable, "-m", "aiomoqt.tools.load_sim",
             f"moqt://127.0.0.1:{port}", "--subs", "2", "-t", "2",
             "-i", "1", "-k", "--draft", "18", "--pub-ns", *extra],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    finally:
        relay.terminate()
        relay.wait(timeout=10)
    return proc.returncode, proc.stdout + proc.stderr


@requires_certs
def test_loopback_load_sim_reports_exact_objects():
    rc, out = run_loopback_load_sim()
    m = re.search(r"^\s*Total objects: (\d+)$", out, re.MULTILINE)
    assert m, out
    assert int(m.group(1)) > 0, out
    assert "RESULT: OK" in out, out
    assert rc == 0, out
