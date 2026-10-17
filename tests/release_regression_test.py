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

    # LOCMAF from Eyevinn's public moqlivemock (manual only)
    tests/release_regression_test.py --test-suite media-locmaf

Exit 0 iff every test passed. `[skip]` suites don't count.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

# Unique per regression run. Track names embed this so every published track is
# distinct across runs AND transports — strict relays (e.g. moqx) reject/mishandle
# a name republished with different content, so we never reuse one.
_RUN_ID = uuid.uuid4().hex[:8]

_PRINT_LOCK = threading.Lock()


def _progress(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)

DEFAULT_CATALOG = Path(__file__).parent / "relays.json"

TIERS = {
    "unit":        ["pytest"],
    "integration": ["loopback-pub-sub",
                    "loopback-pub-sub-tiny",
                    "loopback-pub-sub-streams",
                    "loopback-pub-sub-paced",
                    # draft × transport matrix — catches session-setup /
                    # framing breaks in our own tools across every draft
                    # and transport (the kind that slipped through 0.9.7-9).
                    "loopback-bench-d14-wt", "loopback-bench-d14-quic",
                    "loopback-bench-d16-wt", "loopback-bench-d16-quic",
                    "loopback-bench-d18-wt", "loopback-bench-d18-quic",
                    "loopback-bench-d20-wt", "loopback-bench-d20-quic",
                    "loopback-bench-d22-wt", "loopback-bench-d22-quic",
                    # adaptive BW over the multi-process loopback path
                    # (the in-process loopback misses the pub/sub-worker
                    # start path — see the 0.9.10 BW clobber).
                    "loopback-adaptive-mp-d14", "loopback-adaptive-mp-d16",
                    "loopback-adaptive-mp-d18",
                    "loopback-fetch"],
    "interop":     ["relay-ctrl-msg", "relay-pub-sub", "relay-discovery",
                    "relay-join", "relay-fetch", "relay-data"],
    "bench":       ["loopback-adaptive-bench"],
}
# Run only when named with --test-suite: no tier and no bare run selects them.
MANUAL_SUITES = ["media-locmaf"]
TIER_CHOICES = tuple(TIERS.keys())
SUITE_CHOICES = (tuple(s for suites in TIERS.values() for s in suites)
                 + tuple(MANUAL_SUITES))

