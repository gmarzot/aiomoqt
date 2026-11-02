"""aiomoqt logging: module and category loggers, and a TRACE level.

Levels, highest first: ERROR, WARNING, INFO, DEBUG, TRACE. TRACE (5) holds
per-object, per-chunk, per-datagram and wire-hex detail. It is registered
with logging.addLevelName only: no Logger method is added and no other
library's logging changes. Emit it as ``logger.log(TRACE, ...)``, behind
``logger.isEnabledFor(TRACE)`` on hot paths.

Category loggers cut across modules (names are stable):

  aiomoqt.control    control messages: sent, parsed, handled; requests
  aiomoqt.data       subgroup streams and datagrams, objects in and out
  aiomoqt.fetch      FETCH streams and their objects
  aiomoqt.transport  QUIC/WebTransport events, stream ops, ALPN
  aiomoqt.relay      aiomoqt-relay (moq_interop_relay)

Everything else logs to its module logger (aiomoqt.protocol, ...).

A log spec is a comma list of LEVEL (every aiomoqt logger) and CAT=LEVEL
(one category, or a full aiomoqt.* logger name), e.g. ``info,data=trace``.
The AIOMOQT_LOG environment variable takes a spec; tools take
``--log-level`` and ``--log``.
"""
import logging
import os
import sys
import warnings
from contextvars import ContextVar
from logging import getLevelName
from typing import Any, Dict, Mapping, Optional, Tuple

TRACE = 5
if getLevelName(TRACE) == f"Level {TRACE}":
    logging.addLevelName(TRACE, "TRACE")

LEVELS: Dict[str, int] = {
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": TRACE,
}

CATEGORIES: Tuple[str, ...] = ("control", "data", "fetch", "transport",
                               "relay")

ENV_VAR = "AIOMOQT_LOG"

# Cache to store created loggers
_loggers: Dict[str, logging.Logger] = {}

_level = None
_handler = None
# Per-logger levels from a spec, by full logger name.
_overrides: Dict[str, int] = {}

# True while handling a session whose records below WARNING are DEBUG
# records (MOQTServer quiet_peers).
quiet_session: ContextVar[bool] = ContextVar("aiomoqt_quiet_session",
                                             default=False)


