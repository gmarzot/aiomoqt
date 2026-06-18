"""draft-18 message classes (Phase 2 skeleton).

This package holds the draft-18-specific message encodings, kept separate
from the d14/d16 classes in the parent package because d18 reworks the wire
beyond a per-field toggle: type renumbering (SUBSCRIBE_NAMESPACE 0x11->0x50,
SUBSCRIBE_TRACKS 0x51 new, PUBLISH_BLOCKED 0xF new, SETUP 0x2F00), replies
dropping their Request-ID field (same-stream targeting), removed messages
(UNSUBSCRIBE, FETCH_CANCEL, ...), per-type parameter value encodings, and
the vi64 integer codec.

d18 classes follow the same required keyword-only ``draft=`` discipline as
the d14/d16 classes (Phase 0). They are registered into
``CONTROL_REGISTRY[MOQTDraft.DRAFT_18]`` and selected only when a session's
draft is 18 — which the session API gates behind ``AIOMOQT_ENABLE_D18``
until the d18 wire is complete (Phases 3/5/6).
"""
from .session_setup import Setup

__all__ = ['Setup']