# load_sim quick mode: N subscribers on one synthesized track.
MULTI_SUB_ARGS_COMMON = [
    "--subs", "3", "-s", "1024", "-r", "30", "-g", "60", "-t", "30",
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


def _run_rc(cmd: list[str], log: Path, timeout: int) -> int | None:
    """Run a command, capture to log, return its exit code (None on timeout)."""
    try:
        with log.open("w") as f:
            return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                  timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        return None


def _run(cmd: list[str], log: Path, timeout: int) -> tuple[bool, str]:
    """Run a command, capture to log, return (ok, tail)."""
    if _run_rc(cmd, log, timeout) is None:
        return False, "timeout"
    tail = log.read_text().splitlines()[-6:]
    return True, "\n".join(tail)


# ---------------------------------------------------------------------------
# Unit-tier runners
# ---------------------------------------------------------------------------
def _pytest_file(test_file: str, log: Path,
                 extra: list[str] = (), timeout: int = 300) -> tuple[str, str]:
    # Use `python -m pytest` rather than the `pytest` binary so the
    # current working directory is added to sys.path. Without that, an
    # installed copy of aiomoqt under site-packages may shadow the
    # source tree at import time, and tests that compute paths relative
    # to their own __file__ (e.g. CERT_DIR = ../../certs) will resolve
    # to the wheel location which has no neighbouring certs/. Result:
    # tests that should run get silently skipped.
    ok, _ = _run([sys.executable, "-m", "pytest", "-q", test_file, *extra],
                 log, timeout)
    if not ok:
        return "FAIL", "timeout"
    # Search for the pytest summary line anywhere in the captured
    # output, not just the last line. Stray output from atexit /
    # connection-cleanup ("Received a connection close request") often
    # follows the summary on stderr and would otherwise mask a passing
    # run. The line is "N word[, N word ...] in T.TTs" in pytest's own
    # order, e.g. "1 failed, 1063 passed, 9 skipped, 1 xfailed in 18.7s".
    text = log.read_text()
    summary_re = re.compile(r"^\d+ \w+(?:, \d+ \w+)* in [\d.]+s",
                            re.MULTILINE)
    lines = summary_re.findall(text)
    if not lines:
        return "FAIL", "(no pytest summary)"
    summary_line = lines[-1]
    counts = {word: int(n) for n, word in
              re.findall(r"(\d+) (\w+)", summary_line.rsplit(" in ", 1)[0])}
    bad = counts.get("failed", 0) + counts.get("error", 0) \
        + counts.get("errors", 0)
    ok = bad == 0 and counts.get("passed", 0) > 0
    return ("PASS" if ok else "FAIL"), summary_line


# Kept out of the whole-tree run so a platform can skip it: on macOS the
# QUIC close drain stalls ~10s on an un-drained fetch stream.
_PYTEST_STANDALONE = ("aiomoqt/tests/test_loopback_fetch.py",)


def _pytest_all(log_dir: Path) -> tuple[str, str]:
    """Every pytest test in one run. Naming files individually leaves new
    test modules silently unrun; the tool-driven suites below cover only
    what pytest cannot reach."""
    # --durations names the slow tests in the log: the tree runs in ~2s
    # on Linux and ~200s on macOS. Cause not established.
    extra = [f"--ignore={p}" for p in _PYTEST_STANDALONE]
    extra.append("--durations=10")
    return _pytest_file("aiomoqt/tests", log_dir / "pytest.log", extra,
                        timeout=900)


# ---------------------------------------------------------------------------
# Integration-tier runners
# ---------------------------------------------------------------------------


def _loopback_pub_sub(log_dir: Path) -> tuple[str, str]:
    log = log_dir / "loopback-pub-sub.log"
    cmd = [
        sys.executable, "-m", "aiomoqt.tools.loopback_bench",
        "-P", "4", "-s", "16384", "-r", "60", "-t", "10",
    ]
    ok, _ = _run(cmd, log, 40)
    text = log.read_text()
    m = re.search(r"Throughput:\s+([\d.]+)\s*Mbps", text)
    no_loss = "Lost:        0 (0.00%)" in text
    tput = m.group(1) if m else "?"
    return ("PASS" if ok and no_loss else "FAIL"), f"{tput} Mbps"


def _loopback_pub_sub_variant(log_dir: Path, slug: str, flags: list[str],
                                timeout: int) -> tuple[str, str]:
    """Generic runner for loopback_bench variants."""
    log = log_dir / f"{slug}.log"
    cmd = [
        sys.executable, "-m", "aiomoqt.tools.loopback_bench",
        *flags,
    ]
    ok, _ = _run(cmd, log, timeout)
    text = log.read_text()
    m = re.search(r"Throughput:\s+([\d.]+)\s*Mbps", text)
    no_loss = "Lost:        0 (0.00%)" in text
    tput = m.group(1) if m else "?"
    return ("PASS" if ok and no_loss else "FAIL"), f"{tput} Mbps"


def _loopback_pub_sub_tiny(log_dir: Path) -> tuple[str, str]:
    # Tiny objects at high obj/s — stresses per-object Python overhead,
    # parser/framer hot path. Target rate ~4 Mbps.
    return _loopback_pub_sub_variant(
        log_dir, "loopback-pub-sub-tiny",
        ["-P", "1", "-s", "64", "-r", "8000", "-g", "1000", "-t", "5"],
        timeout=30,
    )


def _loopback_pub_sub_streams(log_dir: Path) -> tuple[str, str]:
    # High stream churn: 4 streams × 2000 obj/s with group_size=100
    # means a new stream every ~50 ms (~400 stream lifecycles over the
    # run). Stresses stream-open / per-stream byte ring / cleanup.
    # Target rate ~16 Mbps.
    return _loopback_pub_sub_variant(
        log_dir, "loopback-pub-sub-streams",
        ["-P", "4", "-s", "256", "-r", "2000", "-g", "100", "-t", "5"],
        timeout=30,
    )


def _loopback_pub_sub_paced(log_dir: Path) -> tuple[str, str]:
    # Paced high-BW (not saturating): 4 streams × 800 obj/s × 8 KiB =
    # ~205 Mbps target. Tests flow control + pacer behavior at a real
    # video-class rate without hitting saturation.
    return _loopback_pub_sub_variant(
        log_dir, "loopback-pub-sub-paced",
        ["-P", "4", "-s", "8192", "-r", "800", "-g", "5000", "-t", "8"],
        timeout=40,
    )


def _loopback_fetch(log_dir: Path) -> tuple[str, str]:
    return _pytest_file("aiomoqt/tests/test_loopback_fetch.py",
                        log_dir / "loopback-fetch.log")


def _loopback_bench_combo(log_dir: Path, draft: int,
                          quic: bool) -> tuple[str, str]:
    """loopback_bench smoke for one draft × transport — guards against
    session-setup / framing regressions in our own tool (e.g. the
    raw-QUIC auto-draft connect break fixed in 0.9.9)."""
    transport = "quic" if quic else "wt"
    flags = ["--draft", str(draft), "-P", "1", "-s", "4096",
             "-r", "2000", "-g", "1000", "-t", "5"]
    if quic:
        flags.append("-Q")
    return _loopback_pub_sub_variant(
        log_dir, f"loopback-bench-d{draft}-{transport}", flags, timeout=30)


def _loopback_adaptive_mp(log_dir: Path, draft: int) -> tuple[str, str]:
    """adaptive_bench --mp (BW, separate pub + sub processes) for
    one draft — exercises the multi-process publisher/subscriber start
    path the in-process loopback misses (e.g. the BW start-gate clobber
    fixed in 0.9.10)."""
    slug = f"loopback-adaptive-mp-d{draft}"
    log = log_dir / f"{slug}.log"
    # High-water counts only intervals past warm-up with no shortfall, so
    # -t 20 leaves several chances; one slow sample on a loaded runner must
    # not fail the suite. _run's timeout is the backstop. No external
    # `timeout` binary (absent on macOS runners — it's `gtimeout` there).
    cmd = [sys.executable, "-m", "aiomoqt.tools.adaptive_bench",
           "--mp", "--draft", str(draft),
           "-P", "1", "-s", "4096", "--start-mbps", "20",
           "--step-mbps", "10", "--max-mbps", "60", "--interval", "2",
           "-t", "20", "-k"]
    _run(cmd, log, 45)
    text = log.read_text()
    if "Traceback" in text:
        return "FAIL", "traceback"
    m = re.search(r"High-water:\s+([\d.]+)\s*([KMGT]?bps)", text)
    if m is None:
        return "FAIL", "no High-water summary (crash or timeout)"
    if float(m.group(1)) > 0:
        return "PASS", f"high-water {m.group(1)} {m.group(2)}"
    return "FAIL", "no interval without shortfall (high-water 0)"


# ---------------------------------------------------------------------------
# Interop-tier runners (per relay × transport × draft)
# ---------------------------------------------------------------------------
# The control-plane cases relay-ctrl-msg gates on. The client's standard run
# also carries data-plane cases; relay-data reports those.
CTRL_CASES = frozenset({
    "setup-only", "announce-only", "publish-namespace-done",
    "subscribe-error", "announce-subscribe", "subscribe-before-announce",
})


def _relay_ctrl_msg(url: str, draft: int, insecure: bool,
                    compat: str, log: Path) -> tuple[str, str]:
    cmd = [sys.executable, "-m", "aiomoqt.tools.moq_interop_client",
           "-r", url, "--draft", str(draft)]
    if insecure:
        cmd.append("--tls-disable-verify")
    if compat:
        cmd += ["--compat", compat]
    ok, _ = _run(cmd, log, 90)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text()
    passed = set(re.findall(r"^ok \d+ - (\S+)", text, re.MULTILINE))
    ok_count = len(passed & CTRL_CASES)
    n = len(CTRL_CASES)
    return ("PASS" if ok_count == n else "FAIL"), f"{ok_count}/{n}"


def _relay_pub_sub(url: str, draft: int, pub_mode: str, insecure: bool,
                   compat: str, log: Path,
                   trackname: str) -> tuple[str, str]:
    cmd = [sys.executable, "-m", "aiomoqt.tools.load_sim",
           url, *MULTI_SUB_ARGS_COMMON, "--draft", str(draft),
           "-T", trackname, *PUB_MODE_FLAGS[pub_mode]]
    if insecure:
        cmd.append("-k")
    if compat:
        cmd += ["--compat", compat]
    rc = _run_rc(cmd, log, 120)
    if rc is None:
        return "FAIL", "timeout"
    return _pub_sub_verdict(log.read_text(), rc)


def _pub_sub_verdict(text: str, rc: int = 0) -> tuple[str, str]:
    """Every subscriber joined, objects arrived, and load_sim itself passed
    (RESULT: OK, exit 0). A missing count or RESULT line fails."""
    m = re.search(r"peak\s+(\d+)/(\d+)", text)
    if not m:
        return "FAIL", "(no summary)"
    got, want = m.group(1), m.group(2)
    om = re.search(r"^\s*Total objects: (\d+)\s*$", text, re.MULTILINE)
    if om is None:
        return "FAIL", f"{got}/{want} subscribed, no object count"
    objects = int(om.group(1))
    detail = f"{got}/{want} subscribed, {objects} objects"
    rm = re.search(r"^\s*RESULT: (\w+)\s*$", text, re.MULTILINE)
    if rm is None:
        return "FAIL", f"{detail}, no RESULT line"
    if rm.group(1) != "OK":
        return "FAIL", f"{detail}, RESULT: {rm.group(1)}"
    if rc != 0:
        return "FAIL", f"{detail}, exit {rc}"
    status = "PASS" if got == want and objects > 0 else "FAIL"
    return status, detail


def _relay_tap_case(url: str, draft: int, case: str, insecure: bool,
                    compat: str, log: Path) -> tuple[str, str]:
    cmd = [sys.executable, "-m", "aiomoqt.tools.moq_interop_client",
           "-r", url, "--draft", str(draft), "-t", case]
    if insecure:
        cmd.append("--tls-disable-verify")
    if compat:
        cmd += ["--compat", compat]
    ok, _ = _run(cmd, log, 90)
    if not ok:
        return "FAIL", "timeout"
    text = log.read_text()
    # TAP line for a single-case run: "ok 1 - <case>" or "not ok 1 - ..."
    if re.search(rf"^ok 1 - {re.escape(case)}", text, re.MULTILINE):
        return "PASS", "ok"
    m = re.search(r"^  message: (.*)$", text, re.MULTILINE)
    if m:
        return "FAIL", m.group(1).strip('"')
    last = text.splitlines()[-1] if text else "no output"
    return "FAIL", last


def _relay_join(url: str, draft: int, insecure: bool,
                compat: str, log: Path) -> tuple[str, str]:
    return _relay_tap_case(url, draft, "join", insecure, compat, log)


def _relay_fetch(url: str, draft: int, insecure: bool,
                 compat: str, log: Path) -> tuple[str, str]:
    return _relay_tap_case(url, draft, "fetch", insecure, compat, log)


def _relay_discovery(url: str, draft: int, insecure: bool,
                     compat: str, log: Path) -> tuple[str, str]:
    return _relay_tap_case(url, draft, "namespace-discovery",
                           insecure, compat, log)


def _relay_data(url: str, draft: int, insecure: bool,
                compat: str, log: Path) -> tuple[str, str]:
    return _relay_tap_case(url, draft, "data-subgroup-basic",
                           insecure, compat, log)


# ---------------------------------------------------------------------------
# Manual runners
# ---------------------------------------------------------------------------
# media-locmaf: sub_media against Eyevinn's moqlivemock, which publishes a
# LOCMAF twin of every CMAF track. Public infrastructure: manual only.
MOQLIVEMOCK_URLS = {
    "raw-quic": "moqt://moqlivemock.demo.osaas.io:443",
    "h3-wt": "https://moqlivemock.demo.osaas.io:443/moq",
}
MOQLIVEMOCK_NAMESPACES = ("mlm/cmsf/clear", "mlm/cmsf/drm-cbcs")
_LOCMAF_TRACK_LINE = re.compile(r"^  (\S+): (\d+) frames, (\d+) rejected$",
                                re.MULTILINE)


def _media_locmaf(url: str, namespace: str, log: Path) -> tuple[str, str]:
    """PASS iff every LOCMAF track subscribed delivered frames and none was
    rejected; no per-track summary line is a FAIL."""
    out = tempfile.mkdtemp(prefix="aiomoqt-locmaf.")
    cmd = [sys.executable, "-m", "aiomoqt.tools.sub_media", url,
           "-N", namespace, "--packaging", "locmaf", "--draft", "18",
           "-t", "12", "--out", out]
    try:
        ok, _ = _run(cmd, log, 60)
    finally:
        shutil.rmtree(out, ignore_errors=True)
    if not ok:
        return "FAIL", "timeout"
    tracks = _LOCMAF_TRACK_LINE.findall(log.read_text())
    if not tracks:
        return "FAIL", "(no LOCMAF track summary)"
    bad = [f"{n}: {f} frames, {r} rejected" for n, f, r in tracks
           if int(f) == 0 or int(r)]
    if bad:
        return "FAIL", "; ".join(bad)
    frames = sum(int(f) for _, f, _ in tracks)
    return "PASS", f"{len(tracks)} tracks, {frames} frames, 0 rejected"


# ---------------------------------------------------------------------------
# Bench-tier runners
# ---------------------------------------------------------------------------
def _loopback_adaptive_bench(log_dir: Path) -> tuple[str, str]:
    log = log_dir / "loopback-adaptive-bench.log"
    # Loopback self-test: short ramp, kill after ~30s so the runner
    # isn't held open by the forever-probing controller.
    cmd = ["timeout", "--signal=INT", "--kill-after=3", "30",
           sys.executable, "-m", "aiomoqt.tools.adaptive_bench",
           "--start-mbps", "10", "--step-mbps", "10",
           "--max-mbps", "500", "--interval", "3",
           "-l", "100", "-k"]
    _run(cmd, log, 45)
    text = log.read_text()
    # fmt_bps emits e.g. "80Mbps" or "1.2Gbps" (no space) — match both
    # space-and-no-space forms.
    m = re.search(r"High-water:\s+([\d.]+)\s*([KMGT]?bps)", text)
    if m:
        return "PASS", f"high-water {m.group(1)} {m.group(2)}"
    return "FAIL", "(no summary — check log)"


# ---------------------------------------------------------------------------
# Record / print helpers
# ---------------------------------------------------------------------------
# Result tuple: (status, test_label, detail, log_path)
#   status ∈ {"PASS", "FAIL", "XFAIL", "SKIP"}
#   XFAIL: a FAIL on a non-gating (relay, draft) — reported, never gates.
Result = tuple[str, str, str, Path]


def _marker(status: str) -> str:
    return {"PASS": "[✓]  ", "FAIL": "[✗]  ",
            "XFAIL": "[xfail]", "SKIP": "[skip]"}[status]


def _print_result(res: Result) -> None:
    status, test, detail, _ = res
    print(f"  {_marker(status)} {test:<34} {detail}")


# ---------------------------------------------------------------------------
# Interop flake handling
# ---------------------------------------------------------------------------
# External relays + the network introduce transient failures (timeouts, the
# load_sim's fixed publisher-register wait racing a relay's namespace
# registration). Retry a failing interop case a couple of times: a genuine
# fail fails every attempt; a flake recovers and is annotated — so a flake
# never reads as a real failure.
INTEROP_RETRIES = 2


def _with_interop_retry(fn, fn_args, log) -> tuple[str, str]:
    status, detail = "FAIL", "(no attempts)"
    for attempt in range(INTEROP_RETRIES + 1):
        status, detail = fn(*fn_args, log)
        if status != "FAIL":
            if attempt:
                detail = f"{detail} (flaky: recovered on attempt {attempt + 1})"
            return status, detail
    return status, f"{detail} (failed all {INTEROP_RETRIES + 1} attempts)"


# ---------------------------------------------------------------------------
# Per-relay matrix runner (one worker of ThreadPoolExecutor)
# ---------------------------------------------------------------------------
def _run_relay_matrix(relay: dict, enabled: set[str],
                      log_dir: Path) -> list[Result]:
    results: list[Result] = []
    disabled = set(relay.get("disabled_suites", []))
    pub_mode = relay.get("pub_mode", "publish")
    insecure = bool(relay.get("insecure", False))
    # Per-relay compat tolerances forwarded to the interop client for
    # known non-spec relay behaviors (e.g. libquicr SUBSCRIBE_OK for a
    # nonexistent track). Tolerated outcomes are annotated, not hidden.
    compat_csv = ",".join(relay.get("compat", []))
    rname = relay["name"]
    # Drafts whose results gate CI. A FAIL on a draft NOT in this set is
    # recorded as XFAIL: reported, but never fails the job. Absent =
    # non-gating for every draft.
    gating_drafts = set(relay.get("gating", []))
    # Suites that run and report but never gate: a documented peer
    # non-conformance the case fails on correctly.
    xfail_suites = set(relay.get("xfail_suites", []))

    def _dispatch(suite: str, label_suffix: str, tag: str, slug: str,
                  fn, *fn_args, gating: bool = True) -> None:
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
        status, detail = _with_interop_retry(fn, fn_args, log)
        if status == "FAIL" and (not gating or suite in xfail_suites):
            status = "XFAIL"
        results.append((status, label, detail, log))
        marker = {"PASS": "[PASS]", "FAIL": "[FAIL]",
                  "XFAIL": "[XFAIL]"}[status]
        _progress(f"  {marker} {label}  {detail}")

    for transport, url in relay["urls"].items():
        for draft in relay["drafts"]:
            tag = f"{transport}/d{draft}"
            slug = f"{rname}_{transport}_d{draft}"
            gating = draft in gating_drafts

            if "relay-ctrl-msg" in enabled:
                _dispatch("relay-ctrl-msg", "", tag, slug,
                          _relay_ctrl_msg, url, draft, insecure, compat_csv,
                          gating=gating)
            if "relay-pub-sub" in enabled:
                tn = f"rr-{rname}-{transport}-{draft}-{_RUN_ID}"
                _dispatch("relay-pub-sub", f"[{pub_mode}]", tag, slug,
                          lambda u, d, log: _relay_pub_sub(
                              u, d, pub_mode, insecure, compat_csv,
                              log, tn),
                          url, draft, gating=gating)
            if "relay-join" in enabled:
                _dispatch("relay-join", "", tag, slug,
                          _relay_join, url, draft, insecure, compat_csv,
                          gating=gating)
            if "relay-fetch" in enabled:
                _dispatch("relay-fetch", "", tag, slug,
                          _relay_fetch, url, draft, insecure, compat_csv,
                          gating=gating)
            if "relay-discovery" in enabled:
                _dispatch("relay-discovery", "", tag, slug,
                          _relay_discovery, url, draft, insecure, compat_csv,
                          gating=gating)
            if "relay-data" in enabled:
                _dispatch("relay-data", "", tag, slug,
                          _relay_data, url, draft, insecure, compat_csv,
                          gating=gating)

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
    ap.add_argument("--skip-suite", action="append", default=[],
                    choices=SUITE_CHOICES, metavar="SUITE",
                    dest="skip_suites",
                    help="exclude this suite even if its tier is selected "
                         "(repeatable). macOS CI skips loopback-fetch: its "
                         "per-test event-loop teardown is pathologically slow "
                         "on GitHub macOS runners. The fetch logic is covered "
                         "on Linux, and the bench matrix covers macOS "
                         "loopback delivery.")
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
        enabled = set(SUITE_CHOICES) - set(MANUAL_SUITES)
    enabled -= set(args.skip_suites)

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
        if "pytest" in enabled:
            status, detail = _pytest_all(log_dir)
            record_and_print((status, "pytest", detail,
                              log_dir / "pytest.log"))

    # --- integration tier ---
    if enabled & set(TIERS["integration"]):
        print("\n== integration ==")
        if "loopback-pub-sub" in enabled:
            status, detail = _loopback_pub_sub(log_dir)
            record_and_print((status, "loopback-pub-sub", detail,
                              log_dir / "loopback-pub-sub.log"))
        if "loopback-pub-sub-tiny" in enabled:
            status, detail = _loopback_pub_sub_tiny(log_dir)
            record_and_print((status, "loopback-pub-sub-tiny", detail,
                              log_dir / "loopback-pub-sub-tiny.log"))
        if "loopback-pub-sub-streams" in enabled:
            status, detail = _loopback_pub_sub_streams(log_dir)
            record_and_print((status, "loopback-pub-sub-streams", detail,
                              log_dir / "loopback-pub-sub-streams.log"))
        if "loopback-pub-sub-paced" in enabled:
            status, detail = _loopback_pub_sub_paced(log_dir)
            record_and_print((status, "loopback-pub-sub-paced", detail,
                              log_dir / "loopback-pub-sub-paced.log"))
        for draft in (14, 16, 18, 20, 22):
            for quic in (False, True):
                suite = f"loopback-bench-d{draft}-{'quic' if quic else 'wt'}"
                if suite in enabled:
                    status, detail = _loopback_bench_combo(log_dir, draft, quic)
                    record_and_print((status, suite, detail,
                                      log_dir / f"{suite}.log"))
        for draft in (14, 16, 18):
            suite = f"loopback-adaptive-mp-d{draft}"
            if suite in enabled:
                status, detail = _loopback_adaptive_mp(log_dir, draft)
                record_and_print((status, suite, detail,
                                  log_dir / f"{suite}.log"))
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

    # --- manual suites ---
    if "media-locmaf" in enabled:
        print("\n== media-locmaf (moqlivemock) ==")
        for transport, url in MOQLIVEMOCK_URLS.items():
            for ns in MOQLIVEMOCK_NAMESPACES:
                slug = f"media-locmaf_{transport}_{ns.replace('/', '-')}"
                log = log_dir / f"{slug}.log"
                status, detail = _media_locmaf(url, ns, log)
                record_and_print((status, f"media-locmaf {transport} {ns}",
                                  detail, log))

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
    xfails = [r for r in results if r[0] == "XFAIL"]
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
    print(f"  {len(passes)} passed, {len(fails)} failed, "
          f"{len(xfails)} xfailed (non-gating), {len(skips)} skipped")

    # Markdown summary for GitHub Actions runners.
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        _write_gh_summary(Path(gh_summary), results, log_dir)

    if xfails:
        print(f"  {len(xfails)} non-gating XFAIL (peer issue, does not "
              f"gate):")
        for _, test, detail, _ in xfails:
            print(f"    {test}  {detail}")

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
    emoji = {"PASS": "✅", "FAIL": "❌", "XFAIL": "🟡", "SKIP": "⚪"}
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
    xfails = sum(1 for r in results if r[0] == "XFAIL")
    skips = sum(1 for r in results if r[0] == "SKIP")
    passes = sum(1 for r in results if r[0] == "PASS")
    lines.extend([
        "",
        f"**{passes} passed · {fails} failed · "
        f"{xfails} xfailed (non-gating) · {skips} skipped**",
        "",
        f"Logs: `{log_dir}`",
        "",
    ])
    with summary_path.open("a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
