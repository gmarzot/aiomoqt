#!/usr/bin/env python3
"""aiomoqt release regression bank.

Runs unit tests plus a live test matrix against every relay in
tests/relays.py (each relay × supported transport × supported draft).

Usage:
    cd ~/Projects/moq/aiomoqt
    tests/release-regression.py

    # Limit to one relay:
    tests/release-regression.py --only moqx-main
    tests/release-regression.py --only cloudflare-d14

Exit 0 iff every test passed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_CATALOG = Path(__file__).parent / "relays.json"

# Tiers group suites by scope. The CLI accepts either a tier name
# (runs every suite in that tier) or a specific suite name.
#   unit        — pure-Python unit tests, no network
#   integration — local pub/sub over QUIC (loopback, no relay)
#   interop     — external relay matrix (relay-smoke + multi-sub per
#                 catalog entry × transport × draft)
TIERS = {
    "unit":        ["pytest", "test_rebuf"],
    "integration": ["loopback"],
    "interop":     ["relay-smoke", "multi-sub"],
}
TIER_CHOICES = tuple(TIERS.keys())
SUITE_CHOICES = tuple(s for suites in TIERS.values() for s in suites)

MULTI_SUB_ARGS_COMMON = [
    "-n", "3", "-s", "1024", "-r", "30", "-g", "60", "-t", "30", "-k",
]
PUB_MODE_FLAGS = {
    "publish":      [],                 # sends only PUBLISH
    "publish-ns":   ["--pub-ns"],       # sends only PUB_NS
    "publish-both": ["--pub-both"],     # sends both
}


def _host(url: str) -> str:
    # Extract host:port part for compact labels.
    m = re.match(r"^[a-z]+://([^/]+)", url)
    return m.group(1) if m else url


def _run(cmd: list[str], log: Path, timeout: int) -> tuple[bool, str]:
    """Run a command, capture to log, return (ok, tail)."""
    try:
        with log.open("w") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    tail = log.read_text().splitlines()[-6:]
    return True, "\n".join(tail)


def _pytest(log_dir: Path) -> tuple[str, str]:
    log = log_dir / "pytest.log"
    ok, _ = _run(["pytest", "-q"], log, 180)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text().strip()
    last = text.splitlines()[-1] if text else ""
    passed = "passed" in last and "failed" not in last
    return ("PASS" if passed else "FAIL"), last


def _rebuf(log_dir: Path) -> tuple[str, str]:
    log = log_dir / "rebuf.log"
    ok, _ = _run(["python", "tests/test_rebuf.py"], log, 60)
    text = log.read_text()
    m = re.search(r"(\d+) passed,\s*(\d+) failed", text)
    passed = ok and m and m.group(2) == "0"
    summary = m.group(0) if m else "(no summary)"
    return ("PASS" if passed else "FAIL"), summary


def _loopback(log_dir: Path) -> tuple[str, str]:
    log = log_dir / "loopback.log"
    cmd = [
        "python", "-m", "aiomoqt.examples.loopback_bench",
        "-P", "4", "-s", "16384", "-r", "60", "-t", "10",
    ]
    ok, _ = _run(cmd, log, 40)
    text = log.read_text()
    m = re.search(r"Throughput:\s+([\d.]+)\s*Mbps", text)
    no_loss = "Lost:" in text and "Lost:" in text and "Lost:        0 (0.00%)" in text
    tput = m.group(1) if m else "?"
    return ("PASS" if ok and no_loss else "FAIL"), f"{tput} Mbps"


def _smoke(url: str, draft: int, log: Path) -> tuple[str, str]:
    cmd = ["python", "-m", "aiomoqt.examples.moq_interop_client",
           "-r", url, "--draft", str(draft)]
    ok, _ = _run(cmd, log, 90)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text()
    ok_count = len(re.findall(r"^ok \d", text, re.MULTILINE))
    return ("PASS" if ok_count == 6 else "FAIL"), f"{ok_count}/6"


def _multi_sub(url: str, draft: int, pub_mode: str, log: Path,
               trackname: str) -> tuple[str, str]:
    cmd = ["python", "-m", "aiomoqt.examples.multi_sub_bench",
           url, *MULTI_SUB_ARGS_COMMON, "--draft", str(draft),
           "--trackname", trackname, *PUB_MODE_FLAGS[pub_mode]]
    ok, _ = _run(cmd, log, 120)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text()
    m = re.search(r"Subscribers:\s+(\d+)/(\d+)\s+ok", text)
    if not m:
        return "FAIL", "(no summary)"
    got, want = m.group(1), m.group(2)
    return ("PASS" if got == want else "FAIL"), f"{got}/{want} ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-tier", action="append", default=[],
                    choices=TIER_CHOICES, metavar="TIER",
                    dest="test_tiers",
                    help=f"run every suite in this tier (repeatable). "
                         f"Choices: {', '.join(TIER_CHOICES)}")
    ap.add_argument("--test-suite", action="append", default=[],
                    choices=SUITE_CHOICES, metavar="SUITE",
                    dest="test_suites",
                    help=f"run this specific suite (repeatable). "
                         f"Choices: {', '.join(SUITE_CHOICES)}")
    ap.add_argument("--only", metavar="RELAY",
                    help="within the interop tier, only test this relay "
                         "by name")
    ap.add_argument("--catalog", default=str(DEFAULT_CATALOG),
                    help=f"relay catalog JSON (default: {DEFAULT_CATALOG})")
    ap.add_argument("--log-dir", default=None,
                    help="directory for per-test logs (default: tmp)")
    args = ap.parse_args()

    # Resolve which suites to run.
    if args.test_tiers or args.test_suites:
        enabled: set[str] = set(args.test_suites)
        for t in args.test_tiers:
            enabled.update(TIERS[t])
    else:
        enabled = set(SUITE_CHOICES)

    with open(args.catalog) as f:
        catalog = json.load(f)
    relays_all = catalog.get("relays", [])

    log_dir = Path(args.log_dir) if args.log_dir else Path(
        tempfile.mkdtemp(prefix="aiomoqt-regression."))
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Logs: {log_dir}")

    relays = [r for r in relays_all
              if args.only is None or r["name"] == args.only]
    if args.only and not relays:
        print(f"error: no relay named {args.only!r} in catalog",
              file=sys.stderr)
        return 2

    results: list[tuple[str, str, str]] = []   # (status, test, detail)

    def record(status, test, detail):
        marker = "✓" if status == "PASS" else "✗"
        print(f"  [{marker}] {test:<40} {detail}")
        results.append((status, test, detail))

    # --- unit tier ---
    if enabled & set(TIERS["unit"]):
        print("\n== unit tier ==")
        if "pytest" in enabled:
            record(*_pytest(log_dir), "pytest")
        if "test_rebuf" in enabled:
            record(*_rebuf(log_dir), "test_rebuf")

    # --- integration tier ---
    if enabled & set(TIERS["integration"]):
        print("\n== integration tier ==")
        if "loopback" in enabled:
            record(*_loopback(log_dir), "loopback")

    # --- interop tier ---
    if not enabled & set(TIERS["interop"]):
        relays = []
    for relay in relays:
        print(f"\n== relay: {relay['name']} ==")
        if "notes" in relay:
            print(f"   note: {relay['notes']}")
        pub_mode = relay["pub_mode"]
        for transport, url in relay["urls"].items():
            for draft in relay["drafts"]:
                tag = f"{relay['name']}/{transport}/d{draft}"
                slug = tag.replace("/", "_")

                if "relay-smoke" in enabled:
                    log = log_dir / f"relay-smoke-{slug}.log"
                    record(*_smoke(url, draft, log),
                           f"relay-smoke {tag}")

                if "multi-sub" in enabled:
                    tn = f"rr-{relay['name']}-{draft}"
                    log = log_dir / f"multi-sub-{slug}.log"
                    record(*_multi_sub(url, draft, pub_mode, log, tn),
                           f"multi-sub   {tag} [{pub_mode}]")

    # --- summary ---
    print("\n" + "═" * 60)
    fails = [r for r in results if r[0] == "FAIL"]
    for status, test, detail in results:
        marker = "PASS" if status == "PASS" else "FAIL"
        print(f"  {marker}  {test:<42} {detail}")
    print("═" * 60)
    print(f"  Logs: {log_dir}")
    if fails:
        print(f"  {len(fails)} FAILED")
        return 1
    print("  all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
