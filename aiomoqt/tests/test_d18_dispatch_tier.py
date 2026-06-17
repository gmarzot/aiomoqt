"""draft-18 dispatch tier (Phase 2 scaffold).

Structural coverage only: the d18 version / profile / registry slots exist
and the AIOMOQT_ENABLE_D18 gate keeps d18 opt-in. The d18 wire (type
renumbering, Request-ID drops, vi64 data plane) lands in later phases, so
these tests do NOT assert d18 wire-correctness — only that the tier is
present and d14/d16 are unaffected.
"""
import pytest

from aiomoqt.types import (
    MOQTDraft, MOQT_VERSION_DRAFT18,
    moqt_version_from_draft, moqt_version_from_alpn, moqt_alpn_for_version,
)
from aiomoqt.context import PROFILES, profile_for, is_draft16_or_later
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.client import MOQTClient


def test_draft18_enum_and_version():
    assert MOQTDraft.DRAFT_18 == 18
    assert MOQT_VERSION_DRAFT18 == 0xff000012
    assert moqt_version_from_draft(18) == 0xff000012


def test_draft18_alpn_roundtrip():
    assert moqt_alpn_for_version(MOQT_VERSION_DRAFT18) == "moqt-18"
    assert moqt_version_from_alpn("moqt-18") == MOQT_VERSION_DRAFT18


def test_draft18_profile():
    prof = profile_for(18)
    assert prof.draft == 18
    # d18 negotiates out-of-band (ALPN/WT-Protocol), delta-coded params.
    assert prof.setup_carries_versions is False
    assert prof.params_delta_coded is True
    assert is_draft16_or_later(18) is True
    assert MOQTDraft.DRAFT_18 in PROFILES


def test_draft18_registry_slot_distinct():
    reg = _MOQTSessionMixin.CONTROL_REGISTRY
    assert MOQTDraft.DRAFT_18 in reg
    # Per-draft dicts are distinct objects — no shared mutable dispatch
    # state between a d16 and a d18 session in the same loop.
    assert reg[MOQTDraft.DRAFT_18] is not reg[MOQTDraft.DRAFT_16]


def test_d18_gated_off_by_default(monkeypatch):
    monkeypatch.delenv("AIOMOQT_ENABLE_D18", raising=False)
    with pytest.raises(ValueError, match="AIOMOQT_ENABLE_D18"):
        MOQTClient("localhost", 4433, supported_drafts=[18])
    with pytest.raises(ValueError, match="AIOMOQT_ENABLE_D18"):
        MOQTClient("localhost", 4433, draft_version=18)


def test_d18_gate_allows_when_enabled(monkeypatch):
    monkeypatch.setenv("AIOMOQT_ENABLE_D18", "1")
    c = MOQTClient("localhost", 4433, supported_drafts=[18, 16])
    assert c.supported_drafts == (18, 16)


def test_default_client_unaffected_by_gate(monkeypatch):
    monkeypatch.delenv("AIOMOQT_ENABLE_D18", raising=False)
    c = MOQTClient("localhost", 4433)  # default (16, 14), no d18
    assert 18 not in c.supported_drafts
