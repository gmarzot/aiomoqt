"""--log-dir: relay.log mirrors the stderr log with rotation, both
written by a thread so a slow write never holds the logging caller, and
crash.log takes a start line before faulthandler's dumps."""
import io
import logging
from logging.handlers import QueueHandler
import os
import re
import stat
import sys
import time

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
        relay._stop_log_listener()
        for h in list(root.handlers):
            if h not in handlers:
                root.removeHandler(h)
                h.close()
        for h in handlers:
            if h not in root.handlers:
                root.addHandler(h)
        aiomoqt_logger.set_log_handler(shown)
        relay.logger.setLevel(level)
        if relay._crash_file is not None:
            relay._crash_file.close()
            relay._crash_file = None


def test_relay_log_rotates_and_mirrors_stderr(log_dir):
    path, _enabled = log_dir
    relay._start_log_dir(str(path))
    listener = relay._log_listener
    files = [h for h in listener.handlers if isinstance(h, relay._LogFile)]
    assert len(files) == 1
    assert (files[0].maxBytes, files[0].backupCount) == (10 * 1024 * 1024, 4)
    queued = [h for h in logging.getLogger().handlers
              if isinstance(h, QueueHandler)]
    assert len(queued) == 1

    relay.logger.info(f"relay: start {relay._versions_line()} pid "
                      f"{os.getpid()}")
    logging.getLogger("other.lib").warning("from the root")
    relay._stop_log_listener()
    text = (path / "relay.log").read_text()
    # aiomoqt's own format, and the root's for everything else.
    assert re.search(r"^\S+ \S+\.\d{3} INFO : relay: start aiomoqt \S+ "
                     r"aiopquic \S+ pid \d+$", text, re.M), text
    assert re.search(r"^\S+ \S+ WARNING other\.lib: from the root$",
                     text, re.M), text
    # aiomoqt records go through the same queue, to stderr too.
    assert aiomoqt_logger._handler is queued[0]
    assert any(isinstance(h, relay._Shown) for h in listener.handlers)

    assert _mode(path) == 0o755
    assert _mode(path / "relay.log") == 0o644
    assert _mode(path / "crash.log") == 0o644
    files[0].doRollover()
    assert _mode(path / "relay.log") == 0o644
    assert _mode(path / "relay.log.1") == 0o644


def test_a_slow_relay_log_write_does_not_hold_the_caller(log_dir,
                                                         monkeypatch):
    path, _enabled = log_dir
    emit = relay._LogFile.emit

    def slow_emit(self, record):
        time.sleep(0.2)
        emit(self, record)
    monkeypatch.setattr(relay._LogFile, "emit", slow_emit)
    relay._start_log_dir(str(path))
    t0 = time.monotonic()
    for i in range(3):
        relay.logger.info(f"relay: line {i}")
        logging.getLogger("other.lib").warning(f"root line {i}")
    assert time.monotonic() - t0 < 0.1
    relay._stop_log_listener()
    text = (path / "relay.log").read_text()
    for i in range(3):
        assert f"relay: line {i}" in text and f"root line {i}" in text, text


class _SlowStream(io.StringIO):
    def write(self, s):
        time.sleep(0.2)
        return super().write(s)


def test_a_blocked_stderr_does_not_hold_the_caller(log_dir):
    path, _enabled = log_dir
    shown, root_shown = _SlowStream(), _SlowStream()
    handler = aiomoqt_logger._handler
    stream = handler.setStream(shown)
    logging.getLogger().addHandler(logging.StreamHandler(root_shown))
    try:
        relay._start_log_dir(str(path))
        t0 = time.monotonic()
        for i in range(3):
            relay.logger.info(f"relay: line {i}")
            logging.getLogger("other.lib").warning(f"root line {i}")
        assert time.monotonic() - t0 < 0.1
        relay._stop_log_listener()
    finally:
        handler.setStream(stream)
    lines = [x for x in shown.getvalue().splitlines() if "relay: line" in x]
    assert len(lines) == 3 and all(
        line.endswith(f" relay: line {i}") for i, line in enumerate(lines)), lines
    assert root_shown.getvalue().splitlines() == [
        f"root line {i}" for i in range(3)], root_shown.getvalue()


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
