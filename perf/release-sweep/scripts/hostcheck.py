#!/usr/bin/env python3
"""hostcheck — measurement-host state probe for the MoQT release sweep.

Records everything PROGRAM.md §5.3 requires us to verify rather than assume,
and evaluates the §6.6 abort rules. Stdlib only and single-file on purpose:
`sweep.py` streams this file over ssh and runs it under the load host's stock
python3 (3.12) as well as the arm interpreter (3.14).

Usage:
  hostcheck.py [--record PATH] [--baseline PATH] [--require]
               [--iface IFACE] [--path DIR] [--ntp SERVER[,SERVER...]]
               [--peer HOST:PORT] [--guard-llama] [--llama-url URL]
               [--json] [--quiet]
  hostcheck.py serve-time --port 9876          # TCP clock peer (see --peer)

Exit status:
  0  probe completed (even if abort rules fired, unless --require)
  1  --require and at least one abort rule fired
  2  probe could not run at all
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

NTP_EPOCH_DELTA = 2208988800  # 1900-01-01 -> 1970-01-01
DEFAULT_NTP = "time.cloudflare.com,pool.ntp.org,time.google.com"
BENCH_PATTERNS = (
    "aiomoqt.tools.sub_bench",
    "aiomoqt.tools.pub_bench",
    "aiomoqt.tools.pub_server",
    "aiomoqt.tools.loopback_bench",
    "aiomoqt.tools.load_sim",
    "moqx-perf",
)

SYSCTLS = {
    "net.core.rmem_max": "/proc/sys/net/core/rmem_max",
    "net.core.wmem_max": "/proc/sys/net/core/wmem_max",
    "net.core.rmem_default": "/proc/sys/net/core/rmem_default",
    "net.core.wmem_default": "/proc/sys/net/core/wmem_default",
    "net.core.udp_mem": "/proc/sys/net/ipv4/udp_mem",
    "net.core.netdev_max_backlog": "/proc/sys/net/core/netdev_max_backlog",
    "net.core.netdev_budget": "/proc/sys/net/core/netdev_budget",
    "net.core.netdev_budget_usecs": "/proc/sys/net/core/netdev_budget_usecs",
    "net.core.optmem_max": "/proc/sys/net/core/optmem_max",
    "fs.nr_open": "/proc/sys/fs/nr_open",
}

MIN_FREE_GIB = 10.0
MAX_STEAL_PCT = 2.0
MAX_CLOCK_JUMP_US = 2000.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def run(argv, timeout=5.0):
    """Run argv, return stdout with errors folded in (never raise)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001 - probe never raises
        return f"<unavailable: {exc.__class__.__name__}>"


