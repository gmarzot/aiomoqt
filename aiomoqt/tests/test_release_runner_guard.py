"""The runner's verdicts. Every suite fails on a non-zero exit. relay-pub-sub:
zero or missing objects, a FAIL or missing load_sim RESULT line fail.
Loopback: zero or missing objects, a missing throughput or a bad payload fail.
A single-case TAP run: ok passes, ok with a SKIP directive is a SKIP, not ok
fails."""
import importlib.util
import sys
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[2] / "tests" / "release_regression_test.py"

pytestmark = pytest.mark.skipif(not _RUNNER.exists(),
                                reason="needs the source tree's tests/")


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("release_regression_test", _RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SUMMARY = (
    "  aiomoqt/track                peak    3/3    joins=3 churn=0 exodus=0 "
    "fails=0 died=0 objs={objs} bytes={nbytes} rx=0MB\n"
    "{totals}"
    "{result}"
)


def _text(objs, totals=True, result="  RESULT: OK\n"):
    t = f"  Total objects: {objs}\n  Total bytes: {objs * 1024}\n" if totals else ""
    return _SUMMARY.format(objs=objs, nbytes=objs * 1024, totals=t, result=result)


def test_zero_objects_fails(runner):
    status, detail = runner._pub_sub_verdict(_text(0))
    assert status == "FAIL"
    assert "0 objects" in detail


def test_missing_object_count_fails(runner):
    status, detail = runner._pub_sub_verdict(_text(90, totals=False))
    assert status == "FAIL"
    assert "no object count" in detail


def test_objects_delivered_passes(runner):
    status, detail = runner._pub_sub_verdict(_text(90))
    assert status == "PASS"
    assert "90 objects" in detail


def test_short_peak_still_fails(runner):
    status, _ = runner._pub_sub_verdict(_text(90).replace("3/3", "2/3"))
    assert status == "FAIL"


def test_load_sim_result_fail_fails(runner):
    status, detail = runner._pub_sub_verdict(_text(90, result="  RESULT: FAIL\n"), 1)
    assert status == "FAIL"
    assert "RESULT: FAIL" in detail


def test_missing_result_line_fails(runner):
    status, detail = runner._pub_sub_verdict(_text(90, result=""))
    assert status == "FAIL"
    assert "no RESULT line" in detail


def test_nonzero_exit_fails(runner):
    status, detail = runner._pub_sub_verdict(_text(90), 1)
    assert status == "FAIL"
    assert "exit 1" in detail


def test_loopback_variants_run_this_interpreter(runner, monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, log, timeout):
        seen.append(cmd)
        log.write_text("")
        return 0, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    runner._loopback_pub_sub_variant(tmp_path, "v", ["-t", "1"], 5)
    assert seen and seen[0][0] == sys.executable


def _tap(line):
    return f"TAP version 14\n1..1\n{line}\n  ---\n  message: \"m\"\n  ...\n"


def test_a_tap_case_that_passes_is_a_pass(runner):
    text = _tap("ok 1 - namespace-discovery")
    assert runner._tap_case_verdict(text, "namespace-discovery") == ("PASS", "ok")


def test_a_tap_case_skip_is_a_skip_not_a_pass(runner):
    text = _tap("ok 1 - namespace-discovery # SKIP relay does not support PUBLISH")
    assert runner._tap_case_verdict(text, "namespace-discovery") == (
        "SKIP", "relay does not support PUBLISH")


def test_a_tap_case_that_fails_reports_its_message(runner):
    text = _tap("not ok 1 - namespace-discovery")
    assert runner._tap_case_verdict(text, "namespace-discovery") == ("FAIL", "m")


_LOOPBACK_OK = ("  Objects:     1,200\n  Bytes:       4,915,200\n"
                "  Throughput:  7.86 Mbps\n  Jitter:      0.10 ms\n"
                "  Lost:        0 (0.00%)\n  Out-of-order:   0\n")


def test_loopback_good_run_passes(runner):
    assert runner._loopback_verdict(_LOOPBACK_OK, 0) == ("PASS", "7.86 Mbps")


@pytest.mark.parametrize("text,rc,why", [
    ("  Objects:     1\n  Throughput:  1.0 Mbps\n  Lost:        0 (0.00%)\n",
     7, "exit 7"),
    ("  Objects:     0\n  Throughput:  0.0 Mbps\n  Lost:        0 (0.00%)\n",
     0, "0 objects"),
    ("  Lost:        0 (0.00%)\n", 0, "no summary"),
    (_LOOPBACK_OK + "  Payload:     1,200 checked, 3 bad\n", 1, "3 bad payloads"),
    (_LOOPBACK_OK.replace("0 (0.00%)", "4 (0.33%)"), 0, "4 lost"),
    (_LOOPBACK_OK, None, "timeout"),
])
def test_loopback_failures_fail(runner, text, rc, why):
    status, detail = runner._loopback_verdict(text, rc)
    assert status == "FAIL"
    assert why in detail


def test_adaptive_exit_fails_after_high_water(runner):
    status, detail = runner._adaptive_verdict("High-water: 12Mbps\n", 7)
    assert status == "FAIL"
    assert "exit 7" in detail


def test_adaptive_good_run_passes(runner):
    assert runner._adaptive_verdict("High-water: 60Mbps\n", 0) == (
        "PASS", "high-water 60 Mbps")


def test_pytest_exit_fails_after_summary(runner):
    status, detail = runner._pytest_verdict("10 passed in 0.01s\n", 7)
    assert status == "FAIL"
    assert "exit 7" in detail


def test_pytest_no_tests_fails(runner):
    status, _ = runner._pytest_verdict("no tests ran in 0.01s\n", 5)
    assert status == "FAIL"


def test_pytest_good_run_passes(runner):
    assert runner._pytest_verdict("10 passed, 1 skipped in 0.01s\n", 0) == (
        "PASS", "10 passed, 1 skipped in 0.01s")


def test_a_tap_case_ok_with_nonzero_exit_fails(runner):
    text = _tap("ok 1 - namespace-discovery")
    assert runner._tap_case_verdict(text, "namespace-discovery", 1) == (
        "FAIL", "ok, exit 1")


@pytest.mark.parametrize("suite", ["adaptive-mp", "adaptive-relay"])
def test_adaptive_suites_use_the_exit_code(runner, monkeypatch, tmp_path, suite):
    def fake_run(cmd, log, timeout):
        log.write_text("High-water: 12Mbps\n")
        return 1, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    if suite == "adaptive-mp":
        status, detail = runner._loopback_adaptive_mp(tmp_path, 22)
    else:
        class _Relay:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, stdout, stderr):
            stderr.write("Listening on 127.0.0.1\n")
            stderr.flush()
            return _Relay()

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        status, detail = runner._loopback_adaptive_relay_quic(tmp_path, 22)
    assert (status, detail) == ("FAIL", "exit 1, high-water 12 Mbps")
