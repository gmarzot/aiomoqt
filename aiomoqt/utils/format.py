"""Number/rate/time formatting helpers for human-readable output.

Convention ("2-place" style):
  value < 10  → one decimal place  ("0.4", "9.9")
  value >= 10 → integer            ("12", "480")

Adaptive units for bitrate and time: scale up/down to keep the numeric
part within [0.1, 500) so it stays readable across 6+ orders of magnitude.
"""


def _num2(x: float) -> str:
    """Render x as 'N.M' below 10, integer above."""
    if x < 10:
        return f"{x:.1f}"
    return f"{x:.0f}"


def fmt_bps(bps: float) -> str:
    """Format a bitrate in bits/sec with adaptive SI units (Kbps/Mbps/Gbps)."""
    if bps != bps or bps < 0:  # NaN or negative
        return "--"
    if bps < 500:
        return _num2(bps) + "bps"
    if bps < 500_000:
        return _num2(bps / 1_000) + "Kbps"
    if bps < 500_000_000:
        return _num2(bps / 1_000_000) + "Mbps"
    return _num2(bps / 1_000_000_000) + "Gbps"


def fmt_ms(ms: float) -> str:
    """Format a time in ms with adaptive units (us/ms/s)."""
    if ms != ms or ms < 0:
        return "--"
    if ms < 1:
        return _num2(ms * 1000) + "us"
    if ms < 1000:
        return _num2(ms) + "ms"
    return _num2(ms / 1000) + "s"


def fmt_rate(per_s: float) -> str:
    """Format an object/event rate per second with 2-place style."""
    if per_s != per_s or per_s < 0:
        return "--"
    return _num2(per_s) + "/s"
