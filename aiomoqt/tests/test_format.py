"""Formatted values pick precision and unit by the value as shown."""
import pytest

from aiomoqt.utils.format import fmt_bps, fmt_ms, fmt_rate


@pytest.mark.parametrize("bps, shown", [
    (499.4, "499bps"),
    (499.6, "0.5Kbps"),
    (499.6e3, "0.5Mbps"),
    (373.89e6, "374Mbps"),
    (999.6e6, "1.0Gbps"),
    (9.94e9, "9.9Gbps"),
    (9.96e9, "10Gbps"),
    (10.04e9, "10Gbps"),
])
def test_fmt_bps(bps, shown):
    assert fmt_bps(bps) == shown


@pytest.mark.parametrize("ms, shown", [
    (0.0042, "4.2us"),
    (0.836, "836us"),
    (0.9994, "999us"),
    (0.9996, "1.0ms"),
    (9.96, "10ms"),
    (999.6, "1.0s"),
])
def test_fmt_ms(ms, shown):
    assert fmt_ms(ms) == shown


@pytest.mark.parametrize("per_s, shown", [
    (9.94, "9.9/s"), (9.96, "10/s"), (74146.2, "74146/s"),
])
def test_fmt_rate(per_s, shown):
    assert fmt_rate(per_s) == shown


@pytest.mark.parametrize("fmt", [fmt_bps, fmt_ms, fmt_rate])
def test_invalid_is_dashes(fmt):
    assert fmt(float("nan")) == "--"
    assert fmt(-1.0) == "--"
