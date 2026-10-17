"""Status for the ersatz relay's dashboard: host metrics from /proc,
event-loop lag, a rolling history, MSF catalog classification and an
atomic JSON writer. Host metrics are Linux only; elsewhere they are empty.
"""

import asyncio
import ipaddress
import json
import os
import platform
import re
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


_CPU_MODES = ("user", "nice", "system", "idle", "iowait", "irq", "softirq",
              "steal")
_STAT_COUNTERS = ("ctxt", "intr", "processes", "procs_running",
                  "procs_blocked")
# Whole block devices; partitions, loop, ram and dm devices are left out.
_DISK = re.compile(r"(sd|vd|xvd|hd)[a-z]+|nvme\d+n\d+|mmcblk\d+")
_UDP_KEYS = ("InDatagrams", "OutDatagrams", "InErrors", "RcvbufErrors",
             "SndbufErrors", "NoPorts")


def parse_cpu_modes(text: str) -> dict[str, list[int]]:
    """/proc/stat cpu lines -> {name: jiffies in _CPU_MODES order}."""
    out = {}
    for line in text.splitlines():
        if line.startswith("cpu"):
            name, *vals = line.split()
            nums = [int(v) for v in vals[:8]]
            out[name] = nums + [0] * (8 - len(nums))
    return out


def parse_stat_counters(text: str) -> dict[str, int]:
    """/proc/stat ctxt, intr (total), processes (forks), procs_running and
    procs_blocked."""
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(" ")
        if key in _STAT_COUNTERS and rest.split():
            out[key] = int(rest.split()[0])
    return out


def parse_pressure(text: str) -> dict[str, float]:
    """/proc/pressure/<resource> -> {"some": avg10, "full": avg10}."""
    out = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        for field in fields:
            key, _, val = field.partition("=")
            if key == "avg10":
                out[kind] = float(val)
    return out


def parse_diskstats(text: str) -> dict[str, tuple[int, int, int, int, int]]:
    """/proc/diskstats whole disks -> {name: (reads, sectors read, writes,
    sectors written, ms doing I/O)}."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 13 and _DISK.fullmatch(f[2]):
            out[f[2]] = (int(f[3]), int(f[5]), int(f[7]), int(f[9]), int(f[12]))
    return out


def parse_net_dev_full(text: str) -> dict[str, tuple[int, ...]]:
    """/proc/net/dev -> {iface: (rx bytes, packets, errs, drop, tx bytes,
    packets, errs, drop)}."""
    out = {}
    for line in text.splitlines()[2:]:
        name, sep, data = line.partition(":")
        f = data.split()
        if sep and len(f) >= 12:
            out[name.strip()] = tuple(int(f[i]) for i in (0, 1, 2, 3, 8, 9, 10, 11))
    return out


def parse_snmp6_udp(text: str) -> dict[str, int]:
    """/proc/net/snmp6 Udp6 counters, named as in /proc/net/snmp."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 2 and f[0].startswith("Udp6"):
            out[f[0][4:]] = int(f[1])
    return out


def parse_udp_sockets(text: str, port: int) -> Optional[tuple[int, int, int]]:
    """(tx queue, rx queue, drops) summed over the /proc/net/udp{,6}
    sockets bound to local `port`; None when there are none."""
    found, txq, rxq, drops = False, 0, 0, 0
    for line in text.splitlines():
        f = line.split()
        if len(f) < 13 or ":" not in f[1] or ":" not in f[4]:
            continue
        try:
            if int(f[1].rsplit(":", 1)[1], 16) != port:
                continue
            tq, rq = (int(x, 16) for x in f[4].split(":"))
            found, txq, rxq, drops = True, txq + tq, rxq + rq, drops + int(f[-1])
        except ValueError:
            continue
    return (txq, rxq, drops) if found else None


def parse_proc_times(text: str) -> tuple[int, int]:
    """/proc/<pid>/stat -> (utime, stime) in clock ticks."""
    f = text.rpartition(")")[2].split()
    return (int(f[11]), int(f[12])) if len(f) > 12 else (0, 0)


def parse_proc_status(text: str) -> dict[str, int]:
    """/proc/<pid>/status context-switch counters."""
    out = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        if key in ("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches"):
            out[key] = int(val.split()[0])
    return out


def _host_iface(name: str) -> bool:
    return not name.startswith(_VIRTUAL_IFACES)


