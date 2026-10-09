"""Publisher conformance sweep against moq-contribution-interop-runner.

Starts the runner in driven mode with the aiomoqt adapter, runs every d18
scenario on one transport that needs no FETCH, merges the requirement
outcomes and compares them with a baseline of passing rows.

    python tests/contrib/sweep.py --runner build/moq-interop-runner --out out

Exit status: 0 when the sweep ran (regressions are reported, not failed,
unless --gate), 1 on a regression with --gate, 2 when the sweep itself
could not run. An empty sweep is never a pass.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIN = HERE.parent.parent / ".github" / "contrib-runner-pin"
TRACK = {"namespace_hex": ["6d65646961"], "name_hex": "766964655f31"}
# Run alone: the API refuses these inside a batch of raw probes.
TYPED = {"subscribe-to-publisher-track",
         "subscribe-again-to-established-publisher-track"}
BATCH = 100
RANK = {"fail": 3, "pass": 2, "not_run": 1}


class SweepError(Exception):
    pass


def _call(base, method, path, body=None):
    req = urllib.request.Request(
        base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _healthy(base):
    try:
        return _call(base, "GET", "/healthz")[0] == 200
    except OSError:
        return False


def _cert(out):
    cert, key = out / "cert.pem", out / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2",
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True, timeout=30)
    return cert, key


def _start_runner(args, out):
    base = f"http://127.0.0.1:{args.port}"
    if _healthy(base):
        raise SweepError(f"port {args.port} already serves a runner")
    cert, key = _cert(out)
    lo, hi = args.publisher_ports
    cmd = [str(args.runner), "--bind", "127.0.0.1", "--port", str(args.port),
           "--database", str(out / "runs.sqlite3"),
           "--publisher-bind", "127.0.0.1",
           "--publisher-advertise", "127.0.0.1",
           "--publisher-port-start", str(lo), "--publisher-port-end", str(hi),
           "--tls-cert", str(cert), "--tls-key", str(key),
           "--driver-executable", str(args.adapter),
           "--driver-log-root", str(out / "driver-logs"),
           "--publisher-no-fetch",
           # d18 gives an unknown token alias no REQUEST_ERROR code; the
           # session sends UNKNOWN_AUTH_TOKEN_ALIAS's session code, 0x17.
           "--unknown-auth-token-alias-compat-code", "0x17",
           # Credentials the adapter's --token-reject policy refuses.
           "--invalid-auth-token", "1:696e76616c6964",
           "--expired-auth-token", "1:65787069726564",
           "--denied-authorization-token", "denied"]
    if args.runner_data:
        cmd += ["--docs", str(args.runner_data / "docs"),
                "--requirements", str(args.runner_data / "requirements")]
    env = dict(os.environ, AIOMOQT_PYTHON=args.python)
    log = open(out / "runner.log", "wb")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    for _ in range(60):
        if proc.poll() is not None:
            raise SweepError(f"runner exited ({proc.returncode}); "
                             f"see {out / 'runner.log'}")
        if _healthy(base):
            return base, proc
        time.sleep(0.5)
    _stop(proc)
    raise SweepError("runner never became healthy")


def _stop(proc):
    """Stop the runner and every publisher it started."""
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def _run(base, args, out, scenarios):
    status, body = _call(base, "POST", "/api/v1/runs", {
        "draft": 18, "transport": args.transport, "mode": "driven",
        "scenarios": scenarios, "timeout_ms": args.timeout_ms,
        "track": TRACK})
    if status != 201:
        return status, body
    rid = body["run"]["id"]
    deadline = (time.time() + 30
                + len(scenarios) * (args.timeout_ms / 1000 + 4))
    while time.time() < deadline:
        _, cur = _call(base, "GET", f"/api/v1/runs/{rid}")
        if cur.get("run", {}).get("state") == "finalized":
            break
        time.sleep(1)
    else:
        raise SweepError(f"{rid} not finalized before its deadline")
    for ext in ("json", "tap"):
        try:
            with urllib.request.urlopen(f"{base}/results/{rid}.{ext}",
                                        timeout=30) as r:
                (out / "results" / f"{rid}.{ext}").write_bytes(r.read())
        except OSError as e:
            raise SweepError(f"fetching {rid}.{ext}: {e}") from e
    print(f"{rid}: {len(scenarios)} scenario(s) -> {cur['run']['verdict']}",
          flush=True)
    return status, body


def sweep(base, args, out):
    results = out / "results"
    results.mkdir(exist_ok=True)
    for stale in results.iterdir():
        stale.unlink()
    _, health = _call(base, "GET", "/healthz")
    todo = sorted({p["scenario"] for p in health["executable_profiles"]
                   if p["draft"] == 18 and p["transport"] == args.transport
                   and p["mode"] == "driven" and not p["requires_fetch"]})
    if not todo:
        raise SweepError(f"runner offers no driven d18 {args.transport} "
                         f"scenarios")
    typed = set(TYPED)
    batch = [s for s in todo if s not in typed]
    print(f"{len(todo)} scenarios", flush=True)
    while batch:
        chunk = batch[:BATCH]
        status, body = _run(base, args, out, chunk)
        if status == 422:
            msg = json.dumps(body)
            bad = next((s for s in chunk if s in msg), None)
            if bad is None:
                raise SweepError(f"422 naming no scenario: {msg}")
            typed.add(bad)
            batch.remove(bad)
            continue
        if status != 201:
            raise SweepError(f"HTTP {status}: {body}")
        batch = batch[BATCH:]
    for s in sorted(typed & set(todo)):
        status, body = _run(base, args, out, [s])
        if status != 201:
            raise SweepError(f"{s}: HTTP {status}: {body}")


def outcomes(results):
    """Worst outcome per scored requirement across every run."""
    rows = {}
    for f in sorted(results.glob("*.json")):
        for r in json.loads(f.read_text())["requirements"]:
            if not r.get("score_eligible"):
                continue
            cur = rows.get(r["id"])
            if cur is None or RANK.get(r["outcome"], 0) > \
                    RANK.get(cur["outcome"], 0):
                rows[r["id"]] = {
                    "strength": r["strength"], "outcome": r["outcome"],
                    "section": r["source"]["section"],
                    "summary": r["summary"]}
    if not rows:
        raise SweepError("no scored requirements in the results")
    return rows


def report(rows, baseline, runner_id, transport):
    passed = {k for k, r in rows.items() if r["outcome"] == "pass"}
    regressed = sorted(set(baseline) - passed)
    gained = sorted(passed - set(baseline))
    counts = Counter((r["strength"], r["outcome"]) for r in rows.values())
    strengths = sorted({s for s, _ in counts},
                       key=lambda s: ("MUST", "MUST NOT", "SHOULD",
                                      "SHOULD NOT", "MAY").index(s)
                       if s in ("MUST", "MUST NOT", "SHOULD", "SHOULD NOT",
                                "MAY") else 9)
    lines = [f"## Contribution runner: d18 {transport}",
             f"`{runner_id}`, {len(rows)} scored requirements", "",
             "| strength | pass | fail | not run |", "|---|---|---|---|"]
    for s in strengths:
        lines.append(f"| {s} | {counts[(s, 'pass')]} | {counts[(s, 'fail')]}"
                     f" | {counts[(s, 'not_run')]} |")

    def section(title, ids):
        if ids:
            lines.extend(["", f"**{title} ({len(ids)})**", ""])
            for k in ids:
                r = rows.get(k, {})
                lines.append(f"- `{k}` {r.get('outcome', 'absent')}: "
                             f"{r.get('summary', '')[:110]}")

    section("Regressions: passed in the baseline", regressed)
    section("New passes: add to the baseline", gained)
    section("Failing MUST / MUST NOT", sorted(
        k for k, r in rows.items() if r["outcome"] == "fail"
        and r["strength"] in ("MUST", "MUST NOT")))
    return "\n".join(lines) + "\n", regressed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runner", type=Path, required=True,
                    help="moq-interop-runner binary")
    ap.add_argument("--out", type=Path, required=True,
                    help="directory for results, logs and the summary")
    ap.add_argument("--runner-data", type=Path,
                    help="installed share/moq-interop directory; by default "
                         "the runner reads its own source tree")
    ap.add_argument("--adapter", type=Path,
                    default=HERE / "aiomoqt-adapter.sh")
    ap.add_argument("--python", default=sys.executable,
                    help="python the adapter runs pub_bench with")
    ap.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    ap.add_argument("--write-baseline", action="store_true",
                    help="record this sweep's passing rows as the baseline")
    ap.add_argument("--gate", action="store_true",
                    help="exit 1 when a baseline row no longer passes")
    ap.add_argument("--transport", default="native-quic")
    ap.add_argument("--timeout-ms", type=int, default=8000,
                    help="per-scenario timeout")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--publisher-ports", type=int, nargs=2,
                    default=(24443, 24452), metavar=("LO", "HI"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    runner_id = (next((ln.strip() for ln in PIN.read_text().splitlines()
                       if ln.strip() and not ln.startswith("#")), "unpinned")
                 if PIN.exists() else "unpinned")
    try:
        base, proc = _start_runner(args, args.out)
        try:
            sweep(base, args, args.out)
        finally:
            _stop(proc)
        rows = outcomes(args.out / "results")
    except (SweepError, OSError, subprocess.SubprocessError) as e:
        print(f"sweep failed: {e}", file=sys.stderr)
        return 2
    (args.out / "outcomes.json").write_text(json.dumps(rows, indent=1))
    if args.write_baseline:
        args.baseline.write_text(json.dumps({
            "runner": runner_id, "draft": 18, "transport": args.transport,
            "pass": sorted(k for k, r in rows.items()
                           if r["outcome"] == "pass")}, indent=1) + "\n")
        print(f"baseline written: {args.baseline}")
        return 0
    try:
        base_doc = json.loads(args.baseline.read_text())
    except (OSError, ValueError) as e:
        print(f"no usable baseline {args.baseline}: {e}", file=sys.stderr)
        return 2
    want = {"runner": runner_id, "draft": 18, "transport": args.transport}
    got = {k: base_doc.get(k) for k in want}
    if got != want:
        print(f"baseline {args.baseline} is for {got}, this sweep is "
              f"{want}; refresh it with --write-baseline", file=sys.stderr)
        return 2
    text, regressed = report(rows, base_doc["pass"], runner_id,
                             args.transport)
    (args.out / "summary.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)
    print(text)
    return 1 if args.gate and regressed else 0


if __name__ == "__main__":
    sys.exit(main())
