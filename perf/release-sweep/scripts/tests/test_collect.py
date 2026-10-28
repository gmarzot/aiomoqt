#!/usr/bin/env python3
"""Unit tests for collect.py against captured bench transcripts.

Run:  python3 -m unittest discover -s perf/release-sweep/scripts/tests -v
Fixtures are real 8 s transcripts captured on shenzhen (see README).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

import collect  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class TestSubFlavour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = collect.parse_file(os.path.join(FIX, "sub_bench_raw_d8s.log"))

    def test_flavour_and_interval_count(self):
        self.assertEqual(self.parsed["flavour"], "sub")
        self.assertEqual(len(self.parsed["intervals"]), 4)
        self.assertEqual([iv["t_s"] for iv in self.parsed["intervals"]], [2.0, 4.0, 6.0, 8.0])

    def test_interval_values(self):
        first = self.parsed["intervals"][0]
        self.assertEqual(first["groups"], 20)
        self.assertEqual(first["objects"], 80336)
        self.assertEqual(first["mbps"], 1300.0)
        self.assertAlmostEqual(first["lat_avg_ms"], 5.9)
        self.assertAlmostEqual(first["p99_ms"], 10.0)
        self.assertAlmostEqual(first["jitter_ms"], 0.021, places=3)
        self.assertEqual(first["lost"], 0)

    def test_summary(self):
        s = self.parsed["summary"]
        self.assertEqual(s["objects"], 383129)
        self.assertEqual(s["groups"], 94)
        self.assertEqual(s["bytes"], 1574277061)
        self.assertAlmostEqual(s["mbps"], 1517.01)
        self.assertAlmostEqual(s["obj_s"], 46149.1)
        self.assertAlmostEqual(s["p50"], 4.8)
        self.assertAlmostEqual(s["p95"], 7.2)
        self.assertAlmostEqual(s["p99"], 9.3)
        self.assertAlmostEqual(s["lat_avg_ms"], 4.9)
        self.assertAlmostEqual(s["lat_max_ms"], 14.3)
        self.assertAlmostEqual(s["jitter_ms"], 0.02)
        self.assertEqual(s["lost"], 0)
        self.assertEqual(s["ooo"], 0)
        self.assertEqual(s["timestamps"], "loc")
        self.assertAlmostEqual(s["active_s"], 8.3)
        self.assertNotIn("unparsed_rows", s)

    def test_interval_cv(self):
        # 4 intervals at 1300/1500/1600/1600 Mbps
        cv = self.parsed["summary"]["interval_cv"]
        self.assertGreater(cv, 0.0)
        self.assertLess(cv, 0.2)

    def test_sample_rows_shape(self):
        rows = collect.sample_rows(self.parsed)
        self.assertEqual(len(rows), 4)
        self.assertEqual(set(rows[0]), {"t_s", "mbps", "lat_ms", "p99"})
        self.assertEqual(rows[1]["t_s"], 4.0)


class TestSenderFlavour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = collect.parse_file(os.path.join(FIX, "pub_server_raw_d8s.log"))

    def test_flavour(self):
        self.assertEqual(self.parsed["flavour"], "sender")

    def test_intervals_have_no_latency(self):
        self.assertEqual(len(self.parsed["intervals"]), 1)
        iv = self.parsed["intervals"][0]
        self.assertEqual(iv["t_s"], 5.0)
        self.assertAlmostEqual(iv["mbps"], 1500.0)
        self.assertNotIn("lat_avg_ms", iv)

    def test_sent_summary(self):
        s = self.parsed["summary"]
        self.assertEqual(s["objects"], 383371)
        self.assertEqual(s["groups"], 94)
        self.assertAlmostEqual(s["obj_s"], 46167.0)
        self.assertAlmostEqual(s["mbps"], 1500.0)

    def test_sample_rows_have_no_latency_keys(self):
        rows = collect.sample_rows(self.parsed)
        self.assertEqual(set(rows[0]), {"t_s", "mbps"})


class TestEdgeCases(unittest.TestCase):
    def test_offset_applied_to_latency_only(self):
        parsed = collect.parse_file(os.path.join(FIX, "sub_bench_raw_d8s.log"))
        before_mbps = parsed["intervals"][0]["mbps"]
        collect.apply_offset(parsed, 3.0)
        self.assertAlmostEqual(parsed["intervals"][0]["lat_avg_ms"], 8.9)
        self.assertAlmostEqual(parsed["summary"]["p99"], 12.3)
        self.assertEqual(parsed["intervals"][0]["mbps"], before_mbps)
        self.assertEqual(parsed["summary"]["clock_offset_ms"], 3.0)

    def test_unparsable_row_is_counted_not_dropped(self):
        text = (
            "  Interval  Grps  GrpRate  Objs  ObjRate  Bitrate  Latency\n"
            "  0-2s      !!broken row!!\n"
        )
        parsed = collect.parse_log(text)
        self.assertEqual(parsed["intervals"], [])
        self.assertEqual(parsed["summary"]["unparsed_rows"], 1)

    def test_empty_log(self):
        parsed = collect.parse_log("")
        self.assertEqual(parsed["intervals"], [])
        self.assertEqual(parsed["summary"], {})
        self.assertEqual(parsed["flavour"], "unknown")

    def test_csv_rollup_from_result_json(self):
        result = {
            "point_id": "D1",
            "arm": "base-070rc2",
            "topology": "D",
            "transport": "raw",
            "started_utc": "2026-10-28T13:00:00Z",
            "duration_s": 65,
            "env": {"clock_offset_us": 84},
            "summary": {"mbps": 940.2, "p99": 12.1, "interval_cv": 0.04},
            "meta": {"run_id": "2026-10-28T13-00-00Z", "rep": 1, "status": "ok"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs", "2026-10-28T13-00-00Z", "D1", "rep-01")
            os.makedirs(path)
            with open(os.path.join(path, "result.json"), "w") as fh:
                json.dump(result, fh)
            csv_path = os.path.join(tmp, "rollup.csv")
            rows = []
            for p in collect.walk_runs(os.path.join(tmp, "runs")):
                with open(p) as fh:
                    rows.append(json.load(fh))
            flattened = collect.build_csv(rows)
            self.assertEqual(len(flattened), 1)
            self.assertEqual(flattened[0]["point_id"], "D1")
            self.assertEqual(flattened[0]["clock_offset_us"], 84)
            self.assertEqual(list(flattened[0].keys()), list(collect.FIELDS))
            self.assertEqual(collect.main(["--runs", os.path.join(tmp, "runs"), "--csv", csv_path]), 0)
            self.assertTrue(os.path.exists(csv_path))


if __name__ == "__main__":
    unittest.main()
