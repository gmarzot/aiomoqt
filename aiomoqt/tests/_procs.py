"""Run aiomoqt tools as processes from the source tree, for CLI tests."""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONUNBUFFERED": "1",
       "PYTHONPATH": os.pathsep.join(
           [str(REPO), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tool_cmd(tool, *args):
    return [sys.executable, "-m", f"aiomoqt.tools.{tool}", *map(str, args)]


def spawn(tool, *args, log):
    """Start a tool with stdout and stderr in `log` (a Path)."""
    with open(log, "w") as f:
        return subprocess.Popen(tool_cmd(tool, *args), stdout=f,
                                stderr=subprocess.STDOUT, cwd=REPO, env=ENV)


def run(tool, *args, timeout=60):
    """Run a tool to completion: (exit code, stdout + stderr)."""
    p = subprocess.run(tool_cmd(tool, *args), capture_output=True, text=True,
                       timeout=timeout, cwd=REPO, env=ENV)
    return p.returncode, p.stdout + p.stderr


def wait_for(log, marker, proc, timeout=30.0):
    """Wait for `marker` in `log`; fail with the log if `proc` exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = log.read_text(errors="replace")
        if marker in text:
            return text
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    raise AssertionError(f"no {marker!r} (rc={proc.poll()}):\n"
                         + log.read_text(errors="replace"))


def stop(proc, timeout=10):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def objects_received(out):
    """sub_bench's summary object count; fails if the line is missing."""
    m = re.search(r"^\s*Objects:\s+([\d,]+)$", out, re.MULTILINE)
    assert m, out
    return int(m.group(1).replace(",", ""))
