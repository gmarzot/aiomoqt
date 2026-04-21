#!/usr/bin/env python3
"""aiomoqt adaptive throughput bench.

Ramps rate until buffer growth is observed, reports the last stable
rate. Works against a loopback server (default) or an external relay
(`-r URL`).

Each step runs `loopback_bench` (or equivalent pub+sub against a relay)
for INTERVAL seconds at a fixed rate, parses throughput/p99/loss, then
decides whether to step up or declare ceiling.

Ceiling signals:
  - any object loss in the last interval
  - p99 latency > baseline * 1.5 for 2 consecutive intervals
  - throughput shortfall > 15% below commanded rate

Usage:
  # loopback
  python -m aiomoqt.examples.adaptive_bench --ramp 10,5,10,200

  # relay
  python -m aiomoqt.examples.adaptive_bench -r moqt://host:port -k \\
      --ramp 20,10,15,500
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def _parse_ramp(s: str) -> tuple[float, float, int, float]:
    parts = s.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "--ramp must be START,STEP,INTERVAL,MAX (Mbps, Mbps, s, Mbps)")
    try:
        start = float(parts[0])
        step = float(parts[1])
        interval = int(parts[2])
        cap = float(parts[3])
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"--ramp parse error: {e}")
    if step <= 0 or interval <= 0 or cap <= start:
        raise argparse.ArgumentTypeError(
            "--ramp requires step>0, interval>0, max>start")
    return start, step, interval, cap


def _mbps_to_ops(mbps: float, object_size: int) -> float:
    """Convert megabits/sec to objects/sec."""
    return mbps * 1e6 / (object_size * 8)


def _run_step(cmd: list[str], timeout: int, log: Path) -> dict:
    """Run one ramp step, return parsed metrics."""
    with log.open("w") as f:
        try:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                           timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}

    text = log.read_text()
    out: dict = {"text": text}

    m = re.search(r"Throughput:\s+([\d.]+)\s*Mbps", text)
    if m:
        out["throughput_mbps"] = float(m.group(1))

    # Per-interval p99 pattern: "p99: N.Mms" or "p99: NMs"
    p99s = re.findall(r"p99:\s*([\d.]+)\s*(us|ms|s)", text)
    if p99s:
        vals = []
        for v, unit in p99s:
            v = float(v)
            if unit == "us":
                v /= 1000.0
            elif unit == "s":
                v *= 1000.0
            vals.append(v)
        out["p99_ms"] = max(vals)

    # Loss from summary: "Lost:   N (P%)" or per-interval "N% (M objs)"
    m = re.search(r"Lost:\s+(\d+)\s*\(([\d.]+)%\)", text)
    if m:
        out["lost_objs"] = int(m.group(1))
        out["loss_pct"] = float(m.group(2))
    else:
        out["lost_objs"] = 0
        out["loss_pct"] = 0.0

    return out


def _step_cmd_loopback(rate_ops: float, interval: int, object_size: int,
                       port: int) -> list[str]:
    return ["python", "-m", "aiomoqt.examples.loopback_bench",
            "-s", str(object_size),
            "-r", f"{rate_ops:.2f}",
            "-t", str(interval),
            "-i", str(interval),
            "-p", str(port),
            "-P", "1"]


def _step_cmd_relay(url: str, rate_ops: float, interval: int,
                    object_size: int, draft: int, insecure: bool,
                    trackname: str) -> list[str]:
    cmd = ["python", "-m", "aiomoqt.examples.relay_bench",
           url,
           "-s", str(object_size),
           "-r", f"{rate_ops:.2f}",
           "-t", str(interval),
           "-i", str(interval),
           "--draft", str(draft),
           "--trackname", trackname,
           "-P", "1"]
    if insecure:
        cmd.append("-k")
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(
        description="adaptive throughput bench — ramps until buffer growth")
    ap.add_argument("-r", "--relay", default=None,
                    help="relay URL (omit for loopback mode)")
    ap.add_argument("--ramp", type=_parse_ramp,
                    default=_parse_ramp("10,5,10,200"),
                    metavar="START,STEP,INTERVAL,MAX",
                    help="Mbps start, Mbps step, seconds per step, "
                         "Mbps cap (default: 10,5,10,200)")
    ap.add_argument("-s", "--object-size", type=int, default=4096)
    ap.add_argument("--draft", type=int, default=14,
                    help="MoQT draft number (relay mode only)")
    ap.add_argument("-k", "--insecure", action="store_true",
                    help="skip TLS verify (relay mode)")
    ap.add_argument("--trackname", default="adaptive-bench",
                    help="trackname for relay mode")
    ap.add_argument("-p", "--port", type=int, default=4434,
                    help="loopback local port")
    ap.add_argument("--log-dir", default=None)
    args = ap.parse_args()

    start, step, interval, cap = args.ramp
    mode = f"relay {args.relay}" if args.relay else "loopback"

    log_dir = Path(args.log_dir) if args.log_dir else Path(
        tempfile.mkdtemp(prefix="aiomoqt-adaptive."))
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"  adaptive bench — {mode}")
    print(f"  ramp: {start} → {cap} Mbps, +{step}/step, {interval}s/step")
    print(f"  object size: {args.object_size} B")
    print(f"  logs: {log_dir}")
    print()

    # Step-by-step ramp
    baseline_p99 = None
    consecutive_p99_high = 0
    last_stable = None
    stop_reason = None
    mbps = start

    while mbps <= cap:
        rate_ops = _mbps_to_ops(mbps, args.object_size)
        log = log_dir / f"step-{int(mbps)}.log"

        if args.relay:
            cmd = _step_cmd_relay(args.relay, rate_ops, interval,
                                  args.object_size, args.draft,
                                  args.insecure, args.trackname)
        else:
            cmd = _step_cmd_loopback(rate_ops, interval,
                                     args.object_size, args.port)

        m = _run_step(cmd, timeout=interval + 30, log=log)
        if "error" in m:
            print(f"  [{mbps:>5.1f} Mbps]  step error: {m['error']}")
            stop_reason = f"step-error: {m['error']}"
            break

        tput = m.get("throughput_mbps", 0.0)
        p99 = m.get("p99_ms", None)
        lost = m.get("lost_objs", 0)
        p99_s = f"{p99:.1f}ms" if p99 is not None else "--"
        print(f"  [{mbps:>5.1f} Mbps]  tput={tput:>6.2f} Mbps  "
              f"p99={p99_s:<8}  lost={lost}")

        if lost > 0:
            stop_reason = "loss"
            break

        shortfall = (mbps - tput) / mbps if mbps > 0 else 0
        if tput > 0 and shortfall > 0.15:
            stop_reason = f"throughput shortfall ({shortfall*100:.0f}%)"
            break

        if p99 is not None:
            if baseline_p99 is None:
                baseline_p99 = p99
            elif p99 > baseline_p99 * 1.5:
                consecutive_p99_high += 1
                if consecutive_p99_high >= 2:
                    stop_reason = "p99 latency growth"
                    break
            else:
                consecutive_p99_high = 0

        last_stable = mbps
        mbps += step

    print()
    if stop_reason:
        if last_stable is not None:
            print(f"  Ceiling: {last_stable} Mbps ({stop_reason})")
        else:
            print(f"  Ceiling: <{start} Mbps ({stop_reason})")
    else:
        print(f"  No ceiling up to {cap} Mbps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
