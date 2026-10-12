"""Drafts 20, 21 and 22: profiles, opt-in offer, fail-closed lookups."""
import dataclasses

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.server import MOQTServer
from aiomoqt.types import (
    MOQT_DEFAULT_DRAFTS, MOQT_VERSIONS, MOQTDraft, moqt_alpn_for_version,
    moqt_version_from_alpn, normalize_supported_drafts, wire_control_type,
)


@pytest.mark.parametrize("draft", [15, 17, 19, 23, 0xff000013])
def test_profile_for_an_unknown_draft_fails_closed(draft):
    with pytest.raises(ValueError):
        profile_for(draft)


@pytest.mark.parametrize("draft", [15, 17, 19, 23])
def test_wire_control_type_for_an_unknown_draft_fails_closed(draft):
    # PUBLISH_OK used to fall back to 0x1E, reserved from d20.
    with pytest.raises(ValueError):
        wire_control_type(draft, 0x1E)


def test_the_default_offer_is_unchanged():
    assert MOQT_DEFAULT_DRAFTS == (18, 16, 14)
    assert normalize_supported_drafts(None) == [18, 16, 14]
    assert MOQTClient("h", 1).supported_drafts == [18, 16, 14]
    assert MOQT_VERSIONS == [0xff00000e, 0xff000010]


async def test_a_server_accepts_d20_to_d22_only_when_configured():
    assert MOQTServer("h", 1, "c", "k").supported_drafts == [18, 16, 14]
    s = MOQTServer("h", 1, "c", "k", supported_drafts=[22, 21, 20, 18])
    assert s.supported_drafts == [22, 21, 20, 18]


@pytest.mark.parametrize("draft", [20, 21, 22])
def test_d20_to_d22_are_accepted_when_named(draft):
    assert int(MOQTDraft(draft)) == draft
    assert MOQTClient("h", 1, supported_drafts=draft).supported_drafts \
        == [draft]
    assert moqt_alpn_for_version(draft) == f"moqt-{draft}"
    assert moqt_version_from_alpn(f"moqt-{draft}") == 0xff000000 + draft


def test_d20_and_d21_share_a_profile_and_d22_only_flips_its_switches():
    d18, d20, d21, d22 = (profile_for(d) for d in (18, 20, 21, 22))
    assert dataclasses.replace(d20, draft=18) == d18
    assert dataclasses.replace(d21, draft=20) == d20
    assert dataclasses.replace(
        d22, draft=20, location_filter_typed=False,
        relay_forwards_publish_namespace=True) == d20
    assert (d18.location_filter_typed, d18.relay_forwards_publish_namespace) \
        == (False, True)
    assert (d22.location_filter_typed, d22.relay_forwards_publish_namespace) \
        == (True, False)
    for prof in (d20, d21, d22):
        assert prof.vi64 and prof.control_uni_pair
        assert not prof.setup_carries_versions
        assert not prof.reply_has_request_id
