import logging
import sys
from contextvars import ContextVar
from logging import getLevelName
from typing import Optional, Dict, Any

# Cache to store created loggers
_loggers: Dict[str, logging.Logger] = {}

_level = None
_handler = None

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


def get_log_level() -> Optional[int]:
    """The level set_log_level() last set, None if never set."""
    return _level


def set_log_level(level: any = None) -> any:
    """Set configured log level."""
    global _level
    _level = level
    for logger in _loggers.values():
        logger.setLevel(level)

    return level

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

    if _level is not None:
        level = _level
    else:
        level = logging.INFO
    logger.setLevel(level)
    logger.debug(f"setLevel: level:{getLevelName(level)} name: {name} ")

    return logger

def class_name(obj: Any) -> str:
    if isinstance(obj, type):
        return obj.__name__
    else:
        return obj.__class__.__name__
