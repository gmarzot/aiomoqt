"""SIGUSR1 / SIGUSR2 diagnostic signal handlers.

When AIOMOQT_TASK_DUMP=1 is set, install() registers:

  SIGUSR1: asyncio task-dump. Prints every asyncio Task's stack to
    stderr — use for "what's every coroutine awaiting" diagnosis.
        kill -USR1 <pid>

  SIGUSR2: aiopquic counter-dump. Walks the live TransportContext
    registry and prints per-context forensic counters (tx_ring
    pushes/pops/arms/fires, wake calls, sc->tx drain arms/fires,
    ns-resolution timestamps). Use to localize wake-chain stalls:
    arms-without-fires, pops-without-pushes, etc.
        kill -USR2 <pid>

Default-off so production runs aren't surprised by registered signal
handlers they didn't ask for. Idempotent: calling install() twice
with the env var set just re-registers the same handlers.
"""
from __future__ import annotations

import os
import signal
import sys


def install() -> bool:
    """Register SIGUSR1 + SIGUSR2 handlers iff AIOMOQT_TASK_DUMP=1.

    Returns True if installed, False otherwise.
    """
    if not os.environ.get("AIOMOQT_TASK_DUMP"):
        return False

    def _dump_tasks(signum, frame):
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

    def _dump_counters(signum, frame):
        # Lazy import — aiopquic may not be importable in every
        # context that loads aiomoqt (tests, tooling). Failing here
        # should not break the SIGUSR1 task-dump path.
        try:
            from aiopquic._binding._transport import dump_all_counters
        except ImportError as e:
            print(f"(SIGUSR2 counter-dump: aiopquic unavailable: {e})",
                  file=sys.stderr, flush=True)
            return
        print(f"\n=== aiopquic counter-dump (SIGUSR2) ===",
              file=sys.stderr, flush=True)
        try:
            dump_all_counters(file=sys.stderr)
        except Exception as e:
            print(f"(SIGUSR2 counter-dump failed: {e})",
                  file=sys.stderr, flush=True)
        print("=== end counter-dump ===",
              file=sys.stderr, flush=True)

        # Per-session data-stream chain stats. Localizes where bytes
        # are pinned downstream of aiopquic's drain.
        try:
            import gc
            seen = 0
            for obj in gc.get_objects():
                try:
                    dumper = getattr(obj, '_dump_data_streams', None)
                    streams = getattr(obj, '_data_streams', None)
                    if (dumper is None or not callable(dumper)
                            or not isinstance(streams, dict)):
                        continue
                    print(f"\n=== aiomoqt session #{seen} ===",
                          file=sys.stderr, flush=True)
                    dumper(file=sys.stderr)
                    seen += 1
                except Exception:
                    continue
            if seen == 0:
                print("(no live MOQTSession instances)",
                      file=sys.stderr, flush=True)
        except Exception as e:
            print(f"(SIGUSR2 chain-dump failed: {e})",
                  file=sys.stderr, flush=True)
        print("=== end SIGUSR2 ===\n",
              file=sys.stderr, flush=True)

    signal.signal(signal.SIGUSR1, _dump_tasks)
    signal.signal(signal.SIGUSR2, _dump_counters)
    print(
        "(AIOMOQT_TASK_DUMP=1: SIGUSR1=task stacks, "
        "SIGUSR2=aiopquic counters)",
        file=sys.stderr,
    )
    return True
