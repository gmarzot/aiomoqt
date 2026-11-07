"""load_sim's verdict: a group that subscribed but received nothing, or only a
trickle of what its subscribers should have seen, fails."""
import math
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
    GroupRT, GroupSpec, PubRT, PubSpec, ShardRT, _accrue_expected,
    _no_data_forever, _verdict,
)
from aiomoqt.utils.cli import SETUP_TIMEOUT


def test_no_time_limit_ends_when_no_data_arrives():
    empty, _ = _group(0, 0, 0)
    busy, _ = _group(3, 10, 1000)
    assert _no_data_forever(empty, math.inf, SETUP_TIMEOUT)
    assert not _no_data_forever(empty, math.inf, SETUP_TIMEOUT - 1)
    assert not _no_data_forever(empty, 300, SETUP_TIMEOUT)
    assert not _no_data_forever(busy, math.inf, SETUP_TIMEOUT)


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



def _av_group(video, audio, expected=(0.0, 0.0), peak=3):
    """A group on video and audio; `video` and `audio` are object counts."""
    vid = PubSpec({"namespace": "ns", "track": "video", "bitrate_kbps": 2190,
                   "rate": 30}, 1.0)
    aud = PubSpec({"namespace": "ns", "track": "audio", "bitrate_kbps": 128,
                   "rate": 46.875, "object_size": 341}, 1.0)
    g = GroupRT(GroupSpec(0, {"subs": 3}, vid, aud))
    shard = ShardRT(0, 3, None, None, None)
    shard.stats = {"total_objs": video + audio,
                   "total_bytes": (video + audio) * 1000,
                   "track_objs": {"video": video, "audio": audio}}
    g.shards.append(shard)
    g.max_live = peak
    g.expected_track = {"video": expected[0], "audio": expected[1]}
    g.expected_objs = sum(expected)
    pubs = {}
    for ps in (vid, aud):
        pubs[ps.key] = PubRT(ps)
        pubs[ps.key].published = True
    return {0: g}, pubs


def test_single_track_group_prints_no_track_lines():
    lines, _ = _verdict(*_group(peak=3, objs=293, nbytes=300032))
    assert not any(line.startswith("    ") for line in lines), lines


def test_multi_track_group_counts_each_track():
    lines, failed = _verdict(*_av_group(90, 140, expected=(90, 140)))
    text = "\n".join(lines)
    assert not failed
    assert "ns/video+audio" in text
    assert "    video" in text and "objs=90 expected=90" in text
    assert "    audio" in text and "objs=140 expected=140" in text
    assert "Total objects: 230" in text
    assert "Expected objects: 230" in text


def test_a_track_with_zero_objects_fails_the_group():
    lines, failed = _verdict(*_av_group(900, 0, expected=(900, 140)))
    text = "\n".join(lines)
    assert failed
    assert "<< audio: NO OBJECTS" in text
    assert "objs=0 expected=140  << NO OBJECTS" in text
    assert "RESULT: FAIL" in text


def test_a_track_below_the_floor_fails_though_the_sum_passes():
    lines, failed = _verdict(*_av_group(900, 20, expected=(900, 140)))
    text = "\n".join(lines)
    assert failed
    assert "objs=20 expected=140  << BELOW 50% OF EXPECTED" in text
    assert "RESULT: FAIL" in text


def test_expected_sums_the_tracks_rates():
    groups, pubs = _av_group(0, 0)
    g = groups[0]
    g.shards[0].live = 2
    _accrue_expected(groups, pubs, t=1.0, dt=1.0)
    assert g.expected_track == {"video": 60, "audio": 93.75}
    assert g.expected_objs == 60 + 93.75


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
    """A short load_sim against aiomoqt-relay on loopback; returns
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
