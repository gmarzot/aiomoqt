#!/usr/bin/env python3
"""relay_sampler.py — 1 Hz out-of-band sampler for the moqx perf relay.

The sweep kills and restarts the relay for every repetition, so relay-level
counters (which are per-process) cannot be read once at the end of a point.
This sampler runs for the whole sweep and writes one CSV row per second:

  t_unix            wall clock (UTC epoch seconds, float)
  relay_pid         pid of the live `moqx serve` (blank between reps)
  cpu_s             relay CPU seconds (utime+stime from /proc/<pid>/stat)
  rss_mb            relay VmHWM in MiB
  q_bytes_read      moqx_quicBytesRead_total    (relay ingress, QUIC payload)
  q_bytes_written   moqx_quicBytesWritten_total (relay egress  -> fan-out bytes)
  q_conns           moqx_quicActiveConnections
  q_streams         moqx_quicActiveStreams
  moq_sessions      moqx_moqActiveSessions
  pub_subs          moqx_pubActiveSubscriptions
  sub_streams       moqx_pubActiveSubscriptionStreams
  pkts_sent         moqx_quicPacketsSent_total
  pkts_recv         moqx_quicPacketsReceived_total
  fc_block_stream   moqx_quicStreamFlowControlBlocked_total
  fc_block_conn     moqx_quicConnFlowControlBlocked_total
  nic_tx            host NIC tx_bytes for the relay interface (cross-check)
  nic_rx            host NIC rx_bytes for the relay interface
  http_ok           1 if the admin scrape succeeded this tick

Post-processing (scripts/relay_report.py) differences two timestamps inside a
repetition window, so the CSV keeps raw monotonic counters.

    python3 scripts/relay_sampler.py --out relay-sampler.csv --interval 1
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

METRIC_MAP = {
    "moqx_quicBytesRead_total": "q_bytes_read",
    "moqx_quicBytesWritten_total": "q_bytes_written",
    "moqx_quicActiveConnections": "q_conns",
    "moqx_quicActiveStreams": "q_streams",
    "moqx_moqActiveSessions": "moq_sessions",
    "moqx_pubActiveSubscriptions": "pub_subs",
    "moqx_pubActiveSubscriptionStreams": "sub_streams",
    "moqx_quicPacketsSent_total": "pkts_sent",
    "moqx_quicPacketsReceived_total": "pkts_recv",
    "moqx_quicStreamFlowControlBlocked_total": "fc_block_stream",
    "moqx_quicConnFlowControlBlocked_total": "fc_block_conn",
}
COLS = ["t_unix", "relay_pid", "cpu_s", "rss_mb"] + list(METRIC_MAP.values()) + \
       ["nic_tx", "nic_rx", "http_ok"]
LINE_RE = re.compile(r"^([a-zA-Z_:][\w:]*)(?:\{[^}]*\})?\s+(\S+)\s*$")


def relay_pid(pattern: str) -> int | None:
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return None
    pids = [int(x) for x in out.stdout.split() if x.isdigit()]
    return pids[0] if pids else None


def proc_cpu(pid: int) -> tuple[float | None, float | None]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        hz = 100.0  # USER_HZ, fixed on Linux
        fields = stat.rsplit(")", 1)[1].split()
        utime, stime = int(fields[11]), int(fields[12])
        rss_mb = None
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                rss_mb = round(int(line.split()[1]) / 1024.0, 1)
                break
        return round((utime + stime) / hz, 2), rss_mb
    except (OSError, IndexError, ValueError):
        return None, None


def iface_for(addr: str) -> str | None:
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                             text=True, timeout=5).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[3].split("/")[0] == addr:
            return parts[1].rstrip(":")
    return None


def nic_bytes(iface: str | None) -> tuple[int | None, int | None]:
    if not iface:
        return None, None
    try:
        tx = int(Path(f"/sys/class/net/{iface}/statistics/tx_bytes").read_text())
        rx = int(Path(f"/sys/class/net/{iface}/statistics/rx_bytes").read_text())
        return tx, rx
    except OSError:
        return None, None


def scrape(url: str) -> tuple[dict, bool]:
    try:
        with urllib.request.urlopen(url, timeout=2) as fh:
            text = fh.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - relay down / between reps is normal
        return {}, False
    vals: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name in METRIC_MAP:
            try:
                vals[name] = float(m.group(2))
            except ValueError:
                pass
    return vals, True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default="http://[::1]:9769/metrics")
    ap.add_argument("--addr", default="10.14.25.173", help="relay address, to pick the NIC")
    ap.add_argument("--pattern", default="moqx serve --config")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    iface = iface_for(args.addr)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists()
    with out.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        if new:
            w.writeheader()
        sys.stderr.write(f"relay_sampler: iface={iface} out={out}\n")
        sys.stderr.flush()
        while True:
            tick = time.time()
            pid = relay_pid(args.pattern)
            cpu_s, rss_mb = proc_cpu(pid) if pid else (None, None)
            vals, ok = scrape(args.url)
            tx, rx = nic_bytes(iface)
            row = {"t_unix": round(tick, 3), "relay_pid": pid or "", "cpu_s": cpu_s,
                   "rss_mb": rss_mb, "nic_tx": tx, "nic_rx": rx, "http_ok": int(ok)}
            for src, dst in METRIC_MAP.items():
                row[dst] = vals.get(src, "")
            w.writerow(row)
            fh.flush()
            time.sleep(max(0.05, args.interval - (time.time() - tick)))


if __name__ == "__main__":
    raise SystemExit(main())
