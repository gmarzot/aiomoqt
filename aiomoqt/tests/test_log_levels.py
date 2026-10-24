"""TRACE below DEBUG, category loggers and log specs."""
import contextlib
import logging
import os
import subprocess
import sys

import pytest

from aiomoqt.utils import cli
from aiomoqt.utils.logger import (
    CATEGORIES, TRACE, get_log_level, get_logger,
    get_logger_levels, parse_log_spec, set_log_level, set_logger_levels,
    step_log_level,
)

# -- specs, flags, environment ----------------------------------------

def test_trace_is_below_debug_and_named():
    assert TRACE < logging.DEBUG
    assert logging.getLevelName(TRACE) == "TRACE"
    assert set(CATEGORIES) == {"control", "data", "fetch", "transport",
                               "relay"}


def test_log_spec_parses():
    assert parse_log_spec("debug, data=trace,control=WARNING") == (
        logging.DEBUG, {"aiomoqt.data": TRACE,
                        "aiomoqt.control": logging.WARNING})
    assert parse_log_spec("aiomoqt.protocol=error") == (
        None, {"aiomoqt.protocol": logging.ERROR})


@pytest.mark.parametrize("spec,needle", [
    ("data=loud", "unknown log level 'loud'"),
    ("loud", "unknown log level 'loud'"),
    ("media=debug", "unknown log category 'media'"),
    ("=debug", "no category"),
])
def test_bad_log_specs_name_the_problem(spec, needle):
    with pytest.raises(ValueError, match=needle):
        parse_log_spec(spec)


def _parser():
    p = cli.make_parser("t")
    p.add_argument("-d", "--debug", action="store_true")
    cli.add_logging(p)
    return p


@contextlib.contextmanager
def _restored():
    saved, saved_per = get_log_level(), get_logger_levels()
    try:
        yield
    finally:
        set_logger_levels(saved_per)
        set_log_level(logging.INFO if saved is None else saved)


def test_cli_flags_set_levels(monkeypatch):
    monkeypatch.delenv("AIOMOQT_LOG", raising=False)
    p = _parser()
    with _restored():
        assert cli.apply_logging(p.parse_args([])) == logging.WARNING
        assert cli.apply_logging(p.parse_args(["-d"])) == logging.DEBUG
        args = p.parse_args(["-d", "--log-level", "TRACE",
                             "--log", "data=info", "--log", "fetch=debug"])
        assert cli.apply_logging(args) == TRACE
        assert get_logger_levels() == {"aiomoqt.data": logging.INFO,
                                       "aiomoqt.fetch": logging.DEBUG}
        assert get_logger("aiomoqt.data").level == logging.INFO
        assert get_logger("aiomoqt.protocol").level == TRACE


@pytest.mark.parametrize("argv", [["--log-level", "verbose"],
                                  ["--log", "nosuch=debug"],
                                  ["--log", "data"]])
def test_bad_cli_flags_error(argv, capsys):
    with pytest.raises(SystemExit):
        _parser().parse_args(argv)
    assert "error:" in capsys.readouterr().err


def test_env_spec_applies_and_flags_win(monkeypatch):
    monkeypatch.setenv("AIOMOQT_LOG", "debug,relay=trace,data=error")
    p = _parser()
    with _restored():
        assert cli.apply_logging(p.parse_args([])) == logging.DEBUG
        assert get_logger_levels() == {"aiomoqt.relay": TRACE,
                                       "aiomoqt.data": logging.ERROR}
        args = p.parse_args(["--log-level", "info", "--log", "data=debug"])
        assert cli.apply_logging(args) == logging.INFO
        assert get_logger_levels() == {"aiomoqt.relay": TRACE,
                                       "aiomoqt.data": logging.DEBUG}


def test_bad_env_spec_errors(monkeypatch):
    monkeypatch.setenv("AIOMOQT_LOG", "data=noisy")
    with _restored(), pytest.raises(SystemExit, match="AIOMOQT_LOG"):
        cli.apply_logging(_parser().parse_args([]))


def _import_with_env(spec):
    code = ("import logging, aiomoqt.utils.logger as L; "
            "print(L.get_log_level(), L.get_logger_levels(), "
            "L.get_logger('aiomoqt.data').level)")
    env = dict(os.environ, AIOMOQT_LOG=spec)
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=60)


def test_env_spec_applies_at_import():
    out = _import_with_env("debug,data=trace")
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["10", "{'aiomoqt.data':", "5}", "5"]


def test_bad_env_spec_at_import_warns_and_is_ignored():
    out = _import_with_env("data=noisy")
    assert out.returncode == 0, out.stderr
    assert "AIOMOQT_LOG ignored" in out.stderr
    assert out.stdout.split() == ["None", "{}", "20"]


def test_set_log_level_keeps_category_levels():
    with _restored():
        set_logger_levels({"fetch": "trace"})
        set_log_level(logging.WARNING)
        assert get_logger("aiomoqt.fetch").level == TRACE
        assert get_logger("aiomoqt.protocol").level == logging.WARNING
        assert set_log_level("debug") == "debug"
        assert get_log_level() == logging.DEBUG


def test_step_log_level_cycles():
    with _restored():
        set_log_level(logging.INFO)
        assert [step_log_level() for _ in range(4)] == [
            logging.DEBUG, TRACE, logging.INFO, logging.DEBUG]
