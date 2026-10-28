#!/usr/bin/env python3
"""sweep.py — manifest-driven measurement orchestration for the aiomoqt release sweep.

One point = one (optional) relay + one publisher + N subscribers, run for a fixed
duration, preceded and followed by a host-state check.  Results are written
atomically as Appendix A `result.json` files under
`perf/release-sweep/runs/<UTC>/<point>/rep-NN/`, so a kill loses at most the
in-flight repetition.

Run with the *system* python3 (needs PyYAML); the bench processes themselves run
under the arm interpreter (`arms/<arm>/venv/bin/python`).

    sweep.py --manifest manifests/baseline-v0.yaml --dry-run
    sweep.py --manifest manifests/baseline-v0.yaml --only D1 --reps 1
    sweep.py --manifest manifests/baseline-v0.yaml --only D1,D2 --resume

Resume semantics (also in README):
  * a new invocation creates a new `runs/<UTC>/` block and skips nothing;
  * `--resume` reuses the newest existing block and skips every repetition whose
    `result.json` is valid *and* was produced by the same arm/config fingerprint;
  * `--force` re-runs everything selected, in a new block unless `--resume`.

Concurrency: an exclusive flock on `<runs>/.sweep.lock`.  A second invocation
waits up to `--lock-wait` seconds (default 0) and then exits 75.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - operator error, not a code path
    sys.exit("sweep.py needs PyYAML (system python3): python3 -c 'import yaml'")

HERE = Path(__file__).resolve().parent          # .../perf/release-sweep/scripts
RS = HERE.parent                                # .../perf/release-sweep
REPO = RS.parent.parent                         # .../aiomoqt
SWEEP_ROOT = Path(os.environ.get("PERF_SWEEP_ROOT", "/home/gmarzot/Projects/marzresearch/perf-sweep"))
HOSTCHECK = HERE / "hostcheck.py"
SCHEMA_VERSION = 1
EXIT_LOCKED = 75
DEFAULT_CLOCK_TOL_US = 2000.0

BENCH_PATTERNS = (
    "aiomoqt.tools.sub_bench",
    "aiomoqt.tools.pub_server",
    "aiomoqt.tools.pub_bench",
    "aiomoqt.tools.loopback_bench",
    "aiomoqt.tools.load_sim",
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def sha256_file(path: Path) -> str | None:
    import hashlib

    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def env_fingerprint(host_name: str, host: "Host") -> dict:
    """Kernel/CPU state that must be identical across a comparison block."""
    cmd = "uname -r; nproc; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a"
    rc, out = host.run(cmd, timeout=10)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return {
        "host": host_name,
        "kernel": lines[0] if lines else None,
        "nproc": int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None,
        "governor": lines[2] if len(lines) > 2 else None,
    }


# --------------------------------------------------------------------------
# hosts
# --------------------------------------------------------------------------
_VERBOSE = False


def vlog(msg: str) -> None:
    """Phase log for --verbose; wall-clock stamps make skew/overlap visible."""
    if _VERBOSE:
        print(f"        [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Host:
    """A bench host: either this machine or a remote box reached over ssh."""

    def __init__(self, name: str, spec: dict, arm: str):
        self.name = name
        self.spec = spec or {}
        self.local = bool(self.spec.get("local"))
        self.ssh = self.spec.get("ssh") or name
        self.root = Path(self.spec.get("root") or SWEEP_ROOT)
        self._arm = arm
        self._children: dict[int, subprocess.Popen] = {}

    # -- command shaping ---------------------------------------------------
    def python(self, arm: str | None = None) -> Path:
        """Interpreter for bench tools inside the arm venv."""
        tmpl = self.spec.get("python") or "arms/{arm}/venv/bin/python"
        rel = tmpl.format(arm=arm or self._arm).replace("{root}", str(self.root))
        path = Path(rel) if rel.startswith("/") else self.root / rel
        return path

    def expand(self, argv: list[str], arm: str) -> list[str]:
        out = []
        for a in argv:
            a = str(a).replace("{python}", str(self.python(arm))).replace("{root}", str(self.root))
            out.append(a)
        # Bench tools print interval rows; without -u python block-buffers stdout
        # to a file, so a point that gets killed leaves a 0-byte log and no
        # diagnosis. Keep every arm-venv invocation unbuffered.
        py = str(self.python(arm))
        if out and out[0] == py and "-P" in out:
            out.insert(out.index("-P") + 1, "-u")
        return out

    # -- execution ---------------------------------------------------------
    def _ssh_prefix(self) -> list[str]:
        # No ControlMaster here: a persistent master inherits the calling
        # ssh's stdout/stderr pipes, so subprocess capture never sees EOF and
        # every call blocks until its timeout.
        return [
            "ssh",
            "-n",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
            "-o", "LogLevel=ERROR",
            self.ssh,
        ]

    def run(self, cmd: str, timeout: float = 30.0, input_bytes: bytes | None = None):
        if self.local:
            p = subprocess.run(
                ["sh", "-c", cmd], capture_output=True, timeout=timeout, input=input_bytes,
                stdin=(None if input_bytes is not None else subprocess.DEVNULL),
            )
        else:
            p = subprocess.run(
                self._ssh_prefix() + [cmd],
                capture_output=True, timeout=timeout, input=input_bytes,
                # ssh without a stdin redirection reads the caller's stdin and
                # forwards it to the remote command, so the session only closes
                # when that stdin hits EOF -- which surfaces here as a bogus
                # TimeoutExpired for a command that in fact finished.
                stdin=(None if input_bytes is not None else subprocess.DEVNULL),
            )
        return p.returncode, (p.stdout or b"").decode(errors="replace") + (p.stderr or b"").decode(errors="replace")

    def start(self, role: str, argv: list[str], log: Path | None, pin: str | None,
              logdir_remote: str | None = None) -> dict:
        """Start a background process; return {role, pid, log, host}."""
        if self.local:
            log.parent.mkdir(parents=True, exist_ok=True)
            full = (["taskset", "-c", pin] if pin else []) + argv
            fh = open(log, "wb")
            try:
                proc = subprocess.Popen(
                    full, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                fh.close()
            self._children[proc.pid] = proc
            return {"role": role, "pid": proc.pid, "log": str(log), "host": self.name}
        # remote: setsid + pidfile so we can signal the whole process group
        remote_log = f"{logdir_remote}/{role}.log" if logdir_remote else f"/tmp/{uuid.uuid4().hex}.log"
        inner = " ".join(shlex.quote(a) for a in ((['taskset', '-c', pin] if pin else []) + argv))
        # `nohup setsid ... & echo $!` returns the pid on stdout in the same ssh
        # round-trip; polling a pidfile with extra ssh calls was slow and could
        # time out while the process was in fact running.
        cmd = (
            f"mkdir -p {shlex.quote(logdir_remote or '/tmp')} && "
            f"cd {shlex.quote(str(self.root))} && "
            f"nohup setsid {inner} >> {shlex.quote(remote_log)} 2>&1 < /dev/null & echo $!"
        )
        # The backgrounded child keeps the ssh channel open until it exits, so a
        # plain blocking run() would sit here for the whole measurement window
        # and report a bogus timeout. Read the pid, then drop the client: the
        # remote child is in its own session (setsid), so it survives.
        proc = subprocess.Popen(
            self._ssh_prefix() + [cmd], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            out_b, err_b = proc.communicate(timeout=6.0)
        except subprocess.TimeoutExpired as exc:
            # ssh is still holding the channel for the backgrounded child; the
            # pid line has already been emitted, so drop the client and keep it.
            proc.kill()
            tail_b, tail_err = proc.communicate()
            out_b = (exc.output or b"") + (tail_b or b"")
            err_b = (exc.stderr or b"") + (tail_err or b"")
        rc = proc.returncode
        out = (out_b or b"").decode(errors="replace") + (err_b or b"").decode(errors="replace")
        pid = None
        for line in reversed(out.splitlines()):
            if line.strip().isdigit():
                pid = int(line.strip())
                break
        if pid is None:
            raise RuntimeError(f"remote start failed on {self.name} for {role}: rc={rc} {out[:400]}")
        return {"role": role, "pid": pid, "log": remote_log, "host": self.name}

    def kill(self, pid: int, grace: float = 5.0, sig=signal.SIGTERM) -> int | None:
        """Signal a process group, escalating to SIGKILL. Returns rc when known.

        ``sig`` may be a signal number or a name accepted by kill(1). SIGINT is
        used for bench tools that print their summary from the
        KeyboardInterrupt path instead of a SIGTERM handler.
        """
        if self.local:
            child = self._children.get(pid)
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError):
                return child.poll() if child is not None else None
            if child is not None:
                try:
                    return child.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
            else:
                deadline = time.time() + grace
                while time.time() < deadline and self.alive(pid):
                    time.sleep(0.05)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            if child is not None:
                try:
                    return child.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    return None
            return None
        sname = sig if isinstance(sig, str) else signal.Signals(sig).name.removeprefix("SIG")

        def _try(cmd: str, t: float) -> None:
            # A slow ssh probe must never abort the point: the summary is
            # already in the remote log by this stage.
            try:
                self.run(cmd, timeout=t)
            except subprocess.TimeoutExpired:
                vlog(f"{self.name}: probe exceeded {t:.0f}s, continuing")

        def _alive() -> bool:
            try:
                return self.alive(pid)
            except subprocess.TimeoutExpired:
                return True

        for s, wait in ((sname, grace), ("KILL", 2.0)):
            _try(f"kill -{s} -- -{pid} 2>/dev/null; kill -{s} {pid} 2>/dev/null", 5)
            deadline = time.time() + wait
            while time.time() < deadline:
                # 1 s cadence: each probe is an ssh round-trip
                time.sleep(min(1.0, max(0.0, deadline - time.time())))
                if not _alive():
                    return None
        return None

    def alive(self, pid: int) -> bool:
        if self.local:
            child = self._children.get(pid)
            if child is not None:
                return child.poll() is None
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True
        rc, out = self.run(f"kill -0 {pid} 2>/dev/null && echo alive", timeout=5)
        return "alive" in out

    def wait_udp_port(self, port: int, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc, out = self.run(f"ss -lunH 2>/dev/null | grep -c ':{port} '", timeout=5)
            if out.strip().splitlines() and out.strip().splitlines()[-1].strip().isdigit():
                if int(out.strip().splitlines()[-1].strip()) > 0:
                    return True
            time.sleep(0.2)
        return False

    def stray_procs(self) -> list[str]:
        pat = "|".join(BENCH_PATTERNS)
        rc, out = self.run(f"pgrep -af '{pat}' 2>/dev/null | grep -v pgrep", timeout=10)
        return [ln for ln in out.splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# manifest / point model
# --------------------------------------------------------------------------
def load_manifest(path: Path) -> dict:
    with open(path) as fh:
        man = yaml.safe_load(fh)
    if not isinstance(man, dict) or "points" not in man:
        raise SystemExit(f"{path}: manifest needs a 'points' list")
    man.setdefault("defaults", {})
    man.setdefault("hosts", {"shenzhen": {"local": True}})
    return man


def merged_point(man: dict, point: dict) -> dict:
    """defaults <- point, with nested dicts merged one level deep."""
    out = json.loads(json.dumps(man.get("defaults") or {}))
    for k, v in point.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    cfg = {"P": 1, "s": 4096, "g": 200, "r": 0}
    cfg.update(out.get("config") or {})
    out["config"] = cfg
    out.setdefault("duration_s", 60)
    out.setdefault("interval_s", 5)
    out.setdefault("warmup_s", 5)
    out.setdefault("sub_delay_s", 2.0)
    out.setdefault("reps", 3)
    out.setdefault("transport", "raw")
    out.setdefault("draft", 16)
    out.setdefault("subs", 1)
    out.setdefault("pin", man.get("defaults", {}).get("pin") or
                   {"relay": "0-1", "pub": "2-5", "sub": "0-3"})
    return out


def transport_flag(transport: str) -> str:
    return "--wt" if transport.lower() in ("wt", "webtransport", "h3") else "-Q"


def url_for(host_addr: str, port: int, transport: str, path: str = "") -> str:
    if transport.lower() in ("wt", "webtransport", "h3"):
        return f"https://{host_addr}:{port}{path}"
    return f"moqt://{host_addr}:{port}"


def build_steps(p: dict, hosts: dict[str, Host], relay: dict) -> list[dict]:
    """Return the ordered step list for a point.

    step = {role, host, argv, bg, delay_s, wait_port, kind}
      kind: "relay" | "pub" | "sub" | "bench"
    """
    topo = str(p["topology"])
    cfg = p["config"]
    tr = p["transport"]
    dur = int(p["duration_s"])
    iv = int(p["interval_s"])
    draft = int(p["draft"])
    ns = p.get("namespace", "aiomoqt")
    trk = p.get("track", "track")
    certs = p.get("certs") or str(RS / "certs")
    base_port = int(p.get("port", relay.get("pub_port", 4434)))
    relay_addr = p.get("relay_addr") or relay.get("addr") or "10.14.25.173"
    relay_port = int(relay.get("port", 9768))
    wt_path = relay.get("wt_path", "/moq-relay")
    steps: list[dict] = []

    def common_sub(url: str, host_name: str = "shenzhen", role: str = "sub") -> dict:
        return {
            "role": role, "host": host_name, "bg": False, "delay_s": p["sub_delay_s"],
            "kind": "sub",
            # sub_bench runs until the publisher closes, and pub_server never
            # closes, so the harness owns the measurement window: run for
            # warmup+duration then SIGINT (the tool prints its summary from the
            # KeyboardInterrupt path).
            "run_for_s": float(p["warmup_s"]) + float(p["duration_s"]),
            # a subscriber that is seconds behind on a firehose needs a while to
            # unwind its receive loop; give SIGINT room before SIGKILL
            "kill_grace_s": 20.0,
            "argv": ["{python}", "-P", "-m", "aiomoqt.tools.sub_bench", url,
                     "-N", ns, "-T", trk, "--draft", str(draft), "-k",
                     "-t", str(dur), "-i", str(iv)],
        }

    def common_pub_server(host_name: str, bind: str) -> dict:
        argv = ["{python}", "-P", "-m", "aiomoqt.tools.pub_server",
                "--bind", bind, "-p", str(base_port), transport_flag(tr),
                "--draft", str(draft), "-N", ns, "-T", trk,
                "-s", str(cfg["s"]), "-g", str(cfg["g"]), "-P", str(cfg["P"])]
        if cfg.get("r"):
            argv += ["-r", str(cfg["r"])]
        argv += ["--cert", str(certs) + "/cert.pem", "--key", str(certs) + "/key.pem"]
        return {"role": "pub", "host": host_name, "bg": True, "delay_s": 0.0,
                "kind": "pub", "wait_port": base_port, "argv": argv}

    def common_pub_bench(url: str, host_name: str) -> dict:
        argv = ["{python}", "-P", "-m", "aiomoqt.tools.pub_bench", url,
                "-N", ns, "-T", trk, "-s", str(cfg["s"]), "-g", str(cfg["g"]),
                "-P", str(cfg["P"]), "-t", str(dur), "--draft", str(draft), "-k"]
        if cfg.get("r"):
            argv += ["-r", str(cfg["r"])]
        if p.get("video"):
            argv += ["--video", str(p["video"])]
        return {"role": "pub", "host": host_name, "bg": True, "delay_s": 0.0,
                "kind": "pub", "wait_port": None, "argv": argv}

    def relay_step(host_name: str) -> dict:
        return {
            "role": "relay", "host": host_name, "bg": True, "delay_s": 0.0,
            "kind": "relay", "wait_port": relay_port,
            "argv": [str(relay.get("binary", "/home/gmarzot/Projects/marzresearch/moqx/build/moqx")),
                     "serve", "--config", str(relay.get("config", RS / "moqx-perf.yaml"))],
        }

    if topo == "L":
        argv = ["{python}", "-P", "-m", "aiomoqt.tools.loopback_bench",
                "-P", str(cfg["P"]), "-s", str(cfg["s"]), "-g", str(cfg["g"]),
                "-t", str(dur), "-i", str(iv)]
        if cfg.get("r"):
            argv += ["-r", str(cfg["r"])]
        if tr.lower() in ("wt", "webtransport", "h3"):
            argv += ["--wt"]
        steps.append({"role": "bench", "host": p.get("bench_host", "shenzhen"),
                      "bg": False, "delay_s": 0.0, "kind": "bench", "argv": argv})
        return steps

    if topo == "P":
        steps.append(common_pub_server("shenzhen", "127.0.0.1"))
        steps.append(common_sub(url_for("127.0.0.1", base_port, tr)))
        return steps

    if topo == "D":
        steps.append(common_pub_server("shenzhen", "0.0.0.0"))
        # D is a two-node topology: the subscriber must live on seawolf, never
        # on the publisher's host (that would silently measure loopback).
        steps.append(common_sub(url_for(p.get("pub_addr", "10.14.25.173"), base_port, tr),
                                "seawolf"))
        return steps

    if topo in ("R", "F", "M", "C2"):
        steps.append(relay_step("shenzhen"))
        steps.append(common_pub_bench(url_for(relay_addr, relay_port, tr, wt_path), "shenzhen"))
        nsubs = int(p["subs"])
        for i in range(nsubs):
            steps.append(common_sub(url_for(relay_addr, relay_port, tr, wt_path), "seawolf",
                                    role=f"sub{i + 1}"))
        return steps

    if topo == "Ri":
        steps.append(relay_step("shenzhen"))
        steps.append(common_pub_bench(url_for(relay_addr, relay_port, tr, wt_path), "seawolf"))
        steps.append(common_sub(url_for("127.0.0.1", relay_port, tr, wt_path), "shenzhen"))
        return steps

    if topo == "C1":
        steps.append(relay_step("shenzhen"))
        steps.append({
            "role": "load", "host": "shenzhen", "bg": False, "delay_s": 0.0, "kind": "bench",
            "argv": ["{python}", "-P", "-m", "aiomoqt.tools.load_sim",
                     url_for(relay_addr, relay_port, tr, wt_path),
                     "-f", p.get("flow", "churn-storm"), "--draft", str(draft), "-k",
                     "--report", "churn.csv"],
        })
        return steps

    raise SystemExit(f"unknown topology {topo!r} for point {p.get('id')}")


# --------------------------------------------------------------------------
# hostcheck
# --------------------------------------------------------------------------
def run_hostcheck(host: Host, *, samples: int, require: bool, guard_llama: bool,
                  peer: str | None, iface: str | None) -> dict:
    args = ["--json", "--samples", str(samples)]
    if require:
        args.append("--require")
    if guard_llama:
        args.append("--guard-llama")
    if peer:
        args += ["--peer", peer]
    if iface:
        args += ["--iface", iface]
    script = HOSTCHECK.read_bytes()
    cmd = "python3 - " + " ".join(shlex.quote(a) for a in args)
    if host.local:
        rc, out = host.run(f"{sys.executable} {shlex.quote(str(HOSTCHECK))} " +
                           " ".join(shlex.quote(a) for a in args), timeout=180)
        rc_code = rc
    else:
        rc, out = host.run(cmd, timeout=180, input_bytes=script)
        rc_code = rc
    payload = None
    start = out.find("{")
    if start >= 0:
        try:
            payload = json.loads(out[start:])
        except ValueError:
            payload = None
    if payload is None:
        payload = {"ok": False, "error": out[-2000:], "abort": [{"rule": "hostcheck_failed",
                                                                "detail": out[-400:]}]}
    payload["_exit_code"] = rc_code
    return payload


def abort_reasons(pre: dict, post: dict | None, clock_tol_us: float) -> list[dict]:
    reasons = list(pre.get("abort") or [])
    if post:
        reasons += list(post.get("abort") or [])
        a = (pre.get("clock") or {}).get("offset_us")
        b = (post.get("clock") or {}).get("offset_us")
        if a is not None and b is not None and abs(b - a) > clock_tol_us:
            reasons.append({"rule": "clock_offset_jump",
                            "detail": f"{a}us -> {b}us (> {clock_tol_us}us)"})
        qa = (pre.get("qdisc") or {}).get("root")
        qb = (post.get("qdisc") or {}).get("root")
        if qa and qb and qa != qb:
            reasons.append({"rule": "qdisc_changed", "detail": f"{qa} -> {qb}"})
    return reasons


# --------------------------------------------------------------------------
# one point
# --------------------------------------------------------------------------
def run_point(p: dict, *, hosts: dict[str, Host], relay: dict, arm: str, arm_dir: Path,
              run_root: Path, rep: int, args) -> dict:
    point_id = p["id"]
    rep_dir = run_root / point_id / f"rep-{rep:02d}"
    rep_dir.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    t0 = time.time()
    notes: list[str] = []
    procs: list[dict] = []
    status = "ok"
    reason = ""

    # --- pre-flight -------------------------------------------------------
    pre_local = run_hostcheck(hosts["shenzhen"], samples=min(3, args.hostcheck_samples),
                              require=False, guard_llama=False, peer=args.clock_peer, iface=args.iface)
    pre_remote = None
    if not hosts["seawolf"].local:
        pre_remote = run_hostcheck(hosts["seawolf"], samples=min(3, args.hostcheck_samples),
                                   require=False, guard_llama=True, peer=None, iface=args.iface)
    write_json_atomic(rep_dir / "hostcheck-pre-local.json", pre_local)
    if pre_remote is not None:
        write_json_atomic(rep_dir / "hostcheck-pre-remote.json", pre_remote)

    pre_abort = abort_reasons(pre_local, None, args.clock_tolerance_us)
    if pre_abort or pre_local.get("_exit_code") not in (0,) or pre_remote and pre_remote.get("_exit_code") not in (0,):
        status = "aborted_pre"
        reason = "; ".join(f"{a['rule']}: {a.get('detail', '')}" for a in pre_abort) or "hostcheck exit != 0"
        notes.append(f"pre-flight abort: {reason}")

    clock = (pre_local.get("clock") or {})
    offset_us = clock.get("offset_us") or 0
    offset_ms = float(offset_us) / 1000.0

    steps = build_steps(p, hosts, relay)
    argv_record = {}
    for s in steps:
        host = hosts[s["host"]]
        argv_record.setdefault(s["role"], host.expand(s["argv"], arm))

    # --- relay probe (relay topologies only) ------------------------------
    relay_probe = None
    if status == "ok" and any(s["kind"] == "relay" for s in steps):
        probe_url = url_for(p.get("relay_addr") or relay.get("addr") or "10.14.25.173",
                            int(relay.get("port", 9768)), p["transport"], relay.get("wt_path", ""))
        probe_argv = hosts["shenzhen"].expand(
            ["{python}", "-P", "-m", "aiomoqt.tools.relay_probe", "--url", probe_url,
             "--draft", str(p["draft"]), "-k"], arm)
        # The relay socket comes up a beat after the process starts, so a single
        # probe right after launch races the bind and reports "connection
        # refused". Retry for up to ~15 s before declaring the point broken.
        rc, out, attempts = 1, "", 0
        for attempts in range(1, 11):
            rc, out = hosts["shenzhen"].run(" ".join(shlex.quote(a) for a in probe_argv), timeout=60)
            if rc == 0:
                break
            time.sleep(1.5)
        relay_probe = {"argv": probe_argv, "rc": rc, "stdout": out[-2000:], "attempts": attempts}
        write_json_atomic(rep_dir / "relay-probe.json", relay_probe)
        if rc != 0:
            status = "aborted_pre"
            reason = f"relay_probe failed rc={rc} after {attempts} attempts"
            notes.append(reason)

    # --- run --------------------------------------------------------------
    remote_logs: dict[str, str] = {}
    remote_host_of: dict[str, str] = {}
    if status == "ok":
        deadline = time.time() + float(p["duration_s"]) + float(p["warmup_s"]) + args.timeout_margin
        try:
            for s in steps:
                host = hosts[s["host"]]
                argv = host.expand(s["argv"], arm)
                argv_record[s["role"]] = argv
                vlog(f"{point_id} rep-{rep:02d}: step {s['role']} @{s['host']} "
                     f"run_for_s={s.get('run_for_s')}")
                if s.get("delay_s"):
                    time.sleep(float(s["delay_s"]))
                if s.get("run_for_s") and not s["bg"]:
                    # Timed measurement window: the subscriber cannot know when
                    # the firehose ends, so start it, wait warmup+duration, then
                    # interrupt it and keep the summary it prints on the way out.
                    pin_sub = (p["pin"] or {}).get("sub")
                    if host.local:
                        info = host.start(s["role"], argv, rep_dir / f"{s['role']}.log", pin=pin_sub)
                    else:
                        logdir = f"{host.root}/runs/{run_root.name}/{point_id}/rep-{rep:02d}"
                        host.run(f"mkdir -p {shlex.quote(logdir)}", timeout=20)
                        info = host.start(s["role"], argv, None, pin=pin_sub, logdir_remote=logdir)
                        remote_logs[s["role"]] = info["log"]
                        remote_host_of[s["role"]] = s["host"]
                    procs.append(info)
                    t_end = time.time() + float(s["run_for_s"])
                    # sleep out the window instead of polling liveness: a remote
                    # liveness probe is an ssh round-trip and would dominate the
                    # window (and starve the per-point watchdog).
                    while time.time() < t_end:
                        time.sleep(min(0.5, max(0.0, t_end - time.time())))
                    try:
                        still_alive = host.alive(info["pid"])
                    except subprocess.TimeoutExpired:
                        still_alive = True
                    if not still_alive:
                        notes.append(f"{s['role']} exited before window end")
                    grace = max(args.kill_grace, float(s.get("kill_grace_s") or 0))
                    rc = host.kill(info["pid"], grace=grace, sig=signal.SIGINT)
                    vlog(f"{point_id}: {s['role']} interrupted rc={rc}")
                    if rc not in (0, None, -signal.SIGINT):
                        notes.append(f"{s['role']} exited rc={rc}")
                    continue
                if s["bg"]:
                    if host.local:
                        info = host.start(s["role"], argv, rep_dir / f"{s['role']}.log",
                                          pin=(p["pin"] or {}).get("pub" if s["kind"] == "pub" else "relay"))
                    else:
                        logdir = f"{host.root}/runs/{run_root.name}/{point_id}/rep-{rep:02d}"
                        info = host.start(s["role"], argv, None, pin=(p["pin"] or {}).get("sub"),
                                          logdir_remote=logdir)
                        remote_logs[s["role"]] = info["log"]
                        remote_host_of[s["role"]] = s["host"]
                    procs.append(info)
                    vlog(f"{point_id}: {s['role']} started pid={info['pid']} log={info['log']}")
                    if s.get("wait_port") and not host.wait_udp_port(int(s["wait_port"]),
                                                                     timeout=args.start_timeout):
                        raise RuntimeError(f"{s['role']} did not listen on udp/{s['wait_port']}")
                else:
                    if host.local:
                        log_path = rep_dir / f"{s['role']}.log"
                        with open(log_path, "wb") as fh:
                            rc = subprocess.run(
                                (["taskset", "-c", (p["pin"] or {}).get("sub")] if (p["pin"] or {}).get("sub") else []) + argv,
                                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                timeout=max(1.0, deadline - time.time()),
                            ).returncode
                    else:
                        logdir = f"{host.root}/runs/{run_root.name}/{point_id}/rep-{rep:02d}"
                        host.run(f"mkdir -p {shlex.quote(logdir)}", timeout=20)
                        cmd = " ".join(shlex.quote(a) for a in argv)
                        pin = (p["pin"] or {}).get("sub")
                        if pin:
                            cmd = f"taskset -c {pin} {cmd}"
                        rc = host.run(f"cd {shlex.quote(str(host.root))} && {cmd} > "
                                      f"{shlex.quote(logdir + '/' + s['role'] + '.log')} 2>&1",
                                      timeout=max(1.0, deadline - time.time()))[0]
                        remote_logs[s["role"]] = f"{logdir}/{s['role']}.log"
                        remote_host_of[s["role"]] = s["host"]
                    if rc != 0:
                        notes.append(f"{s['role']} exited rc={rc}")
                        if status == "ok":
                            status = "failed"
                            reason = f"{s['role']} rc={rc}"
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            reason = f"harness timeout: {exc}"
        except Exception as exc:  # noqa: BLE001 - record, never lose the point
            status = "failed"
            reason = f"{type(exc).__name__}: {exc}"
        finally:
            for info in reversed(procs):
                host = hosts[info["host"]]
                host.kill(info["pid"], grace=args.kill_grace)

    # --- fetch remote logs ------------------------------------------------
    for role, remote_log in remote_logs.items():
        dest = rep_dir / f"{role}.log"
        cmd = f"cat {shlex.quote(remote_log)}"
        h = hosts[remote_host_of[role]]
        argv_cat = (["sh", "-c", cmd] if h.local else h._ssh_prefix() + [cmd])
        with open(dest, "wb") as fh:
            subprocess.run(argv_cat, stdout=fh, stderr=subprocess.DEVNULL, timeout=60)

    local_sub = rep_dir / "sub.log"
    if not local_sub.exists() and (rep_dir / "sub1.log").exists():
        local_sub.write_text((rep_dir / "sub1.log").read_text(errors="replace"))

    # --- post-flight ------------------------------------------------------
    post_local = run_hostcheck(hosts["shenzhen"], samples=min(3, args.hostcheck_samples), require=False,
                               guard_llama=False, peer=args.clock_peer, iface=args.iface)
    write_json_atomic(rep_dir / "hostcheck-post-local.json", post_local)
    post_remote = None
    if not hosts["seawolf"].local:
        post_remote = run_hostcheck(hosts["seawolf"], samples=min(3, args.hostcheck_samples), require=False,
                                    guard_llama=True, peer=None, iface=args.iface)
        write_json_atomic(rep_dir / "hostcheck-post-remote.json", post_remote)
    post_abort = abort_reasons(post_local, pre_local, args.clock_tolerance_us)
    if post_abort and status == "ok":
        status = "aborted_post"
        reason = "; ".join(f"{a['rule']}: {a.get('detail', '')}" for a in post_abort)
        notes.append(f"post-flight abort: {reason}")

    # --- strays -----------------------------------------------------------
    strays = {"shenzhen": hosts["shenzhen"].stray_procs()}
    if not hosts["seawolf"].local:
        strays["seawolf"] = hosts["seawolf"].stray_procs()
    if any(strays.values()):
        notes.append(f"stray bench processes after point: {strays}")
        if status == "ok":
            status = "dirty"

    # --- parse ------------------------------------------------------------
    sys.path.insert(0, str(HERE))
    import collect as _collect

    parsed_sub = None
    if local_sub.exists():
        parsed_sub = _collect.parse_file(str(local_sub), offset_ms)
    parsed_pub = None
    pub_log = rep_dir / "pub.log"
    if pub_log.exists():
        parsed_pub = _collect.parse_file(str(pub_log), offset_ms)
        parsed_pub["summary"] = {f"pub_{k}": v for k, v in parsed_pub["summary"].items()}

    summary = {}
    if parsed_sub:
        summary = dict(parsed_sub["summary"])
    if parsed_pub:
        for k, v in parsed_pub["summary"].items():
            summary.setdefault(k, v)
    samples = _collect.sample_rows(parsed_sub) if parsed_sub else []
    if status == "ok" and not summary:
        status = "failed"
        reason = "no parsed summary from sub log"
        notes.append(reason)

    wheel = arm_dir / "wheels"
    arm_meta = load_json(arm_dir / "arm.json", {}) or {}
    aiopquic_whl = sorted(wheel.glob("aiopquic-*.whl")) if wheel.exists() else []

    result = {
        "point_id": point_id,
        "arm": arm,
        "topology": p["topology"],
        "transport": p["transport"],
        "draft": int(p["draft"]),
        "config": {**p["config"], "t": int(p["duration_s"])},
        "hosts": {"relay": "shenzhen",
                  "pub": "shenzhen" if p["topology"] not in ("Ri",) else "seawolf",
                  "load": "seawolf", "subs": int(p["subs"])},
        "shas": {
            "aiomoqt": args.aiomoqt_sha,
            "aiomoqt_dist": args.aiomoqt_dist,
            "aiopquic": arm_meta.get("aiopquic", "unknown"),
            "aiopquic_wheel_sha256": sha256_file(aiopquic_whl[0]) if aiopquic_whl else None,
            "python": (arm_meta.get("python") or ""),
        },
        "env": {
            "cc": args.cc,
            "jemalloc": False,
            "gso": args.gso,
            "qdisc": ((pre_local.get("qdisc") or {}).get("root")),
            "pinning": " ".join(f"{k} {v}" for k, v in (p["pin"] or {}).items()),
            "clock_offset_us": offset_us,
            "clock_method": (pre_local.get("clock") or {}).get("method"),
            "clock_error_us": (pre_local.get("clock") or {}).get("error_us"),
            "clock_tolerance_us": args.clock_tolerance_us,
            "fingerprint": {"shenzhen": env_fingerprint("shenzhen", hosts["shenzhen"]),
                            **({"seawolf": env_fingerprint("seawolf", hosts["seawolf"])}
                               if not hosts["seawolf"].local else {})},
        },
        "started_utc": started,
        "duration_s": round(time.time() - t0, 3),
        "samples": samples,
        "summary": summary,
        "artifacts": sorted(p.name for p in rep_dir.glob("*.log")),
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_root.name,
            "rep": rep,
            "status": status,
            "reason": reason,
            "notes": notes,
            "arm": arm,
            "arm_dir": str(arm_dir),
            "manifest": str(args.manifest),
            "point_argv": argv_record,
            "relay_probe": relay_probe,
            "hostcheck_pre_local": {k: pre_local.get(k) for k in
                                    ("host","ts_utc","qdisc","qdisc_root","txqueuelen","rings","rps",
                                     "sysctls","governor","disk","link","steal_pct","clock","llama","abort")},
            "hostcheck_post_local": {k: post_local.get(k) for k in
                                     ("host","ts_utc","qdisc","txqueuelen","rings","rps","sysctls",
                                      "governor","disk","link","steal_pct","clock","llama","abort")},
            "hostcheck_pre_remote": pre_remote,
            "hostcheck_post_remote": post_remote,
            "strays": strays,
            "config_fingerprint": {
                "arm": arm,
                "transport": p["transport"],
                "draft": int(p["draft"]),
                "config": p["config"],
                "duration_s": int(p["duration_s"]),
                "subs": int(p["subs"]),
                "video": p.get("video"),
            },
            "sweep_version": "1.0",
            "hosts": {k: {"local": h.local, "ssh": h.ssh, "root": str(h.root)}
                      for k, h in hosts.items()},
        },
    }
    return result


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def acquire_lock(runs: Path, wait_s: float):
    runs.mkdir(parents=True, exist_ok=True)
    lock_path = runs / ".sweep.lock"
    fh = open(lock_path, "w")
    deadline = time.time() + wait_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(f"{os.getpid()} {utc_now()}\n")
            fh.flush()
            return fh
        except BlockingIOError:
            if time.time() >= deadline:
                fh.close()
                print(f"sweep.py: another sweep holds {lock_path}; exiting {EXIT_LOCKED}",
                      file=sys.stderr)
                raise SystemExit(EXIT_LOCKED)
            time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="manifest-driven aiomoqt bench sweep")
    ap.add_argument("--manifest", default=str(RS / "manifests" / "baseline-v0.yaml"))
    ap.add_argument("--only", help="comma-separated point ids")
    ap.add_argument("--reps", type=int, help="override manifest reps")
    ap.add_argument("--arm", help="override arm name")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    ap.add_argument("--force", action="store_true", help="re-run even if a result exists")
    ap.add_argument("--resume", action="store_true", help="continue the newest run block")
    ap.add_argument("--out", default=str(RS / "runs"), help="root for run blocks")
    ap.add_argument("--lock-wait", type=float, default=0.0)
    ap.add_argument("--tag", default="", help="suffix appended to the run block name")
    ap.add_argument("--hostcheck-samples", type=int, default=20)
    ap.add_argument("--clock-peer", default=os.environ.get("CLOCK_PEER", "10.14.25.37:9876"))
    ap.add_argument("--clock-tolerance-us", type=float,
                    default=float(os.environ.get("CLOCK_JUMP_TOLERANCE_US", DEFAULT_CLOCK_TOL_US)))
    ap.add_argument("--iface", default=os.environ.get("PERF_IFACE"))
    ap.add_argument("--start-timeout", type=float, default=20.0)
    ap.add_argument("--timeout-margin", type=float, default=120.0,
                    help="seconds of slack past warmup+duration before a point is "
                         "declared hung (covers ssh round-trips, kill grace and the "
                         "per-point hostcheck pair)")
    ap.add_argument("--kill-grace", type=float, default=5.0)
    ap.add_argument("--cc", default=os.environ.get("PERF_CC", "bbr1"))
    ap.add_argument("--gso", action="store_true", default=True)
    ap.add_argument("--no-gso", dest="gso", action="store_false")
    ap.add_argument("--examples", action="store_true", help="print README-style examples and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    global _VERBOSE
    _VERBOSE = bool(args.verbose)

    if args.examples:
        print(__doc__)
        return 0

    man = load_manifest(Path(args.manifest))
    arm = args.arm or man.get("defaults", {}).get("arm") or man.get("arm") or "base-070rc2"
    arm_dir = SWEEP_ROOT / "arms" / arm
    if not (arm_dir / "venv" / "bin" / "python").exists():
        raise SystemExit(f"arm interpreter missing: {arm_dir}/venv/bin/python")

    # git identity of the tree under test
    def git(*a) -> str:
        try:
            return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                                  text=True, timeout=15).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    args.aiomoqt_sha = git("rev-parse", "--short", "HEAD")
    args.aiomoqt_dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    local_whl = sorted((Path("/tmp/aiomoqt-wheels")).glob("aiomoqt-*.whl"))
    args.aiomoqt_dist = (local_whl[-1].stem.split("-")[1] if local_whl else
                         git("describe", "--tags", "--always"))

    hosts = {name: Host(name, spec, arm) for name, spec in (man.get("hosts") or {}).items()}
    for need in ("shenzhen", "seawolf"):
        if need not in hosts:
            raise SystemExit(f"manifest is missing host {need!r}")
    relay = man.get("relay") or {}

    points = [merged_point(man, pt) for pt in man["points"]]
    seen = set()
    for p in points:
        if p["id"] in seen:
            raise SystemExit(f"duplicate point id {p['id']}")
        seen.add(p["id"])
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        unknown = want - seen
        if unknown:
            raise SystemExit(f"unknown point id(s): {', '.join(sorted(unknown))}")
        points = [p for p in points if p["id"] in want]
    if args.reps:
        for p in points:
            p["reps"] = args.reps

    runs = Path(args.out)
    run_root = None
    if not args.dry_run:
        if args.resume and runs.exists():
            cands = sorted([d for d in runs.iterdir() if d.is_dir() and not d.name.startswith(".")])
            if cands:
                run_root = cands[-1]
        if run_root is None:
            run_root = runs / (utc_stamp() + (f"-{args.tag}" if args.tag else ""))
    else:
        run_root = runs / "<UTC>"

    # ---- dry run ---------------------------------------------------------
    if args.dry_run:
        print(f"manifest : {args.manifest}")
        print(f"arm      : {arm}  ({arm_dir})")
        print(f"run block: {run_root}")
        print(f"points   : {len(points)} selected, {sum(p['reps'] for p in points)} reps\n")
        for p in points:
            steps = build_steps(p, hosts, relay)
            cfg = p["config"]
            print(f"[{p['id']:4s}] {p['topology']:2s} {p['transport']:3s} draft={p['draft']:2d} "
                  f"P={cfg['P']} s={cfg['s']} g={cfg['g']} r={cfg.get('r', 0)} "
                  f"t={p['duration_s']}s subs={p['subs']} reps={p['reps']}"
                  + (f"  # {p['purpose']}" if p.get("purpose") else ""))
            for s in steps:
                host = hosts[s["host"]]
                cmd = " ".join(shlex.quote(a) for a in host.expand(s["argv"], arm))
                prefix = f"ssh {host.ssh} " if not host.local else ""
                print(f"        {s['role']:5s} {'bg' if s['bg'] else 'fg'} +{s['delay_s']:.1f}s  "
                      f"{prefix}{cmd}")
        return 0

    lock = acquire_lock(runs, args.lock_wait)
    try:
        run_root.mkdir(parents=True, exist_ok=True)
        print(f"run block: {run_root}")
        for p in points:
            for rep in range(1, int(p["reps"]) + 1):
                rep_dir = run_root / p["id"] / f"rep-{rep:02d}"
                result_path = rep_dir / "result.json"
                existing = load_json(result_path)
                if existing and not args.force:
                    fp = (existing.get("meta") or {}).get("config_fingerprint") or {}
                    same = (fp.get("arm") == arm and fp.get("transport") == p["transport"]
                            and fp.get("config") == p["config"]
                            and fp.get("duration_s") == int(p["duration_s"]))
                    if (existing.get("meta") or {}).get("status") in ("ok", "dirty") and same:
                        print(f"  skip  {p['id']} rep-{rep:02d} (valid result)")
                        continue
                print(f"  run   {p['id']} rep-{rep:02d} ...", flush=True)
                try:
                    result = run_point(p, hosts=hosts, relay=relay, arm=arm, arm_dir=arm_dir,
                                       run_root=run_root, rep=rep, args=args)
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "point_id": p["id"], "arm": arm, "topology": p["topology"],
                        "transport": p["transport"], "draft": int(p["draft"]),
                        "config": {**p["config"], "t": int(p["duration_s"])},
                        "hosts": {}, "shas": {}, "env": {}, "started_utc": utc_now(),
                        "duration_s": 0, "samples": [], "summary": {}, "artifacts": [],
                        "meta": {"schema_version": SCHEMA_VERSION, "run_id": run_root.name,
                                 "rep": rep, "status": "failed",
                                 "reason": f"{type(exc).__name__}: {exc}", "notes": []},
                    }
                write_json_atomic(result_path, result)
                st = (result.get("meta") or {}).get("status")
                print(f"        -> {st} {result.get('summary', {}).get('mbps', '')} Mbps")
                if st not in ("ok", "dirty"):
                    print(f"        aborted: {(result.get('meta') or {}).get('reason')}", file=sys.stderr)
                    return 1
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
