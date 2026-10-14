"""Agent-layer exceptions."""
from __future__ import annotations


class AgentError(Exception):
    """Base for every aiomoqt.agent failure."""


class SpecError(AgentError, ValueError):
    """A spec could not be built from the supplied data.

    Subclasses ValueError so callers that already catch ValueError around
    config parsing keep working.
    """


class Unsupported(AgentError, NotImplementedError):
    """A spec sets a field to a value this runtime does not implement,
    which it would otherwise ignore."""


class DecodeError(AgentError, ValueError):
    """An object's payload does not decode as its reader's `decode` asks.
    The raw bytes stay available as `Obj.payload`."""
