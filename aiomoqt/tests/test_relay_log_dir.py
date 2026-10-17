"""--log-dir: relay.log mirrors the stderr log with rotation, and
crash.log takes a start line before faulthandler's dumps."""
import logging
import os
import re
import stat
import sys

import pytest

from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.utils import logger as aiomoqt_logger


class _Armed(Exception):
    pass


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    root = logging.getLogger()
    handlers = list(root.handlers)
    shown = aiomoqt_logger._handler
    level = relay.logger.level
    relay.logger.setLevel(logging.INFO)
    enabled = []
    monkeypatch.setattr(relay.faulthandler, "enable",
                        lambda **kw: enabled.append(kw))
    old_umask = os.umask(0o077)
    try:
        yield tmp_path / "logs", enabled
    finally:
        os.umask(old_umask)
        for h in root.handlers:
            if h not in handlers:
                root.removeHandler(h)
                h.close()
        aiomoqt_logger.set_log_handler(shown)
        relay.logger.setLevel(level)
        if relay._crash_file is not None:
            relay._crash_file.close()
            relay._crash_file = None


def test_relay_log_rotates_and_mirrors_stderr(log_dir):
    path, _enabled = log_dir
    relay._start_log_dir(str(path))
    files = [h for h in logging.getLogger().handlers
             if isinstance(h, relay._LogFile)]
    assert len(files) == 1
    assert (files[0].maxBytes, files[0].backupCount) == (10 * 1024 * 1024, 4)

    relay.logger.info(f"relay: start {relay._versions_line()} pid "
                      f"{os.getpid()}")
    logging.getLogger("other.lib").warning("from the root")
    files[0].flush()
    text = (path / "relay.log").read_text()
    # aiomoqt's own format, and the root's for everything else.
    assert re.search(r"^\S+ \S+\.\d{3} INFO : relay: start aiomoqt \S+ "
                     r"aiopquic \S+ pid \d+$", text, re.M), text
    assert re.search(r"^\S+ \S+ WARNING other\.lib: from the root$",
                     text, re.M), text
    # The stderr handler still gets every aiomoqt record.
    assert any(t is not files[0] for t in aiomoqt_logger._handler.targets)

    assert _mode(path) == 0o755
    assert _mode(path / "relay.log") == 0o644
    assert _mode(path / "crash.log") == 0o644
    files[0].doRollover()
    assert _mode(path / "relay.log") == 0o644
    assert _mode(path / "relay.log.1") == 0o644


def test_crash_log_start_line_precedes_faulthandler(log_dir, monkeypatch):
    path, enabled = log_dir
    seen = []
    monkeypatch.setattr(relay.faulthandler, "enable", lambda **kw: (
        enabled.append(kw), seen.append((path / "crash.log").read_text())))
    (path).mkdir()
    (path / "crash.log").write_text("earlier run\n")
    relay._start_log_dir(str(path))
    assert len(enabled) == 1 and enabled[0]["file"] is relay._crash_file
    assert re.fullmatch(
        r"earlier run\nrelay: start \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ "
        r"pid \d+ aiomoqt \S+ aiopquic \S+\n", seen[0]), seen[0]


async def test_heartbeat_dumps_to_crash_log(log_dir, monkeypatch):
    path, _enabled = log_dir
    relay._start_log_dir(str(path))
    armed = []

    def later(timeout, exit=False, file=sys.stderr):
        armed.append(file)
        raise _Armed

    monkeypatch.setattr(relay.faulthandler, "dump_traceback_later", later)
    with pytest.raises(_Armed):
        await relay._heartbeat(5.0)
    assert armed == [relay._crash_file]