def read_text(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def default_iface():
    """Interface carrying the default route (falls back to the first up NIC)."""
    out = run(["ip", "-o", "route", "get", "1.1.1.1"])
    m = re.search(r"\bdev\s+(\S+)", out)
    if m:
        return m.group(1)
    for name in sorted(os.listdir("/sys/class/net")):
        if name == "lo":
            continue
        if read_text(f"/sys/class/net/{name}/operstate") == "up":
            return name
    return "lo"


def qdisc_state(iface):
    out = run(["tc", "qdisc", "show", "dev", iface])
    if out.startswith("<unavailable"):
        return {"status": "unavailable", "detail": out}
    root = None
    for line in out.splitlines():
        m = re.search(r"qdisc\s+(\S+)", line)
        if m:
            root = m.group(1)
            break
    return {"status": "ok", "root": root, "detail": out.strip()}


def txqueuelen(iface):
    out = run(["ip", "-o", "link", "show", "dev", iface])
    m = re.search(r"qlen\s+(\d+)", out)
    return int(m.group(1)) if m else None


def nic_rings(iface):
    out = run(["ethtool", "-g", iface])
    if out.startswith("<unavailable") or "Cannot get device ring settings" in out:
        return {"status": "unavailable"}
    cur = {}
    section = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Current hardware settings"):
            section = "current"
            continue
        if line.startswith("Pre-set maximums"):
            section = "max"
            continue
        m = re.match(r"(RX|TX):\s+(\d+)", line)
        if m and section:
            cur.setdefault(section, {})[m.group(1)] = int(m.group(2))
    return {"status": "ok", **cur} if cur else {"status": "unavailable"}


def rps_state(iface):
    state = {}
    for path in sorted(glob.glob(f"/sys/class/net/{iface}/queues/rx-*/rps_cpus")):
        state[path.rsplit("/", 2)[-2]] = read_text(path)
    flow = read_text(f"/sys/class/net/{iface}/queues/rx-0/rps_flow_cnt")
    return {"queues": state, "rps_flow_cnt": flow, "enabled": any(
        (v or "0").strip("0,") for v in state.values())}


def sysctl_state():
    out = {}
    for name, path in SYSCTLS.items():
        val = read_text(path)
        if val is None:
            out[name] = None
            continue
        parts = val.split()
        try:
            out[name] = int(parts[0]) if len(parts) == 1 else [int(p) for p in parts]
        except ValueError:
            out[name] = val
    nofile = run(["sh", "-c", "ulimit -n"])
    try:
        out["rlimit_nofile"] = int(nofile.strip())
    except ValueError:
        out["rlimit_nofile"] = nofile.strip() or None
    return out


def governor_state():
    vals = set()
    for path in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
        v = read_text(path)
        if v:
            vals.add(v)
    return sorted(vals) if vals else ["unavailable"]


def disk_state(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return {"path": path, "error": str(exc)}
    return {"path": path, "free_gib": round(usage.free / 2**30, 2),
            "total_gib": round(usage.total / 2**30, 2)}


def cpu_counters():
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("cpu "):
                parts = [int(x) for x in line.split()[1:]]
                # user nice system idle iowait irq softirq steal guest guest_nice
                idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
                steal = parts[7] if len(parts) > 7 else 0
                total = sum(parts[:8])
                return total, idle, steal
    return 0, 0, 0


def steal_pct(window=1.0):
    t0, _, s0 = cpu_counters()
    time.sleep(window)
    t1, _, s1 = cpu_counters()
    dt = t1 - t0
    return round(100.0 * (s1 - s0) / dt, 3) if dt > 0 else None


def link_state(iface):
    speed = read_text(f"/sys/class/net/{iface}/speed")
    carrier = read_text(f"/sys/class/net/{iface}/carrier")
    stats = run(["ip", "-s", "link", "show", "dev", iface])
    rx_bytes = tx_bytes = None
    m = re.search(r"RX:.*bytes\s+(\d+)", stats, re.S)
    if m:
        rx_bytes = int(m.group(1))
    m = re.search(r"TX:.*bytes\s+(\d+)", stats, re.S)
    if m:
        tx_bytes = int(m.group(1))
    return {"iface": iface, "speed_mbps": int(speed) if speed and speed.isdigit() else None,
            "carrier": carrier, "rx_bytes": rx_bytes, "tx_bytes": tx_bytes}


def bench_processes():
    out = run(["ps", "-eo", "pid=,etimes=,args="])
    me = os.getpid()
    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_s, etimes, args = line.split(None, 2)
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == me or "hostcheck" in args or args.startswith("ps "):
            continue
        if any(pat in args for pat in BENCH_PATTERNS):
            found.append({"pid": pid, "etimes": int(etimes), "args": args[:200]})
    return found


def llama_state(url):
    state = {"url": url}
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/slots", timeout=2.0) as resp:
            slots = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - absent server == idle
        state.update(reachable=False, detail=exc.__class__.__name__, busy=False, slots=0)
        return state
    busy = [s.get("id") for s in slots if s.get("is_processing")]
    state.update(reachable=True, slots=len(slots), busy=bool(busy), busy_slots=busy)
    return state


# --------------------------------------------------------------------------
# clock
# --------------------------------------------------------------------------
def _ntp_to_unix(sec, frac):
    return (sec - NTP_EPOCH_DELTA) + frac / 2**32


def ntp_sample(server, timeout=1.0):
    """One SNTP exchange -> (offset_s, rtt_s). Raises on failure."""
    pkt = b"\x1b" + 47 * b"\x00"
    infos = socket.getaddrinfo(server, 123, socket.AF_INET, socket.SOCK_DGRAM)
    addr = infos[0][4]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t1 = time.time()
        sock.sendto(pkt, addr)
        data, _ = sock.recvfrom(1024)
        t4 = time.time()
    if len(data) < 48:
        raise ValueError("short NTP reply")
    sec2, frac2 = struct.unpack("!II", data[32:40])
    sec3, frac3 = struct.unpack("!II", data[40:48])
    t2 = _ntp_to_unix(sec2, frac2)
    t3 = _ntp_to_unix(sec3, frac3)
    return ((t2 - t1) + (t3 - t4)) / 2.0, (t4 - t1) - (t3 - t2)


def clock_ntp(servers, samples=20):
    results, errors = [], []
    for server in servers:
        for _ in range(samples):
            try:
                results.append(ntp_sample(server) + (server,))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{server}:{exc.__class__.__name__}")
        if len(results) >= samples:
            break
    if not results:
        return {"method": "ntp", "available": False, "errors": errors[:5]}
    min_rtt = min(r[1] for r in results)
    keep = [r for r in results if r[1] <= max(min_rtt * 1.5, min_rtt + 1e-4)]
    offset = sum(r[0] for r in keep) / len(keep)
    return {"method": "ntp", "available": True,
            "server": results[0][2], "samples": len(results), "used": len(keep),
            "offset_us": round(offset * 1e6, 1),
            "error_us": round(min_rtt * 1e6 / 2.0, 1),
            "min_rtt_us": round(min_rtt * 1e6, 1)}


def clock_peer(peer, samples=20):
    host, _, port = peer.rpartition(":")
    results, errors = [], []
    for _ in range(samples):
        try:
            with socket.create_connection((host, int(port)), timeout=2.0) as sock:
                t1 = time.time()
                sock.sendall(struct.pack("!d", t1))
                buf = b""
                while len(buf) < 16:
                    chunk = sock.recv(16 - len(buf))
                    if not chunk:
                        raise ConnectionError("peer closed")
                    buf += chunk
                t4 = time.time()
            t2, t3 = struct.unpack("!dd", buf)
            results.append((((t2 - t1) + (t3 - t4)) / 2.0, (t4 - t1) - (t3 - t2)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc.__class__.__name__)
    if not results:
        return {"method": "tcp-peer", "peer": peer, "available": False,
                "errors": errors[:5]}
    min_rtt = min(r[1] for r in results)
    keep = [r for r in results if r[1] <= max(min_rtt * 1.5, min_rtt + 1e-4)]
    offset = sum(r[0] for r in keep) / len(keep)
    return {"method": "tcp-peer", "peer": peer, "available": True,
            "samples": len(results), "used": len(keep),
            "offset_us": round(offset * 1e6, 1),
            "error_us": round(min_rtt * 1e6 / 2.0, 1),
            "min_rtt_us": round(min_rtt * 1e6, 1)}


def serve_time(port, host="0.0.0.0"):
    """Answer TCP clock probes with (t2, t3) timestamps, 16 bytes each."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    while True:
        conn, _ = srv.accept()
        with conn:
            t2 = time.time()
            try:
                conn.recv(8)
            except OSError:
                continue
            t3 = time.time()
            conn.sendall(struct.pack("!dd", t2, t3))


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------
def evaluate(state, baseline, guard_llama):
    """Return the list of PROGRAM §6.6 abort conditions that fired."""
    abort = []

    if guard_llama and state["llama"].get("reachable"):
        if state["llama"].get("busy"):
            abort.append({"rule": "llama_slot_busy",
                          "detail": f"slots processing: {state['llama'].get('busy_slots')}"})

    if state.get("steal_pct") is not None and state["steal_pct"] > MAX_STEAL_PCT:
        abort.append({"rule": "cpu_steal",
                      "detail": f"steal {state['steal_pct']:.2f}% > {MAX_STEAL_PCT}%"})

    disk = state["disk"].get("free_gib")
    if disk is not None and disk < MIN_FREE_GIB:
        abort.append({"rule": "disk_low", "detail": f"{disk:.1f} GiB free"})

    if state["bench_procs"]:
        abort.append({"rule": "stray_bench_process",
                      "detail": f"{len(state['bench_procs'])} process(es) already running"})

    if baseline:
        bq = (baseline.get("qdisc") or {})
        cq = state["qdisc"]
        if bq.get("status") == "ok" and cq.get("status") == "ok":
            if bq.get("root") != cq.get("root"):
                abort.append({"rule": "qdisc_changed",
                              "detail": f"{bq.get('root')} -> {cq.get('root')}"})
        elif cq.get("status") == "ok" and bq.get("status") == "unavailable":
            pass  # host gained tc visibility; recorded only
        if baseline.get("txqueuelen") != state["txqueuelen"]:
            abort.append({"rule": "txqueuelen_changed",
                          "detail": f"{baseline.get('txqueuelen')} -> {state['txqueuelen']}"})
        bspeed = (baseline.get("link") or {}).get("speed_mbps")
        cspeed = (state.get("link") or {}).get("speed_mbps")
        if bspeed and cspeed and bspeed != cspeed:
            abort.append({"rule": "link_renegotiated",
                          "detail": f"{bspeed} -> {cspeed} Mbps"})
        boff = (baseline.get("clock") or {}).get("offset_us")
        coff = (state.get("clock") or {}).get("offset_us")
        if boff is not None and coff is not None:
            if abs(coff - boff) > MAX_CLOCK_JUMP_US:
                abort.append({"rule": "clock_offset_jump",
                              "detail": f"{boff:.0f} -> {coff:.0f} us "
                                        f"(>{MAX_CLOCK_JUMP_US:.0f} us)"})
    return abort


def collect(args):
    iface = args.iface or default_iface()
    state = {
        "ok": True,
        "host": socket.gethostname(),
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "uname": os.uname().release,
        "nproc": os.cpu_count(),
        "iface": iface,
        "qdisc": qdisc_state(iface),
        "txqueuelen": txqueuelen(iface),
        "rings": nic_rings(iface),
        "rps": rps_state(iface),
        "sysctls": sysctl_state(),
        "governor": governor_state(),
        "disk": disk_state(args.path or os.getcwd()),
        "link": link_state(iface),
        "bench_procs": bench_processes(),
        "llama": llama_state(args.llama_url) if args.guard_llama
                 else {"checked": False},
        "steal_pct": steal_pct(),
    }
    if args.peer:
        state["clock"] = clock_peer(args.peer, samples=args.samples)
    else:
        state["clock"] = clock_ntp(args.ntp.split(","), samples=args.samples)

    baseline = None
    if args.baseline:
        with open(args.baseline) as fh:
            baseline = json.load(fh)
    state["abort"] = evaluate(state, baseline, args.guard_llama)
    state["baseline"] = args.baseline or None
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record", metavar="PATH", help="write the probe as JSON")
    parser.add_argument("--baseline", metavar="PATH",
                        help="previous --record JSON to compare against")
    parser.add_argument("--require", action="store_true",
                        help="exit 1 if any §6.6 abort condition fired")
    parser.add_argument("--iface", help="measured interface (default: route to 1.1.1.1)")
    parser.add_argument("--path", help="filesystem to check free space on")
    parser.add_argument("--ntp", default=DEFAULT_NTP, help="NTP servers, comma separated")
    parser.add_argument("--peer", help="HOST:PORT TCP clock peer instead of NTP")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--guard-llama", action="store_true",
                        help="abort when seawolf's llama.cpp has a processing slot")
    parser.add_argument("--llama-url", default="http://127.0.0.1:8080")
    parser.add_argument("--json", action="store_true", help="print JSON only")
    parser.add_argument("--quiet", action="store_true", help="print nothing but errors")
    sub = parser.add_subparsers(dest="cmd")
    st = sub.add_parser("serve-time", help="run the TCP clock peer")
    st.add_argument("--port", type=int, default=9876)
    st.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args(argv)

    if args.cmd == "serve-time":
        serve_time(args.port, args.host)
        return 0

    try:
        state = collect(args)
    except Exception as exc:  # noqa: BLE001
        print(f"hostcheck failed: {exc}", file=sys.stderr)
        return 2

    if args.record:
        tmp = f"{args.record}.tmp"
        os.makedirs(os.path.dirname(args.record) or ".", exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, args.record)

    if args.json:
        json.dump(state, sys.stdout, indent=2)
        print()
    elif not args.quiet:
        print(f"hostcheck {state['host']}  {state['ts_utc']}")
        print(f"  iface={state['iface']} qdisc={state['qdisc'].get('root') or state['qdisc']['status']}"
              f" txqueuelen={state['txqueuelen']} rings={state['rings']['status']}"
              f" rps={state['rps']['enabled']} governor={'/'.join(state['governor'])}")
        disk = state["disk"].get("free_gib")
        print(f"  disk={disk} GiB free  steal={state['steal_pct']}%"
              f"  link={state['link'].get('speed_mbps')} Mbps")
        clock = state["clock"]
        if clock.get("available"):
            print(f"  clock: offset={clock['offset_us']} us"
                  f" ±{clock['error_us']} us ({clock['method']}, {clock.get('samples')} samples)")
        else:
            print(f"  clock: unavailable ({clock.get('errors')})")
        if state["llama"].get("checked"):
            print(f"  llama: reachable={state['llama']['reachable']}"
                  f" busy={state['llama'].get('busy')}")
        print(f"  stray bench procs: {len(state['bench_procs'])}")
        for proc in state["bench_procs"]:
            print(f"    pid={proc['pid']} {proc['args']}")
        for item in state["abort"]:
            print(f"  ABORT {item['rule']}: {item['detail']}")

    if args.require and state["abort"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
