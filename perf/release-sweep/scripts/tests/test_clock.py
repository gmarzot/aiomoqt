"""Clock-offset sign convention (the one thing that silently flips every latency)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect  # noqa: E402
import sweep  # noqa: E402


class ClockSignTest(unittest.TestCase):
    def test_correction_is_negated_sub_minus_pub(self):
        # Sub clock 1.5 s ahead of the pub clock: raw one-way latency is inflated by 1500 ms.
        self.assertEqual(collect.correction_ms(1_500_000), -1500.0)
        parsed = {"intervals": [{"lat_avg_ms": 1502.0, "p99_ms": 1510.0}],
                  "summary": {"p50": 1502.0, "p95": 1508.0, "p99": 1510.0,
                              "lat_min_ms": 1501.0, "lat_avg_ms": 1503.0, "lat_max_ms": 1520.0}}
        collect.apply_offset(parsed, collect.correction_ms(1_500_000))
        self.assertAlmostEqual(parsed["summary"]["p50"], 2.0)
        self.assertAlmostEqual(parsed["summary"]["p99"], 10.0)
        self.assertAlmostEqual(parsed["intervals"][0]["lat_avg_ms"], 2.0)

    def test_cross_clock_negates_probe_made_from_seawolf(self):
        # Probe runs on seawolf against shenzhen, so the raw offset is (shenzhen - seawolf).
        hc = {"clock": {"method": "tcp-peer", "available": True, "offset_us": -1_500_000.0,
                        "error_us": 2000.0, "samples": 20, "used": 20,
                        "peer": "10.14.25.173:9876"}}
        c = sweep.cross_clock(hc)
        self.assertEqual(c["w_minus_s_us"], 1_500_000.0)
        self.assertEqual(c["error_us"], 2000.0)

    def test_unmeasured_clock_is_none_never_zero(self):
        self.assertIsNone(sweep.cross_clock(None))
        self.assertIsNone(sweep.cross_clock(
            {"clock": {"method": "ntp", "available": True, "offset_us": 5.0}}))
        self.assertIsNone(sweep.cross_clock(
            {"clock": {"method": "tcp-peer", "available": False}}))

    def test_latency_keys_are_the_offset_dependent_ones(self):
        for k in ("p50", "p95", "p99", "lat_avg_ms"):
            self.assertIn(k, collect.LAT_KEYS)
        for k in ("jitter_ms", "lat_sd_ms", "mbps", "lost"):
            self.assertNotIn(k, collect.LAT_KEYS)


if __name__ == "__main__":
    unittest.main()