class _QuietSessionFilter(logging.Filter):
    """Demote a quiet session's records below WARNING to DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (logging.DEBUG < record.levelno < logging.WARNING
                and quiet_session.get()):
            record.levelno, record.levelname = logging.DEBUG, "DEBUG"
            logger = _loggers.get(record.name)
            return logger is not None and logger.isEnabledFor(logging.DEBUG)
        return True


_quiet_filter = _QuietSessionFilter()


def parse_level(name: Any) -> int:
    """A level name from LEVELS (any case), or an int."""
    if isinstance(name, int):
        return name
    level = LEVELS.get(str(name).strip().lower())
    if level is None:
        raise ValueError(f"unknown log level {name!r}; "
                         f"use one of {', '.join(LEVELS)}")
    return level


def _logger_name(cat: str) -> str:
    if cat in CATEGORIES:
        return f"aiomoqt.{cat}"
    if cat.startswith("aiomoqt."):
        return cat
    raise ValueError(f"unknown log category {cat!r}; use one of "
                     f"{', '.join(CATEGORIES)} or an aiomoqt.* logger name")


def parse_log_spec(spec: str) -> Tuple[Optional[int], Dict[str, int]]:
    """Parse ``LEVEL`` / ``CAT=LEVEL`` entries: (level, {logger: level})."""
    level, per = None, {}
    for entry in (e.strip() for e in spec.split(",")):
        if not entry:
            continue
        if "=" in entry:
            cat, _, value = (s.strip() for s in entry.partition("="))
            if not cat:
                raise ValueError(f"log spec entry {entry!r}: no category")
            per[_logger_name(cat)] = parse_level(value)
        else:
            level = parse_level(entry)
    return level, per


def _effective(name: str) -> int:
    return _overrides.get(name, logging.INFO if _level is None else _level)


def _apply() -> None:
    for name, logger in _loggers.items():
        logger.setLevel(_effective(name))


def get_log_level() -> Optional[int]:
    """The level set_log_level() last set, None if never set."""
    return _level


def set_log_level(level: Any = None) -> Any:
    """Set the level of every aiomoqt logger without a per-logger level
    from a spec. Takes an int or a LEVELS name."""
    global _level
    _level = None if level is None else parse_level(level)
    _apply()
    return level


def get_logger_levels() -> Dict[str, int]:
    """Per-logger levels from a spec, by full logger name."""
    return dict(_overrides)


def set_logger_levels(levels: Mapping[str, Any]) -> None:
    """Replace the per-logger levels; keys are categories or aiomoqt.*
    logger names."""
    _overrides.clear()
    for cat, level in levels.items():
        _overrides[_logger_name(cat)] = parse_level(level)
    _apply()


def configure_logging(level: Any = None, spec: Optional[str] = None,
                      default: int = logging.INFO) -> int:
    """Set levels from a tool's flags and AIOMOQT_LOG. The global level is
    the first of level, spec's bare LEVEL, the env's, default; spec's
    CAT=LEVEL entries override the env's. Raises ValueError naming the
    bad input. Returns the global level."""
    env_level, per = None, {}
    env = os.environ.get(ENV_VAR)
    if env:
        try:
            env_level, per = parse_log_spec(env)
        except ValueError as e:
            raise ValueError(f"{ENV_VAR}={env!r}: {e}") from None
    spec_level = None
    if spec:
        spec_level, spec_per = parse_log_spec(spec)
        per.update(spec_per)
    chosen = next((v for v in (level, spec_level, env_level)
                   if v is not None), default)
    _overrides.clear()
    _overrides.update(per)
    set_log_level(chosen)
    return _level


def step_log_level() -> int:
    """INFO -> DEBUG -> TRACE -> INFO; any other level steps to DEBUG."""
    cur = logging.INFO if _level is None else _level
    nxt = {logging.DEBUG: TRACE, TRACE: logging.INFO}.get(cur, logging.DEBUG)
    set_log_level(nxt)
    return nxt


def set_log_handler(handler: logging.Handler) -> None:
    """Send every aiomoqt logger, existing and future, to handler."""
    global _handler
    old = _handler
    _handler = handler
    for logger in _loggers.values():
        if old is not None:
            logger.removeHandler(old)
        if handler not in logger.handlers:
            logger.addHandler(handler)


def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    """Get a logger with consistent formatting."""
    global _level, _handler

    if name in _loggers:
        logger = _loggers[name]
    else:
        logger = logging.getLogger(name)
        _loggers[name] = logger

    if not logger.handlers:  # Only add handler if none exists
        if _handler is None:
            handler = logging.StreamHandler(sys.stderr)
            formatter = logging.Formatter(
                fmt='%(asctime)s.%(msecs)03d %(levelname)-5s: %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            handler.setFormatter(formatter)
            _handler = handler

        logger.addHandler(_handler)
        # Don't bubble to root — root may have its own StreamHandler
        # (e.g. multiprocessing workers calling logging.basicConfig)
        # which would emit each message twice.
        logger.propagate = False
    if _quiet_filter not in logger.filters:
        logger.addFilter(_quiet_filter)

    level = _effective(name)
    logger.setLevel(level)
    logger.debug(f"setLevel: level:{getLevelName(level)} name: {name} ")

    return logger


def get_category_logger(cat: str) -> logging.Logger:
    """The logger of one of CATEGORIES."""
    if cat not in CATEGORIES:
        raise ValueError(f"unknown log category {cat!r}; use one of "
                         f"{', '.join(CATEGORIES)}")
    return get_logger(f"aiomoqt.{cat}")


def _apply_env() -> None:
    global _level
    spec = os.environ.get(ENV_VAR)
    if not spec:
        return
    try:
        level, per = parse_log_spec(spec)
    except ValueError as e:
        warnings.warn(f"{ENV_VAR} ignored: {e}", stacklevel=2)
        return
    if level is not None:
        _level = level
    _overrides.update(per)


_apply_env()


def class_name(obj: Any) -> str:
    if isinstance(obj, type):
        return obj.__name__
    else:
        return obj.__class__.__name__