class HostSampler:
    """Host and process metrics, with rates taken between successive
    samples. `proc` and `os_release` are paths so a test can point them
    at a fixture tree; `udp_port` names the relay's own socket."""

    def __init__(self, proc: str = "/proc",
                 os_release: Iterable[str] = ("/host/etc/os-release",
                                              "/etc/os-release"),
                 disk: str = "/", udp_port: Optional[int] = None) -> None:
        self.proc = proc
        self.disk = disk
        self.udp_port = udp_port
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._page = (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf")
                      else 4096)
        self._prev: Optional[dict] = None
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

    def _read_counters(self, now: float) -> dict[str, Any]:
        stat_text = self._proc("stat")
        net = {k: v for k, v in parse_net_dev_full(self._proc("net/dev")).items()
               if _host_iface(k)}
        udp4 = parse_snmp_udp(self._proc("net/snmp"))
        udp6 = parse_snmp6_udp(self._proc("net/snmp6"))
        sock = None
        if self.udp_port:
            sock = parse_udp_sockets(
                self._proc("net/udp") + "\n" + self._proc("net/udp6"),
                self.udp_port)
        self_stat = self._proc("self/stat")
        try:
            fds = len(os.listdir(os.path.join(self.proc, "self/fd")))
        except OSError:
            fds = None
        return {
            "t": now, "stat": parse_stat(stat_text),
            "modes": parse_cpu_modes(stat_text),
            "counters": parse_stat_counters(stat_text),
            "net": net,
            "udp": {k: udp4.get(k, 0) + udp6.get(k, 0) for k in _UDP_KEYS
                    if k in udp4 or k in udp6},
            "disks": parse_diskstats(self._proc("diskstats")),
            "sock": sock,
            "self": parse_proc_self_stat(self_stat),
            "times": parse_proc_times(self_stat),
            "ctx": parse_proc_status(self._proc("self/status")),
            "fds": fds,
        }

    def sample(self, now: float) -> dict[str, Any]:
        """One sample at monotonic time `now`; rates are None on the
        first."""
        cur = self._read_counters(now)
        mem = parse_meminfo(self._proc("meminfo"))
        load = self._proc("loadavg").split()[:3]
        uptime = self._proc("uptime").split()[:1]
        _ticks, threads, rss = cur["self"]
        cached = mem.get("Cached", 0) + mem.get("SReclaimable", 0)
        out = {
            "load": [float(x) for x in load],
            "mem_total": mem.get("MemTotal", 0),
            "mem_used": mem.get("MemTotal", 0) - mem.get("MemAvailable", 0),
            "mem": {
                "total": mem.get("MemTotal", 0),
                "available": mem.get("MemAvailable", 0),
                "free": mem.get("MemFree", 0),
                "buffers": mem.get("Buffers", 0),
                "cached": cached,
                "used": max(0, mem.get("MemTotal", 0) - mem.get("MemFree", 0)
                            - mem.get("Buffers", 0) - cached),
                "swap_total": mem.get("SwapTotal", 0),
                "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
            },
            "uptime_s": float(uptime[0]) if uptime else None,
            "proc_rss": rss * self._page,
            "proc_threads": threads,
            "procs_running": cur["counters"].get("procs_running"),
            "procs_blocked": cur["counters"].get("procs_blocked"),
            "psi": {r: parse_pressure(self._proc(f"pressure/{r}"))
                    for r in ("cpu", "io", "memory")},
            "proc": {"rss": rss * self._page, "threads": threads,
                     "fds": cur["fds"], "user_pct": None, "system_pct": None,
                     "ctx_vol_per_s": None, "ctx_invol_per_s": None},
            "udp_socket": None,
            "cpu": None, "cpu_cores": [], "cpu_modes": {}, "rx_bps": None,
            "tx_bps": None, "udp_in_errors": None, "udp_rcvbuf_errors": None,
            "proc_cpu": None, "ctxt_per_s": None, "intr_per_s": None,
            "forks_per_s": None, "disks": [], "ifaces": [], "udp": {},
        }
        if cur["sock"] is not None:
            txq, rxq, drops = cur["sock"]
            out["udp_socket"] = {"port": self.udp_port, "tx_queue": txq,
                                 "rx_queue": rxq, "drops": drops,
                                 "drops_per_s": None}
        try:
            st = os.statvfs(self.disk)
            out["disk_total"] = st.f_blocks * st.f_frsize
            out["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
        except OSError:
            pass
        prev, self._prev = self._prev, cur
        if prev is None or now <= prev["t"]:
            return out
        dt = now - prev["t"]

        def per_s(a, b):
            # Counters reset when their source goes away: clamp at zero.
            return max(0, a - b) / dt

        def pct(name):
            b0, t0 = prev["stat"].get(name, (0, 0))
            b1, t1 = cur["stat"].get(name, (0, 0))
            return round(100 * (b1 - b0) / (t1 - t0), 1) if t1 > t0 else 0.0

        out["cpu"] = pct("cpu")
        out["cpu_cores"] = [pct(k) for k in cur["stat"] if k != "cpu"]
        m0, m1 = prev["modes"].get("cpu"), cur["modes"].get("cpu")
        if m0 and m1 and sum(m1) > sum(m0):
            total = sum(m1) - sum(m0)
            out["cpu_modes"] = {mode: round(100 * (b - a) / total, 1)
                                for mode, a, b in zip(_CPU_MODES, m0, m1)}
        c0, c1 = prev["counters"], cur["counters"]
        for key, name in (("ctxt_per_s", "ctxt"), ("intr_per_s", "intr"),
                          ("forks_per_s", "processes")):
            if name in c0 and name in c1:
                out[key] = round(per_s(c1[name], c0[name]), 1)
        rx = sum(v[0] for v in cur["net"].values())
        tx = sum(v[4] for v in cur["net"].values())
        out["rx_bps"] = per_s(rx, sum(v[0] for v in prev["net"].values())) * 8
        out["tx_bps"] = per_s(tx, sum(v[4] for v in prev["net"].values())) * 8
        for name, v in sorted(cur["net"].items()):
            p = prev["net"].get(name)
            if p is None:
                continue
            out["ifaces"].append({
                "name": name,
                "rx_bps": per_s(v[0], p[0]) * 8, "tx_bps": per_s(v[4], p[4]) * 8,
                "rx_pps": per_s(v[1], p[1]), "tx_pps": per_s(v[5], p[5]),
                "errs_per_s": per_s(v[2] + v[6], p[2] + p[6]),
                "drops_per_s": per_s(v[3] + v[7], p[3] + p[7]),
            })
        u0, u1 = prev["udp"], cur["udp"]
        out["udp"] = {k: round(per_s(u1[k], u0[k]), 1) for k in u1 if k in u0}
        out["udp_in_errors"] = out["udp"].get("InErrors")
        out["udp_rcvbuf_errors"] = out["udp"].get("RcvbufErrors")
        for name, v in sorted(cur["disks"].items()):
            p = prev["disks"].get(name)
            if p is None:
                continue
            out["disks"].append({
                "name": name,
                "read_iops": round(per_s(v[0], p[0]), 1),
                "read_Bps": per_s(v[1], p[1]) * 512,
                "write_iops": round(per_s(v[2], p[2]), 1),
                "write_Bps": per_s(v[3], p[3]) * 512,
                "busy_pct": round(min(100.0, per_s(v[4], p[4]) / 10), 1),
            })
        if out["udp_socket"] is not None and prev["sock"] is not None:
            out["udp_socket"]["drops_per_s"] = round(
                per_s(cur["sock"][2], prev["sock"][2]), 1)
        out["proc_cpu"] = round(
            100 * (cur["self"][0] - prev["self"][0]) / self._hz / dt, 1)
        out["proc"]["user_pct"] = round(
            100 * (cur["times"][0] - prev["times"][0]) / self._hz / dt, 1)
        out["proc"]["system_pct"] = round(
            100 * (cur["times"][1] - prev["times"][1]) / self._hz / dt, 1)
        x0, x1 = prev["ctx"], cur["ctx"]
        for key, name in (("ctx_vol_per_s", "voluntary_ctxt_switches"),
                          ("ctx_invol_per_s", "nonvoluntary_ctxt_switches")):
            if name in x0 and name in x1:
                out["proc"][key] = round(per_s(x1[name], x0[name]), 1)
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
        self.length = length
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


def display_addr(addr) -> str | None:
    """A peer's IP address without the port: an IPv4-mapped IPv6 address
    as IPv4, no zone. Takes a host or (host, port). None when unknown or
    unparseable."""
    host = addr[0] if isinstance(addr, (tuple, list)) else addr
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(str(host).split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


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
