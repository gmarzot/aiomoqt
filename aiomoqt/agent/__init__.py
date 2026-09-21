"""Declarative, agent-facing API over MoQT.

Specs are plain dataclasses that round-trip through JSON, so a model can
emit one and a harness can persist it. Nothing here imports the MCP SDK;
the MCP server lives in aiomoqt.mcp behind the `mcp` extra.

Provisional through 0.12.x: names and fields may change without a
deprecation cycle until the surface settles.
"""
from __future__ import annotations

from .errors import AgentError, SpecError
from .spec import (
    DECODES,
    GROUP_ORDERS,
    MAPPINGS,
    ON_FULL,
    SPEC_VERSION,
    START_MODES,
    Choices,
    Doc,
    FetchSpec,
    Priority,
    PublishSpec,
    Range,
    StartAt,
    SubscribeSpec,
    TrackRef,
)

__all__ = [
    "AgentError",
    "SpecError",
    "SPEC_VERSION",
    "TrackRef",
    "StartAt",
    "FetchSpec",
    "Priority",
    "SubscribeSpec",
    "PublishSpec",
    "Choices",
    "Doc",
    "Range",
    "START_MODES",
    "GROUP_ORDERS",
    "ON_FULL",
    "MAPPINGS",
    "DECODES",
]
