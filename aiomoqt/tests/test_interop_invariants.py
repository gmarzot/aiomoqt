"""Regression-lockdown tests for wire-format invariants that 0.9.x
broke without loopback detecting it.

Loopback can't catch spec-violation bugs that both sides commit
symmetrically (path strip, missing WT-Available-Protocols, etc.).
This file asserts the specific shape invariants where the bugs
manifested, so they can't drift back in without breaking a test.

See memory/project_regression_d14d16_wt_rawquic.md for the full
story; memory/feedback_release_gate_basic_interop.md for the
release-gate policy.
"""
from __future__ import annotations

import pytest

from aiomoqt.utils.url import parse_relay_url
from aiomoqt.types import (
    MOQT_VERSION_DRAFT14, MOQT_VERSION_DRAFT16, MOQT_VERSIONS,
    moqt_version_from_draft, moqt_alpn_for_version,
)


# =====================================================================
# URL path normalization
# =====================================================================

class TestUrlPathLeadingSlash:
    """url.py stripped the leading `/` from the path; qh3 silently
    re-prepended it on the wire (in 0.8.x), aiopquic doesn't (0.9.x).
    Result: HTTP/3 `:path: moq-relay` instead of `:path: /moq-relay`
    — every mvfst/moxygen server rejects the CONNECT.

    Lock down: parser must produce a `path` that, when handed to the
    transport, results in a valid HTTP/3 `:path` header (i.e., starts
    with `/`).
    """

    def test_https_with_path_keeps_leading_slash(self):
        r = parse_relay_url("https://host.example.net/moq-relay")
        assert r.path == "/moq-relay", \
            f"path stripped leading /; got {r.path!r}"

    def test_https_with_trailing_slash_dropped(self):
        # Trailing slash is decorative; should be normalized away.
        r = parse_relay_url("https://host.example.net/moq-relay/")
        assert r.path == "/moq-relay"

    def test_https_without_path_uses_default(self):
        # No path component → default_path. Empty default stays empty
        # (aiopquic normalizes to "/" at the WT layer).
        r = parse_relay_url("https://host.example.net")
        assert r.path == "" or r.path == "/"

    def test_https_explicit_default_path_preserved(self):
        # Caller-supplied default_path gets the same /-normalization.
        r = parse_relay_url("https://host.example.net",
                            default_path="moq-relay")
        assert r.path == "/moq-relay"


# =====================================================================
# Draft-number API contract
# =====================================================================

class TestDraftNumberApi:
    """High-level aiomoqt APIs accept the integer draft number
    (14, 16, ...). The wire-encoded IETF version code
    (0xff000010 etc.) is wire legacy — never enters the public API.
    """

    def test_known_drafts_normalize_to_wire_code(self):
        assert moqt_version_from_draft(14) == 0xff00000e
        assert moqt_version_from_draft(16) == 0xff000010

    def test_unsupported_draft_raises_clearly(self):
        # aiomoqt only knows d14 and d16 today; d15 / unrelated must reject
        # with a message identifying the supported set, not silently fail.
        with pytest.raises(ValueError, match="not supported"):
            moqt_version_from_draft(15)
        with pytest.raises(ValueError, match="not supported"):
            moqt_version_from_draft(99)

    def test_hex_wire_form_is_not_accepted_as_draft(self):
        # The wire form (0xff000010) is NOT a valid draft number arg.
        # Callers passing it are buggy; reject loudly.
        with pytest.raises(ValueError):
            moqt_version_from_draft(0xff000010)

    def test_negative_or_non_int_rejected(self):
        with pytest.raises(ValueError):
            moqt_version_from_draft(-1)
        with pytest.raises(ValueError):
            moqt_version_from_draft("16")  # type: ignore[arg-type]


# =====================================================================
# ALPN derivation by draft
# =====================================================================

class TestAlpnByDraft:
    """ALPN convention from moq-transport-16 §3.1:
    - pre-d15: legacy "moq-00"
    - d15+: "moqt-NN"
    Used both for raw-QUIC ALPN handshake and for WT-Available-Protocols.
    """

    def test_d14_uses_legacy_moq00(self):
        # d14 raw-QUIC uses moq-00 ALPN; CLIENT_SETUP carries versions.
        assert moqt_alpn_for_version(MOQT_VERSION_DRAFT14) == "moq-00"

    def test_d16_uses_moqt16(self):
        # d16+ raw-QUIC uses moqt-NN ALPN; version negotiated via ALPN.
        assert moqt_alpn_for_version(MOQT_VERSION_DRAFT16) == "moqt-16"


# =====================================================================
# MOQTClient public API
# =====================================================================

class TestMoqtClientDraftValidation:
    """MOQTClient.supported_drafts is part of the public API contract;
    it accepts draft NUMBERS (14, 16, ...) — a single int or a list —
    validates against the supported set, and normalizes to a non-empty
    list of draft numbers (newest first). Drafts stay representation-
    independent ints; the IETF wire form is only used when building or
    parsing wire messages. Confusion between draft-number form and IETF
    wire form was the source of the 0.9.x silent setup failure.
    """

    def test_accepts_supported_draft_number(self):
        from aiomoqt.client import MOQTClient
        # Should not raise; a single int normalizes to a one-element list.
        c = MOQTClient("localhost", 4433, supported_drafts=14)
        assert c.supported_drafts == [14]
        c = MOQTClient("localhost", 4433, supported_drafts=16)
        assert c.supported_drafts == [16]

    def test_accepts_draft_list_normalized_newest_first(self):
        from aiomoqt.client import MOQTClient
        c = MOQTClient("localhost", 4433, supported_drafts=[14, 16])
        assert c.supported_drafts == [16, 14]
        # duplicates collapse
        c = MOQTClient("localhost", 4433, supported_drafts=[16, 16, 14])
        assert c.supported_drafts == [16, 14]

    def test_rejects_unsupported_draft_number(self):
        from aiomoqt.client import MOQTClient
        with pytest.raises(ValueError, match="not supported"):
            MOQTClient("localhost", 4433, supported_drafts=15)
        with pytest.raises(ValueError, match="not supported"):
            MOQTClient("localhost", 4433, supported_drafts=[16, 99])

    def test_rejects_hex_wire_form_as_draft_arg(self):
        # The 0.9.x bug: passing the wire form 0xff000010 as a draft
        # "worked" silently but produced inconsistent internal state.
        # Must now reject clearly.
        from aiomoqt.client import MOQTClient
        with pytest.raises(ValueError):
            MOQTClient("localhost", 4433, supported_drafts=0xff000010)

    def test_none_offers_all_drafts_newest_first(self):
        # No draft pinning is legal — aiomoqt offers all known drafts
        # (newest first) and the server picks via ALPN / negotiation.
        from aiomoqt.client import MOQTClient
        c = MOQTClient("localhost", 4433, supported_drafts=None)
        assert c.supported_drafts == [16, 14]


# =====================================================================
# MOQT_VERSIONS canonical set
# =====================================================================

class TestSupportedVersions:
    """The supported-versions set is what CLIENT_SETUP advertises in
    legacy (d14) mode. Lock the set so accidental removal breaks a
    test, not interop.
    """

    def test_d14_and_d16_both_present(self):
        assert MOQT_VERSION_DRAFT14 in MOQT_VERSIONS
        assert MOQT_VERSION_DRAFT16 in MOQT_VERSIONS

    def test_no_unknown_versions(self):
        # If a new entry lands without updating the matrix, tests for
        # the new draft against real relays must pass first.
        assert set(MOQT_VERSIONS) == {0xff00000e, 0xff000010}
