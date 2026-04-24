#!/usr/bin/env python3
"""aiomoqt release regression bank.

Runs unit, integration, interop, and bench tiers against a relay catalog.
Each suite is a discrete test with its own status line; tiers group them.

Usage:
    cd ~/Projects/moq/aiomoqt

    # CI path: unit + integration (no network)
    tests/release_regression_test.py --test-tier unit --test-tier integration

    # Interop across every relay in catalog, 4 relays in parallel
    tests/release_regression_test.py --test-tier interop --interop-parallel 4

    # Limit to one relay
    tests/release_regression_test.py --only moqx-main
    tests/release_regression_test.py --only cloudflare-d14

    # Adaptive throughput bench (manual dispatch only)
    tests/release_regression_test.py --test-tier bench

Exit 0 iff every test passed. `[skip]` suites don't count.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

_PRINT_LOCK = threading.Lock()


def _progress(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)

DEFAULT_CATALOG = Path(__file__).parent / "relays.json"

TIERS = {
    "unit":        ["buffer", "message", "track"],
    "integration": ["loopback-setup", "loopback-pub-sub",
                    "loopback-join", "loopback-fetch"],
    "interop":     ["relay-ctrl-msg", "relay-pub-sub",
                    "relay-join", "relay-fetch"],
    "bench":       ["loopback-adaptive-bench"],
}
TIER_CHOICES = tuple(TIERS.keys())
SUITE_CHOICES = tuple(s for suites in TIERS.values() for s in suites)

MULTI_SUB_ARGS_COMMON = [
    "-n", "3", "-s", "1024", "-r", "30", "-g", "60", "-t", "30",
]
PUB_MODE_FLAGS = {
    "publish":      [],
    "publish-ns":   ["--pub-ns"],
    "publish-both": ["--pub-both"],
}


def _host(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/]+)", url)
    return m.group(1) if m else url


def _relay_host(relay: dict) -> str:
    """Pick a representative host:port for a relay catalog entry."""
    for transport in ("raw-quic", "h3-wt"):
        if transport in relay.get("urls", {}):
            return _host(relay["urls"][transport])
    return "(no url)"


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


# ---------------------------------------------------------------------------
# Unit-tier runners
# ---------------------------------------------------------------------------
def _pytest_file(test_file: str, log: Path) -> tuple[str, str]:
    ok, _ = _run(["pytest", "-q", test_file], log, 180)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text().strip()
    last = text.splitlines()[-1] if text else ""
    passed = "passed" in last and "failed" not in last
    return ("PASS" if passed else "FAIL"), last or "(no summary)"


def _buffer(log_dir: Path) -> tuple[str, str]:
    log = log_dir / "buffer.log"
    ok, _ = _run(["python", "tests/test_rebuf.py"], log, 60)
    text = log.read_text()
    m = re.search(r"(\d+) passed,\s*(\d+) failed", text)
    passed = ok and m and m.group(2) == "0"
    summary = m.group(0) if m else "(no summary)"
    return ("PASS" if passed else "FAIL"), summary


def _message(log_dir: Path) -> tuple[str, str]:
    return _pytest_file("aiomoqt/tests/test_messages.py",
                        log_dir / "message.log")


def _track(log_dir: Path) -> tuple[str, str]:
    return _pytest_file("aiomoqt/tests/test_track.py",
                        log_dir / "track.log")


# ---------------------------------------------------------------------------
# Integration-tier runners
# ---------------------------------------------------------------------------
def _loopback_setup(log_dir: Path) -> tuple[str, str]:
    return _pytest_file("aiomoqt/tests/test_loopback_setup.py",
                        log_dir / "loopback-setup.log")


def _loopback_pub_sub(log_dir: Path) -> tuple[str, str]:
    log = log_dir / "loopback-pub-sub.log"
    cmd = [
        "python", "-m", "aiomoqt.examples.loopback_bench",
        "-P", "4", "-s", "16384", "-r", "60", "-t", "10",
    ]
    ok, _ = _run(cmd, log, 40)
    text = log.read_text()
    m = re.search(r"Throughput:\s+([\d.]+)\s*Mbps", text)
    no_loss = "Lost:        0 (0.00%)" in text
    tput = m.group(1) if m else "?"
    return ("PASS" if ok and no_loss else "FAIL"), f"{tput} Mbps"


def _loopback_join(log_dir: Path) -> tuple[str, str]:
    return _pytest_file("aiomoqt/tests/test_loopback_join.py",
                        log_dir / "loopback-join.log")


def _loopback_fetch(log_dir: Path) -> tuple[str, str]:
    return _pytest_file("aiomoqt/tests/test_loopback_fetch.py",
                        log_dir / "loopback-fetch.log")


# ---------------------------------------------------------------------------
# Interop-tier runners (per relay × transport × draft)
# ---------------------------------------------------------------------------
def _relay_ctrl_msg(url: str, draft: int, insecure: bool,
                    log: Path) -> tuple[str, str]:
    cmd = ["python", "-m", "aiomoqt.examples.moq_interop_client",
           "-r", url, "--draft", str(draft)]
    if insecure:
        cmd.append("--tls-disable-verify")
    ok, _ = _run(cmd, log, 90)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text()
    ok_count = len(re.findall(r"^ok \d", text, re.MULTILINE))
    return ("PASS" if ok_count == 6 else "FAIL"), f"{ok_count}/6"


def _relay_pub_sub(url: str, draft: int, pub_mode: str, insecure: bool,
                   log: Path, trackname: str) -> tuple[str, str]:
    cmd = ["python", "-m", "aiomoqt.examples.multi_sub_bench",
           url, *MULTI_SUB_ARGS_COMMON, "--draft", str(draft),
           "--trackname", trackname, *PUB_MODE_FLAGS[pub_mode]]
    if insecure:
        cmd.append("-k")
    ok, _ = _run(cmd, log, 120)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text()
    m = re.search(r"Subscribers:\s+(\d+)/(\d+)\s+ok", text)
    if not m:
        return "FAIL", "(no summary)"
    got, want = m.group(1), m.group(2)
    return ("PASS" if got == want else "FAIL"), f"{got}/{want} ok"


def _relay_tap_case(url: str, draft: int, case: str, insecure: bool,
                    log: Path) -> tuple[str, str]:
    cmd = ["python", "-m", "aiomoqt.examples.moq_interop_client",
           "-r", url, "--draft", str(draft), "-t", case]
    if insecure:
        cmd.append("--tls-disable-verify")
    ok, _ = _run(cmd, log, 90)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text()
    # TAP line for a single-case run: "ok 1 - <case>" or "not ok 1 - ..."
    if re.search(rf"^ok 1 - {re.escape(case)}", text, re.MULTILINE):
        return "PASS", "ok"
    last = text.splitlines()[-1] if text else "no output"
    return "FAIL", last


def _relay_join(url: str, draft: int, insecure: bool,
                log: Path) -> tuple[str, str]:
    return _relay_tap_case(url, draft, "join", insecure, log)


def _relay_fetch(url: str, draft: int, insecure: bool,
                 log: Path) -> tuple[str, str]:
    return _relay_tap_case(url, draft, "fetch", insecure, log)


# ---------------------------------------------------------------------------
# Bench-tier runners
# ---------------------------------------------------------------------------
def _loopback_adaptive_bench(log_dir: Path) -> tuple[str, str]:
    log = log_dir / "loopback-adaptive-bench.log"
    # Loopback self-test: short ramp, kill after ~30s so the runner
    # isn't held open by the forever-probing controller.
    cmd = ["timeout", "--signal=INT", "--kill-after=3", "30",
           "python", "-m", "aiomoqt.examples.adaptive_bench",
           "--start-mbps", "10", "--step-mbps", "10",
           "--max-mbps", "500", "--interval", "3",
           "-l", "100"]
    _run(cmd, log, 45)
    text = log.read_text()
    m = re.search(r"High-water:\s+([\d.]+)\s+(\S+)", text)
    if m:
        return "PASS", f"high-water {m.group(1)} {m.group(2)}"
    return "FAIL", "(no summary — check log)"


# ---------------------------------------------------------------------------
# Record / print helpers
# ---------------------------------------------------------------------------
# Result tuple: (status, test_label, detail, log_path)
#   status ∈ {"PASS", "FAIL", "SKIP"}
Result = tuple[str, str, str, Path]


def _marker(status: str) -> str:
    return {"PASS": "[✓]  ", "FAIL": "[✗]  ", "SKIP": "[skip]"}[status]


def _print_result(res: Result) -> None:
    status, test, detail, _ = res
    print(f"  {_marker(status)} {test:<34} {detail}")


# ---------------------------------------------------------------------------
# Per-relay matrix runner (one worker of ThreadPoolExecutor)
# ---------------------------------------------------------------------------
def _run_relay_matrix(relay: dict, enabled: set[str],
                      log_dir: Path) -> list[Result]:
    results: list[Result] = []
    disabled = set(relay.get("disabled_suites", []))
    pub_mode = relay.get("pub_mode", "publish")
    insecure = bool(relay.get("insecure", False))
    rname = relay["name"]

    def _dispatch(suite: str, label_suffix: str, tag: str, slug: str,
                  fn, *fn_args) -> None:
        # Order label so the eye can scan relay-then-transport-then-suite
        label = f"{rname:<14} {tag:<14} {suite}{label_suffix}"
        if suite in disabled:
            # Relay-join / relay-fetch are disabled on almost every
            # relay by default — don't print a line per combo, just
            # record the SKIP for the summary. Real relay-specific
            # disables still announce themselves on a single line.
            default_disabled = suite in ("relay-join", "relay-fetch")
            if default_disabled:
                marker = f"(disabled: {suite.split('-')[-1].upper()} "
                marker += "not supported)"
                results.append(("SKIP", label, marker, Path("/dev/null")))
                return
            marker = "(disabled for this relay)"
            results.append(("SKIP", label, marker, Path("/dev/null")))
            _progress(f"  [skip] {label}  {marker}")
            return
        log = log_dir / f"{suite}_{slug}.log"
        status, detail = fn(*fn_args, log)
        results.append((status, label, detail, log))
        marker = "[PASS]" if status == "PASS" else "[FAIL]"
        _progress(f"  {marker} {label}  {detail}")

    for transport, url in relay["urls"].items():
        for draft in relay["drafts"]:
            tag = f"{transport}/d{draft}"
            slug = f"{rname}_{transport}_d{draft}"

            if "relay-ctrl-msg" in enabled:
                _dispatch("relay-ctrl-msg", "", tag, slug,
                          _relay_ctrl_msg, url, draft, insecure)
            if "relay-pub-sub" in enabled:
                tn = f"rr-{rname}-{draft}"
                _dispatch("relay-pub-sub", f"[{pub_mode}]", tag, slug,
                          lambda u, d, log: _relay_pub_sub(
                              u, d, pub_mode, insecure, log, tn),
                          url, draft)
            if "relay-join" in enabled:
                _dispatch("relay-join", "", tag, slug,
                          _relay_join, url, draft, insecure)
            if "relay-fetch" in enabled:
                _dispatch("relay-fetch", "", tag, slug,
                          _relay_fetch, url, draft, insecure)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
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
    ap.add_argument("--interop-parallel", type=int, default=4,
                    metavar="N", help="relays to run concurrently in the "
                                      "interop tier (default: 4)")
    args = ap.parse_args()

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

    if args.only is not None:
        # --only bypasses the disabled flag so you can still probe
        # a disabled relay without editing the catalog.
        relays = [r for r in relays_all if r["name"] == args.only]
        if not relays:
            print(f"error: no relay named {args.only!r} in catalog",
                  file=sys.stderr)
            return 2
    else:
        relays = [r for r in relays_all if not r.get("disabled", False)]

    results: list[Result] = []

    def record_and_print(res: Result) -> None:
        _print_result(res)
        results.append(res)

    # --- unit tier ---
    if enabled & set(TIERS["unit"]):
        print("\n== unit ==")
        if "buffer" in enabled:
            status, detail = _buffer(log_dir)
            record_and_print((status, "buffer", detail,
                              log_dir / "buffer.log"))
        if "message" in enabled:
            status, detail = _message(log_dir)
            record_and_print((status, "message", detail,
                              log_dir / "message.log"))
        if "track" in enabled:
            status, detail = _track(log_dir)
            record_and_print((status, "track", detail,
                              log_dir / "track.log"))

    # --- integration tier ---
    if enabled & set(TIERS["integration"]):
        print("\n== integration ==")
        if "loopback-setup" in enabled:
            status, detail = _loopback_setup(log_dir)
            record_and_print((status, "loopback-setup", detail,
                              log_dir / "loopback-setup.log"))
        if "loopback-pub-sub" in enabled:
            status, detail = _loopback_pub_sub(log_dir)
            record_and_print((status, "loopback-pub-sub", detail,
                              log_dir / "loopback-pub-sub.log"))
        if "loopback-join" in enabled:
            status, detail = _loopback_join(log_dir)
            record_and_print((status, "loopback-join", detail,
                              log_dir / "loopback-join.log"))
        if "loopback-fetch" in enabled:
            status, detail = _loopback_fetch(log_dir)
            record_and_print((status, "loopback-fetch", detail,
                              log_dir / "loopback-fetch.log"))

    # --- interop tier (parallel per relay) ---
    # Per-suite progress prints live from worker threads via _progress().
    # Catalog-order rollup lives in the summary block at end.
    if enabled & set(TIERS["interop"]) and relays:
        print("\n== interop ==")
        for relay in relays:
            print(f"  relay: {relay['name']} ({_relay_host(relay)})")
        print(f"  workers: {args.interop_parallel}")
        print()
        workers = max(1, min(args.interop_parallel, len(relays)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_run_relay_matrix, relay, enabled, log_dir): relay
                for relay in relays
            }
            relay_results: dict[str, list[Result]] = {}
            for fut in concurrent.futures.as_completed(futures):
                relay = futures[fut]
                try:
                    relay_results[relay["name"]] = fut.result()
                except Exception as e:
                    relay_results[relay["name"]] = [
                        ("FAIL", f"relay-matrix {relay['name']}",
                         f"exception: {e}", Path("/dev/null"))
                    ]

        # Collect results in catalog order for the summary block
        for relay in relays:
            for res in relay_results.get(relay["name"], []):
                results.append(res)

    # --- bench tier ---
    if enabled & set(TIERS["bench"]):
        print("\n== bench ==")
        if "loopback-adaptive-bench" in enabled:
            status, detail = _loopback_adaptive_bench(log_dir)
            record_and_print((status, "loopback-adaptive-bench", detail,
                              log_dir / "loopback-adaptive-bench.log"))

    # --- summary ---
    print("\n" + "═" * 72)
    fails = [r for r in results if r[0] == "FAIL"]
    skips = [r for r in results if r[0] == "SKIP"]
    passes = [r for r in results if r[0] == "PASS"]
    # Hide default-disabled relay-join/relay-fetch SKIPs from the
    # summary block — they are a permanent property of every active
    # relay in the catalog and only add noise. The SKIP count still
    # reflects them.
    for res in results:
        if res[0] == "SKIP" and (
                "JOIN not supported" in res[2]
                or "FETCH not supported" in res[2]):
            continue
        print(f"  {res[0]:<4}  {res[1]:<50} {res[2]}")
    print("═" * 72)
    print(f"  Logs: {log_dir}")
    print(f"  {len(passes)} passed, {len(fails)} failed, {len(skips)} skipped")

    # Markdown summary for GitHub Actions runners.
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        _write_gh_summary(Path(gh_summary), results, log_dir)

    if fails:
        print(f"  {len(fails)} FAILED — tails follow")
        for _, test, _, log in fails:
            if log.exists() and log != Path("/dev/null"):
                print(f"\n--- {test} ({log.name}) ---")
                tail = log.read_text().splitlines()[-40:]
                for line in tail:
                    print(line)
        return 1
    print("  all green")
    return 0


def _write_gh_summary(summary_path: Path, results: list[Result],
                      log_dir: Path) -> None:
    """Append a markdown table + totals to the GitHub Actions run summary."""
    emoji = {"PASS": "✅", "FAIL": "❌", "SKIP": "⚪"}
    lines = [
        "## aiomoqt regression",
        "",
        "| Status | Suite | Detail |",
        "|---|---|---|",
    ]
    for status, test, detail, _ in results:
        detail_safe = detail.replace("|", "\\|")
        lines.append(f"| {emoji[status]} {status} | `{test}` | {detail_safe} |")
    fails = sum(1 for r in results if r[0] == "FAIL")
    skips = sum(1 for r in results if r[0] == "SKIP")
    passes = sum(1 for r in results if r[0] == "PASS")
    lines.extend([
        "",
        f"**{passes} passed · {fails} failed · {skips} skipped**",
        "",
        f"Logs: `{log_dir}`",
        "",
    ])
    with summary_path.open("a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
