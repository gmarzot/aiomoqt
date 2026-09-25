"""Agent-layer exceptions."""
from __future__ import annotations


class AgentError(Exception):
    """Base for every aiomoqt.agent failure."""


class SpecError(AgentError, ValueError):
    """A spec could not be built from the supplied data.

    Subclasses ValueError so callers that already catch ValueError around
    config parsing keep working.
    """
