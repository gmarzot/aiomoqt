"""Appendix A validator: accepts honest records, rejects silent nulls and missing provenance."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import validate_result as vr  # noqa: E402


def good() -> dict:
    nm = {"p99_9": "sub_bench reports p50/p95/p99 only", "samples.p50": "interval rows carry avg and p99",
          "cpu_pub": "no /proc sample"}
    return {
        "point_id": "D-raw", "arm": "base-070rc2", "topology": "D", "transport": "raw", "draft": 16,
        "config": {"P": 4, "s": 4096, "g": 200, "r": 0, "t": 30},
        "hosts": {"relay": "shenzhen", "load": "seawolf", "subs": 1},
        "shas": {"aiomoqt": "b811c16", "aiopquic": "v0.7.0rc2", "picoquic": "70b5fd5a"},
        "env": {"cc": "bbr1", "jemalloc": False, "gso": True, "qdisc": "mq", "pinning": "pub 2-5",
                "clock_offset_us": 1500000.0, "fingerprint": {"shenzhen": {"nproc": 16}}},
        "started_utc": "2026-10-30T18:00:00Z", "duration_s": 40.0,
        "samples": [{"t_s": 5, "mbps": 100.0, "p50": None, "p99": 3.0}],
        "summary": {"mbps": 100.0, "obj_s": 10.0, "grp_s": 1.0, "p50": 1.0, "p95": 2.0, "p99": 3.0,
                    "p99_9": None, "jitter_ms": 0.1, "lost": 0, "ooo": 0, "interval_cv": 0.01,
                    "cpu_pub": None, "cpu_sub": 55.0, "rss_max_mb": 120.0, "not_measured": nm},
        "artifacts": ["sub.log"],
        "meta": {"status": "ok", "run_id": "2026-10-30T18-00-00Z", "rep": 1, "arm": "base-070rc2",
                 "point_argv": {}, "hostcheck_pre_local": {}, "hostcheck_post_local": {},
                 "config_fingerprint": {}, "clock": {"latency_why": None}},
    }


class ValidateTest(unittest.TestCase):
    def test_honest_record_is_valid(self):
        self.assertEqual(vr.validate(good()), [])

    def test_missing_key_is_reported(self):
        d = good()
        del d["summary"]["p95"]
        self.assertTrue(any("summary.p95" in e for e in vr.validate(d)))

    def test_silent_null_is_rejected(self):
        d = good()
        d["summary"]["cpu_sub"] = None  # no not_measured reason given
        self.assertTrue(any("cpu_sub" in e and "not_measured" in e for e in vr.validate(d)))

    def test_made_up_provenance_is_rejected(self):
        d = good()
        d["shas"]["aiopquic"] = "unknown"
        d["meta"]["run_id"] = ""
        d["env"]["fingerprint"] = {}
        errs = " ".join(vr.validate(d))
        self.assertIn("shas.aiopquic", errs)
        self.assertIn("run id", errs)
        self.assertIn("env fingerprint", errs)

    def test_clock_offset_null_needs_a_reason(self):
        d = good()
        d["env"]["clock_offset_us"] = None
        self.assertTrue(any("clock_offset_us" in e for e in vr.validate(d)))
        d["meta"]["clock"]["latency_why"] = "cross-host clock offset not measured"
        self.assertEqual(vr.validate(copy.deepcopy(d)), [])

    def test_aborted_point_is_a_record_not_a_measurement(self):
        d = good()
        d["meta"]["status"] = "aborted_pre"
        self.assertTrue(any("not a completed measurement" in e for e in vr.validate(d)))
        self.assertFalse(any("not a completed" in e for e in vr.validate(d, require_complete=False)))


if __name__ == "__main__":
    unittest.main()
