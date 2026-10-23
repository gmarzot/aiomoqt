"""SIGINT and SIGTERM on long-running tools: a clean exit, no traceback or
unretrieved task exception."""
import signal

import pytest

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests._procs import spawn, stop, wait_for

CLEAN_RC = (0, 130)


def _interrupt(proc, log, sig=signal.SIGINT, timeout=15):
    proc.send_signal(sig)
    try:
        rc = proc.wait(timeout=timeout)
    finally:
        stop(proc)
    out = log.read_text(errors="replace")
    assert "Traceback" not in out, out
    assert "never retrieved" not in out, out
    return rc, out


@requires_certs
@pytest.mark.parametrize("marker", ["latency threshold:", "warming"],
                         ids=["start", "mid-run"])
def test_adaptive_bench_sigint(tmp_path, marker):
    log = tmp_path / "adaptive.log"
    proc = spawn("adaptive_bench", "--interval", 1, "-t", 30, log=log)
    wait_for(log, marker, proc)
    rc, out = _interrupt(proc, log)
    assert rc in CLEAN_RC, out
