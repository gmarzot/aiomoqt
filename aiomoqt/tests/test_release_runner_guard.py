"""The runner's relay-pub-sub verdict: zero or missing objects, a FAIL or missing
load_sim RESULT line, or a non-zero exit fail."""
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
        return True, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    runner._loopback_pub_sub_variant(tmp_path, "v", ["-t", "1"], 5)
    assert seen and seen[0][0] == sys.executable
