"""SIGUSR1 asyncio task-dump diagnostic.

When AIOMOQT_TASK_DUMP=1 is set, install() registers a SIGUSR1 handler
that prints every asyncio Task's stack to stderr. Send the signal during
a hang to see exactly what every coroutine is awaiting:

    kill -USR1 <pid>

Default-off so production runs aren't surprised by a registered signal
handler they didn't ask for. Idempotent: calling install() twice with
the env var set just re-registers the same handler.
"""
from __future__ import annotations

import os
import signal
import sys


def install() -> bool:
    """Register the SIGUSR1 handler iff AIOMOQT_TASK_DUMP=1 in env.

    Returns True if installed, False otherwise.
    """
    if not os.environ.get("AIOMOQT_TASK_DUMP"):
        return False

    def _dump(signum, frame):
        import asyncio
        try:
            tasks = list(asyncio.all_tasks())
        except RuntimeError:
            print(
                "(SIGUSR1 task-dump: no running asyncio loop)",
                file=sys.stderr,
            )
            return
        print(
            f"\n=== task-dump ({len(tasks)} tasks; SIGUSR1) ===",
            file=sys.stderr,
        )
        for task in tasks:
            name = (task.get_name() if hasattr(task, 'get_name')
                    else 'unknown')
            done = task.done()
            print(
                f"\n--- {name} done={done} cancelled={task.cancelled()} ---",
                file=sys.stderr,
            )
            try:
                task.print_stack(file=sys.stderr)
            except Exception as e:
                print(f"  (no stack: {e})", file=sys.stderr)
        print("=== end task-dump ===\n", file=sys.stderr)
        sys.stderr.flush()

    signal.signal(signal.SIGUSR1, _dump)
    print(
        "(AIOMOQT_TASK_DUMP=1: SIGUSR1 will dump asyncio task stacks)",
        file=sys.stderr,
    )
    return True
