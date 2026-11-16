"""cli.run: Ctrl-C reaches the main task through the event loop."""
import ast
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from aiomoqt.tests._procs import ENV, REPO
from aiomoqt.tests.test_cli_grid import EXAMPLES, TOOLS
from aiomoqt.utils import cli

posix_only = pytest.mark.skipif(not hasattr(signal, "pthread_kill"),
                                reason="needs POSIX signals")

# Tools whose coroutine installs its own SIGINT handler.
OWN_SIGINT = {"moq_interop_relay", "pub_media", "abr_ingest", "load_sim"}
# Tools that take Ctrl-C through asyncio.run.
ASYNCIO_RUN_SIGINT = {"loopback_bench"}


@pytest.fixture(autouse=True)
def default_sigint():
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    yield
    signal.signal(signal.SIGINT, signal.default_int_handler)


@posix_only
def test_sigint_runs_between_callbacks():
    async def main():
        signal.raise_signal(signal.SIGINT)
        pending = asyncio.current_task().cancelling()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return pending
    # asyncio.run would have cancelled inside raise_signal().
    assert cli.run(main()) == 0


@posix_only
def test_sigint_raises_after_cleanup():
    cleaned = []

    async def main():
        asyncio.get_running_loop().call_later(
            0.05, signal.raise_signal, signal.SIGINT)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)
    with pytest.raises(KeyboardInterrupt):
        cli.run(main())
    assert cleaned == [True]
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@posix_only
def test_swallowed_cancel_returns():
    async def main():
        asyncio.get_running_loop().call_later(
            0.05, signal.raise_signal, signal.SIGINT)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return 7
    assert cli.run(main()) == 7


@posix_only
def test_second_sigint_interrupts_a_wedged_loop():
    async def main():
        asyncio.get_running_loop().call_later(
            0.05, signal.raise_signal, signal.SIGINT)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            threading.Timer(0.05, os.kill,
                            (os.getpid(), signal.SIGINT)).start()
            time.sleep(10)
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        cli.run(main())
    assert time.monotonic() - t0 < 5


def test_non_default_handler_is_left_alone():
    def mine(*_):
        pass
    signal.signal(signal.SIGINT, mine)

    async def main():
        return signal.getsignal(signal.SIGINT)
    assert cli.run(main()) is mine
    assert signal.getsignal(signal.SIGINT) is mine


def test_off_main_thread_is_asyncio_run():
    out = []

    async def main():
        return 3
    t = threading.Thread(target=lambda: out.append(cli.run(main())))
    t.start()
    t.join()
    assert out == [3]


# Callbacks keep setting an Event the main task waits on, while a helper
# process sends SIGINT at random moments. asyncio.run's handler can cancel
# the waiter between Event.set()'s done() check and set_result().
_STRESS = r'''
import asyncio, json, os, signal, subprocess, sys
from aiomoqt.utils import cli
helper = subprocess.Popen([sys.executable, "-c", """
import os, random, signal, sys, time
while os.read(0, 1):
    time.sleep(random.uniform(0, 0.003))
    os.kill(int(sys.argv[1]), signal.SIGINT)
    os.write(1, b"k")
""", str(os.getpid())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    bufsize=0)
hits = 0

async def main():
    loop = asyncio.get_running_loop()

    def on_error(loop, ctx):
        global hits
        if isinstance(ctx.get("exception"), asyncio.InvalidStateError):
            hits += 1
        else:
            loop.default_exception_handler(ctx)
    loop.set_exception_handler(on_error)
    ev = asyncio.Event()

    def setter():
        ev.set()
        loop.call_soon(setter)
    loop.call_soon(setter)
    helper.stdin.write(b"x")
    while True:
        ev.clear()
        await ev.wait()

result = {}
for name, run, trials, stop_on_hit in (("asyncio", asyncio.run, 4000, True),
                                       ("cli", cli.run, 600, False)):
    hits, ends = 0, {}
    for _ in range(trials):
        try:
            run(main())
            end = "returned"
        except KeyboardInterrupt:
            end = "KeyboardInterrupt"
        helper.stdout.read(1)
        ends[end] = ends.get(end, 0) + 1
        if stop_on_hit and hits:
            break
    result[name] = {"hits": hits, "ends": ends}
helper.stdin.close()
helper.wait()
print(json.dumps(result))
'''


@posix_only
def test_sigint_stress():
    p = subprocess.run([sys.executable, "-c", _STRESS], capture_output=True,
                       text=True, timeout=120, cwd=REPO, env=ENV)
    assert p.returncode == 0, p.stderr
    r = json.loads(p.stdout)
    assert r["asyncio"]["hits"] > 0, r
    assert r["cli"] == {"hits": 0, "ends": {"KeyboardInterrupt": 600}}, r


def _calls_asyncio_run(path):
    tree = ast.parse(path.read_text())
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "asyncio"]


@pytest.mark.parametrize("tool", TOOLS + EXAMPLES)
def test_entry_points_use_cli_run(tool):
    sub = "examples" if tool in EXAMPLES else "tools"
    path = Path(REPO, "aiomoqt", sub, f"{tool}.py")
    lines = _calls_asyncio_run(path)
    if tool in OWN_SIGINT:
        src = path.read_text()
        assert ("add_signal_handler" in src
                or "signal.signal(signal.SIGINT" in src), tool
    elif tool not in ASYNCIO_RUN_SIGINT:
        assert not lines, f"{tool}: asyncio.run at line(s) {lines}; use cli.run"
