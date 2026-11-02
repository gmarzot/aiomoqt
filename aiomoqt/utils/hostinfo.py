"""This host's CPU model, CPU count and clock, for benchmark output.

Linux reads /proc/cpuinfo and cpufreq, macOS sysctl. A value that cannot
be read is None and is left out of the summary.
"""
import functools
import os
import platform
import subprocess
import sys
from typing import Optional

__all__ = ["cpu_info", "cpu_summary", "CPU_FIELDS", "cpu_fields"]

# Column names for machine-readable reports, in cpu_fields() order.
CPU_FIELDS = ("cpu_model", "cpu_count", "cpu_mhz")


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _linux() -> tuple[Optional[str], Optional[float]]:
    model = mhz = None
    text = _read("/proc/cpuinfo") or ""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields.setdefault(key.strip(), value.strip())
    # x86 gives "model name"; some ARM kernels give "Processor" or "Hardware".
    for key in ("model name", "Processor", "Hardware", "cpu model"):
        if fields.get(key):
            model = fields[key]
            break
    khz = _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    try:
        mhz = int(khz) / 1000 if khz else float(fields["cpu MHz"])
    except (KeyError, ValueError):
        mhz = None
    return model, mhz


def _sysctl(name: str) -> Optional[str]:
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True,
                             text=True, timeout=2, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


def _macos() -> tuple[Optional[str], Optional[float]]:
    model = _sysctl("machdep.cpu.brand_string")
    # Apple silicon reports no hw.cpufrequency.
    hz = _sysctl("hw.cpufrequency_max") or _sysctl("hw.cpufrequency")
    try:
        mhz = int(hz) / 1e6 if hz else None
    except ValueError:
        mhz = None
    return model, mhz


@functools.lru_cache(maxsize=None)
def cpu_info() -> dict:
    """cpu_model (str), cpu_count (logical CPUs, vCPUs on a VM), cpu_mhz
    (int) and machine (str); each None when unavailable."""
    try:
        if sys.platform.startswith("linux"):
            model, mhz = _linux()
        elif sys.platform == "darwin":
            model, mhz = _macos()
        else:
            model, mhz = None, None
    except Exception:
        model, mhz = None, None
    model = model or platform.processor() or None
    if model:
        model = " ".join(model.split())
    return {"cpu_model": model, "cpu_count": os.cpu_count(),
            "cpu_mhz": round(mhz) if mhz else None,
            "machine": platform.machine() or None}


def cpu_summary() -> str:
    """One line, e.g. 'AMD EPYC 7J13 64-Core Processor, 4 CPUs, 2.45 GHz
    (x86_64)'."""
    info = cpu_info()
    parts = [info["cpu_model"] or "unknown CPU"]
    if info["cpu_count"]:
        parts.append(f"{info['cpu_count']} CPUs")
    if info["cpu_mhz"]:
        mhz = info["cpu_mhz"]
        parts.append(f"{mhz / 1000:.2f} GHz" if mhz >= 1000 else f"{mhz} MHz")
    text = ", ".join(parts)
    return f"{text} ({info['machine']})" if info["machine"] else text


def cpu_fields() -> list:
    """CPU_FIELDS values for a CSV row; '' when unavailable."""
    info = cpu_info()
    return ["" if info[k] is None else info[k] for k in CPU_FIELDS]
