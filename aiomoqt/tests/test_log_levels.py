"""TRACE below DEBUG, category loggers and log specs: DEBUG stays bounded
however many objects flow, TRACE shows each one, and a category spec
turns on that category alone. Raw QUIC and WebTransport, two tracks."""
import asyncio
import contextlib
import logging
import os
import subprocess
import sys

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType
from aiomoqt.utils import cli
from aiomoqt.utils.logger import (
    CATEGORIES, TRACE, _loggers, get_log_level, get_logger,
    get_logger_levels, parse_log_spec, set_log_level, set_logger_levels,
    step_log_level,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 18450
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(1)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def at(self, level):
        return [r for r in self.records if r.levelno == level]


@contextlib.contextmanager
def _captured(level, per=None):
    """Every aiomoqt record at or above its logger's level."""
    records = _Records()
    saved, saved_per = get_log_level(), get_logger_levels()
    set_log_level(level)
    set_logger_levels(per or {})
    loggers = list(_loggers.values())
    for lg in loggers:
        lg.addHandler(records)
    try:
        yield records
    finally:
        for lg in loggers:
            lg.removeHandler(records)
        set_logger_levels(saved_per)
        set_log_level(logging.INFO if saved is None else saved)


class _Finite(PublishedTrack):
    """`count` objects in one group: one stream per track."""

    count = 0

    async def produce(self, out):
        for oid in range(self.count):
            await out.write(0, oid, bytes(self.object_size),
                            group_start=oid == 0)


async def _two_tracks(port, use_quic, count):
    """PUBLISH two tracks of `count` objects; returns objects received
    per track."""
    got = {}
    publishes = asyncio.Queue()

    async def on_publish(session, msg):
        await publishes.put((session, msg))

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH, on_publish)
    quic_server = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=18)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            tracks = []
            for name in ("a", "b"):
                track = _Finite(session, namespace="log/levels",
                                trackname=name, object_size=100,
                                group_size=count + 1, num_subgroups=1)
                track.count = count
                tracks.append(track)
                await track.publish(forward=0)
            for _ in tracks:
                rx, msg = await asyncio.wait_for(publishes.get(), 5)
                alias = msg.track_alias
                got[alias] = 0

                def _on_object(obj, *_a, alias=alias):
                    if obj.payload:
                        got[alias] += 1
                rx.register_object_handler(alias, _on_object)
                rx.publish_ok(msg, forward=1)
            async with asyncio.timeout(10):
                while len(got) < 2 or min(got.values()) < count:
                    await asyncio.sleep(0.02)
    finally:
        quic_server.close()
    return got


async def _debug_lines(port, use_quic, count):
    with _captured(logging.DEBUG) as records:
        got = await _two_tracks(port, use_quic, count)
    assert sorted(got.values()) == [count, count]
    assert not records.at(TRACE)
    return [r.getMessage() for r in records.at(logging.DEBUG)]


@requires_certs
@_TRANSPORTS
async def test_debug_lines_do_not_grow_with_objects(use_quic):
    port = _BASE_PORT + 2 * (not use_quic)
    few = await _debug_lines(port, use_quic, 10)
    many = await _debug_lines(port + 1, use_quic, 200)
    assert few and many
    assert len(many) <= len(few) + 10, (len(few), len(many), many)
    assert not [m for m in many if "object 0." in m]


@requires_certs
@_TRANSPORTS
async def test_trace_logs_each_object(use_quic):
    count = 30
    with _captured(TRACE) as records:
        got = await _two_tracks(_BASE_PORT + 4 + (not use_quic), use_quic,
                                count)
    assert sorted(got.values()) == [count, count]
    trace = [r for r in records.at(TRACE) if r.name == "aiomoqt.data"]
    received = [r for r in trace if "): object 0." in r.getMessage()
                and r.getMessage().endswith("status=0")]
    sent = [r for r in trace if r.getMessage().startswith(
        "MOQT send: object 0.")]
    assert len(received) == 2 * count
    assert len(sent) == 2 * count


@requires_certs
@_TRANSPORTS
async def test_a_category_spec_enables_that_category_only(use_quic):
    _, per = parse_log_spec("data=trace")
    with _captured(logging.INFO, per) as records:
        got = await _two_tracks(_BASE_PORT + 6 + (not use_quic), use_quic,
                                10)
    assert sorted(got.values()) == [10, 10]
    below_info = [r for r in records.records if r.levelno < logging.INFO]
    assert below_info
    assert {r.name for r in below_info} == {"aiomoqt.data"}


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
