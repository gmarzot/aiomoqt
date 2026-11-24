"""load_sim as a process against the relay: over WebTransport, with a
packaged scenario file, and with a group on several tracks."""
import csv
import json
import re

import pytest

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port, run, spawn, stop, wait_for


@pytest.fixture
def relay(tmp_path):
    port = free_udp_port()
    log = tmp_path / "relay.log"
    proc = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                 "--dual", "--cert", CERT, "--key", KEY, log=log)
    try:
        wait_for(log, "Listening on", proc)
        yield port
    finally:
        stop(proc)


def _total_objects(out):
    m = re.search(r"^\s*Total objects: (\d+)$", out, re.MULTILINE)
    assert m, out
    return int(m.group(1))


@requires_certs
def test_load_sim_over_webtransport(relay):
    rc, out = run("load_sim", f"https://127.0.0.1:{relay}/", "--subs", 2,
                  "-t", 3, "-i", 1, "-k", "--draft", 18, "--pub-ns",
                  timeout=60)
    assert _total_objects(out) > 0, out
    assert "RESULT: OK" in out, out
    assert rc == 0, out


@requires_certs
def test_load_sim_runs_a_packaged_scenario(relay):
    rc, out = run("load_sim", f"moqt://127.0.0.1:{relay}", "-f", "default",
                  "--duration-scale", 0.05, "-t", 8, "-i", 2, "-k",
                  "--draft", 18, timeout=90)
    assert "scenarios/default.json" in out, out
    assert _total_objects(out) > 0, out
    assert rc == 0, out


def _two_track_scenario(path):
    pub = {"namespace": "av", "start_delay": 0, "duration": 4}
    path.write_text(json.dumps({
        "publishers": [
            dict(pub, track="video", bitrate_kbps=240, rate=30),
            dict(pub, track="audio", bitrate_kbps=128, rate=46.875,
                 object_size=341, gop_s=0.02),
        ],
        "subscribers": [
            {"namespace": "av", "tracks": ["video", "audio"], "subs": 2,
             "join_rate": 2, "churn_rate": 0.0},
        ],
    }))
    return path


@requires_certs
@pytest.mark.parametrize("scheme", ["moqt", "https"])
def test_load_sim_group_takes_several_tracks_on_one_session(relay, tmp_path,
                                                            scheme):
    scenario = _two_track_scenario(tmp_path / "av.json")
    url = (f"moqt://127.0.0.1:{relay}" if scheme == "moqt"
           else f"https://127.0.0.1:{relay}/")
    rc, out = run("load_sim", url, "-f", scenario, "-i", 1, "-k",
                  "--draft", 18, timeout=90)
    assert "av/video+audio" in out, out
    per_track = dict(re.findall(r"^\s+(video|audio)\s+objs=(\d+) ", out,
                                re.MULTILINE))
    assert set(per_track) == {"video", "audio"}, out
    assert all(int(n) > 0 for n in per_track.values()), out
    assert _total_objects(out) == sum(int(n) for n in per_track.values()), out
    assert "RESULT: OK" in out, out
    assert rc == 0, out


@requires_certs
def test_load_sim_reports_worst_p99_next_to_p90(relay, tmp_path):
    report = tmp_path / "run.csv"
    rc, out = run("load_sim", f"moqt://127.0.0.1:{relay}", "--subs", 2,
                  "-t", 3, "-i", 1, "-k", "--draft", 18, "--pub-ns",
                  "--report", report, timeout=60)
    assert _total_objects(out) > 0, out
    assert rc == 0, out
    lines = re.findall(r"^\s*T\+.* p90=(\d+)ms p99=(\d+)ms", out,
                       re.MULTILINE)
    assert lines, out
    assert all(int(p99) >= int(p90) for p90, p99 in lines), out
    with open(report, newline="") as f:
        rows = list(csv.reader(f))
    head = rows[0]
    assert head[-1] == "p99_ms" and head[6] == "p90_ms", rows[0]
    data = rows[1:]
    assert data, rows
    assert all(len(r) == len(head) for r in data), rows
    assert all(float(r[-1]) >= float(r[6]) for r in data), rows
    assert any(float(r[-1]) > 0 for r in data), rows
