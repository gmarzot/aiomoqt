"""Status for the ersatz relay's dashboard: host metrics from /proc,
event-loop lag, a rolling history, MSF catalog classification and an
atomic JSON writer. Host metrics are Linux only; elsewhere they are empty.
"""

import asyncio
import json
import os
import platform
from collections import deque
from typing import Any, Iterable, Optional

# Interfaces whose traffic is not the host's own.
_VIRTUAL_IFACES = ("lo", "docker", "veth", "br-", "virbr", "cni", "flannel")

# (implementer, part) -> core name, for ARM /proc/cpuinfo, which has no
# model name.
_ARM_CORES = {
    (0x41, 0xd0c): "Neoverse-N1", (0x41, 0xd40): "Neoverse-V1",
    (0x41, 0xd49): "Neoverse-N2", (0x41, 0xd4f): "Neoverse-V2",
}


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def parse_stat(text: str) -> dict[str, tuple[int, int]]:
    """/proc/stat cpu lines -> {name: (busy, total)} in jiffies."""
    out = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        name, *vals = line.split()
        # user nice system idle iowait irq softirq steal; guest is
        # already counted in user.
        nums = [int(v) for v in vals[:8]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        out[name] = (sum(nums) - idle, sum(nums))
    return out


def parse_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """/proc/net/dev -> {iface: (rx bytes, tx bytes)}."""
    out = {}
    for line in text.splitlines()[2:]:
        name, sep, data = line.partition(":")
        fields = data.split()
        if sep and len(fields) >= 9:
            out[name.strip()] = (int(fields[0]), int(fields[8]))
    return out


def parse_snmp_udp(text: str) -> dict[str, int]:
    """/proc/net/snmp Udp counters by name."""
    rows = [line.split()[1:] for line in text.splitlines()
            if line.startswith("Udp:")]
    if len(rows) < 2:
        return {}
    return {k: int(v) for k, v in zip(rows[0], rows[1])}


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo -> {key: bytes}."""
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            scale = 1024 if fields[1:2] == ["kB"] else 1
            out[key] = int(fields[0]) * scale
    return out


def parse_proc_self_stat(text: str) -> tuple[int, int, int]:
    """/proc/<pid>/stat -> (utime + stime ticks, threads, rss pages)."""
    # Fields after the parenthesised comm, which may itself hold spaces;
    # index 0 is field 3 (state).
    f = text.rpartition(")")[2].split()
    if len(f) < 22:
        return 0, 0, 0
    return int(f[11]) + int(f[12]), int(f[17]), int(f[21])


def parse_os_release(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def parse_cpu_model(text: str) -> str:
    """Model name; on ARM the core named from implementer and part."""
    implementer = part = None
    for line in text.splitlines():
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "model name":
            return val
        try:
            if key == "CPU implementer" and implementer is None:
                implementer = int(val, 0)
            elif key == "CPU part" and part is None:
                part = int(val, 0)
        except ValueError:
            continue
    if implementer is None:
        return platform.machine()
    return _ARM_CORES.get((implementer, part),
                          f"ARM {implementer:#x}/{part or 0:#x}")


def _host_iface(name: str) -> bool:
    return not name.startswith(_VIRTUAL_IFACES)


class HostSampler:
    """Host and process metrics, with rates taken between successive
    samples. `proc` and `os_release` are paths so a test can point them
    at a fixture tree."""

    def __init__(self, proc: str = "/proc",
                 os_release: Iterable[str] = ("/host/etc/os-release",
                                              "/etc/os-release"),
                 disk: str = "/") -> None:
        self.proc = proc
        self.disk = disk
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._page = (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf")
                      else 4096)
        self._prev: Optional[tuple] = None
        os_name = ""
        for path in os_release:
            os_name = parse_os_release(_read(path))
            if os_name:
                break
        cores = [k for k in parse_stat(self._proc("stat")) if k != "cpu"]
        self.static = {
            "cpu_model": parse_cpu_model(self._proc("cpuinfo")),
            "cores": len(cores) or os.cpu_count() or 0,
            "arch": platform.machine(),
            "kernel": platform.release(),
            "os": os_name,
        }

    def _proc(self, rel: str) -> str:
        return _read(os.path.join(self.proc, rel))

    def sample(self, now: float) -> dict[str, Any]:
        """One sample at monotonic time `now`; rates are None on the
        first."""
        stat = parse_stat(self._proc("stat"))
        net = parse_net_dev(self._proc("net/dev"))
        rx = sum(v[0] for k, v in net.items() if _host_iface(k))
        tx = sum(v[1] for k, v in net.items() if _host_iface(k))
        udp = parse_snmp_udp(self._proc("net/snmp"))
        mem = parse_meminfo(self._proc("meminfo"))
        ticks, threads, rss = parse_proc_self_stat(self._proc("self/stat"))
        load = self._proc("loadavg").split()[:3]
        uptime = self._proc("uptime").split()[:1]
        out = {
            "load": [float(x) for x in load],
            "mem_total": mem.get("MemTotal", 0),
            "mem_used": mem.get("MemTotal", 0) - mem.get("MemAvailable", 0),
            "uptime_s": float(uptime[0]) if uptime else None,
            "proc_rss": rss * self._page,
            "proc_threads": threads,
            "cpu": None, "cpu_cores": [], "rx_bps": None, "tx_bps": None,
            "udp_in_errors": None, "udp_rcvbuf_errors": None,
            "proc_cpu": None,
        }
        try:
            st = os.statvfs(self.disk)
            out["disk_total"] = st.f_blocks * st.f_frsize
            out["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
        except OSError:
            pass
        cur = (now, stat, rx, tx, udp, ticks)
        prev, self._prev = self._prev, cur
        if prev is None or now <= prev[0]:
            return out
        dt = now - prev[0]

        def pct(name):
            b0, t0 = prev[1].get(name, (0, 0))
            b1, t1 = stat.get(name, (0, 0))
            return round(100 * (b1 - b0) / (t1 - t0), 1) if t1 > t0 else 0.0

        out["cpu"] = pct("cpu")
        out["cpu_cores"] = [pct(k) for k in stat if k != "cpu"]
        # Counters reset when an interface goes away: clamp at zero.
        out["rx_bps"] = max(0, rx - prev[2]) * 8 / dt
        out["tx_bps"] = max(0, tx - prev[3]) * 8 / dt
        for key, name in (("udp_in_errors", "InErrors"),
                          ("udp_rcvbuf_errors", "RcvbufErrors")):
            if name in udp and name in prev[4]:
                out[key] = max(0, udp[name] - prev[4][name]) / dt
        out["proc_cpu"] = round(100 * (ticks - prev[5]) / self._hz / dt, 1)
        return out


def percentile(values: Iterable[float], q: float) -> Optional[float]:
    """Nearest-rank percentile, `q` in [0, 100]; None when empty."""
    s = sorted(values)
    if not s:
        return None
    return s[min(len(s) - 1, max(0, round(q / 100 * len(s)) - 1))]


class LoopLag:
    """How late the event loop runs a timer, sampled every `interval`."""

    def __init__(self, interval: float = 0.1, keep: int = 600) -> None:
        self.interval = interval
        self.samples: deque[float] = deque(maxlen=keep)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            t = loop.time()
            await asyncio.sleep(self.interval)
            self.samples.append(max(0.0, loop.time() - t - self.interval))

    def summary(self) -> dict[str, Optional[float]]:
        """p50, p99 and max lag in milliseconds."""
        ms = [s * 1000 for s in self.samples]
        return {"p50": percentile(ms, 50), "p99": percentile(ms, 99),
                "max": max(ms) if ms else None}


class History:
    """Aligned fixed-length series, columnar so the JSON stays small."""

    def __init__(self, keys: Iterable[str], length: int) -> None:
        self.series = {k: deque(maxlen=length) for k in keys}

    def add(self, values: dict[str, Any]) -> None:
        for key, col in self.series.items():
            col.append(values.get(key))

    def to_json(self) -> dict[str, list]:
        return {k: list(v) for k, v in self.series.items()}


# MSF packaging -> what the dashboard calls the broadcast.
_FORMATS = (("cmaf", "CMSF"), ("locmaf", "LOCMAF"), ("loc", "MSF"))


def classify_catalog(payload: bytes) -> Optional[str]:
    """"MSF", "CMSF" or "LOCMAF" (joined with "+" when mixed) for an MSF
    catalog object; None when the payload is not an independent MSF
    catalog with media tracks."""
    try:
        doc = json.loads(bytes(payload))
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or doc.get("deltaUpdate"):
        return None
    tracks = doc.get("tracks")
    if not isinstance(tracks, list):
        return None
    found = {t.get("packaging") for t in tracks if isinstance(t, dict)}
    names = [label for pkg, label in _FORMATS if pkg in found]
    return "+".join(names) or None


def write_json_atomic(path: str, obj: Any) -> None:
    """Replace `path` whole, so a reader never sees a partial file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def versions() -> dict[str, str]:
    """aiomoqt, aiopquic, picoquic and Python versions."""
    out = {"python": platform.python_version()}
    try:
        import aiomoqt
        out["aiomoqt"] = aiomoqt.__version__
    except (ImportError, AttributeError):
        out["aiomoqt"] = "unknown"
    try:
        import aiopquic
        out["aiopquic"] = aiopquic.__version__
    except (ImportError, AttributeError):
        out["aiopquic"] = "unknown"
    try:
        from aiopquic import _build_info as bi
        pq = getattr(bi, "PICOQUIC_DESCRIBE", "") or ""
        if not pq or pq == "unknown":
            pq = (getattr(bi, "PICOQUIC_SHA", "") or "unknown")[:10]
        out["picoquic"] = pq
    except ImportError:
        out["picoquic"] = "unknown"
    return out
