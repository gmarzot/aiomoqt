"""A draft pinned by the interop runner (DRAFT / MOQT_DRAFT) that aiomoqt
cannot speak is reported as unsupported, never run at another draft."""
import os
import subprocess
import sys


def _run(module, *args, draft):
    env = {**os.environ, "DRAFT": draft}
    env.pop("MOQT_DRAFT", None)
    return subprocess.run([sys.executable, "-m", module, *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_client_reports_an_unsupported_pin_as_not_supported():
    r = _run("aiomoqt.tools.moq_interop_client", "-r", "moqt://127.0.0.1:9",
             "-t", "setup-only", draft="20")
    assert r.returncode == 127
    assert r.stdout.splitlines()[:2] == [
        "TAP version 14",
        "1..0 # SKIP draft 20 not supported; aiomoqt speaks [14, 16, 18]"]


def test_relay_refuses_an_unsupported_pin():
    r = _run("aiomoqt.tools.moq_interop_relay", "--port", "9",
             "--cert", "x", "--key", "y", draft="20")
    assert r.returncode == 1
    assert "draft 20 not supported" in r.stderr
