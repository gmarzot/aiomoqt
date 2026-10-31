"""CPU model, count and clock for benchmark output."""
import pytest

from aiomoqt.utils import hostinfo

X86 = """processor\t: 0
model name\t: AMD EPYC 7J13 64-Core Processor
cpu MHz\t\t: 2449.998
"""
ARM = """processor\t: 0
BogoMIPS\t: 50.00
CPU implementer\t: 0x41
"""


@pytest.fixture
def linux(monkeypatch):
    files = {}
    monkeypatch.setattr(hostinfo.sys, "platform", "linux")
    monkeypatch.setattr(hostinfo, "_read", files.get)
    monkeypatch.setattr(hostinfo.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(hostinfo.platform, "processor", lambda: "")
    monkeypatch.setattr(hostinfo.platform, "machine", lambda: "x86_64")
    hostinfo.cpu_info.cache_clear()
    yield files
    hostinfo.cpu_info.cache_clear()


def test_linux_model_count_and_clock(linux):
    linux["/proc/cpuinfo"] = X86
    assert hostinfo.cpu_info()["cpu_model"] == "AMD EPYC 7J13 64-Core Processor"
    assert hostinfo.cpu_summary() == ("AMD EPYC 7J13 64-Core Processor, "
                                      "4 CPUs, 2.45 GHz (x86_64)")
    assert hostinfo.cpu_fields() == ["AMD EPYC 7J13 64-Core Processor", 4, 2450]


def test_linux_cpufreq_max_wins(linux):
    linux["/proc/cpuinfo"] = X86
    linux["/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"] = "3500000\n"
    assert hostinfo.cpu_info()["cpu_mhz"] == 3500


def test_missing_values_are_left_out(linux):
    linux["/proc/cpuinfo"] = ARM
    assert hostinfo.cpu_summary() == "unknown CPU, 4 CPUs (x86_64)"
    assert hostinfo.cpu_fields() == ["", 4, ""]


def test_unreadable_cpuinfo(linux):
    assert hostinfo.cpu_info()["cpu_model"] is None
    assert hostinfo.cpu_summary().startswith("unknown CPU")


def test_macos_sysctl(monkeypatch):
    values = {"machdep.cpu.brand_string": "Apple M2"}
    monkeypatch.setattr(hostinfo.sys, "platform", "darwin")
    monkeypatch.setattr(hostinfo, "_sysctl", values.get)
    monkeypatch.setattr(hostinfo.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(hostinfo.platform, "machine", lambda: "arm64")
    hostinfo.cpu_info.cache_clear()
    try:
        assert hostinfo.cpu_summary() == "Apple M2, 8 CPUs (arm64)"
    finally:
        hostinfo.cpu_info.cache_clear()


def test_sysctl_missing_binary(monkeypatch):
    def _missing(*a, **k):
        raise FileNotFoundError("sysctl")
    monkeypatch.setattr(hostinfo.subprocess, "run", _missing)
    assert hostinfo._sysctl("hw.ncpu") is None
