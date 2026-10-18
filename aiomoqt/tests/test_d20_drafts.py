"""Drafts 20 and 22: profiles, the default offer, fail-closed lookups; no
odd-numbered draft is supported."""
import dataclasses

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.server import MOQTServer
from aiomoqt.types import (
    MOQT_DEFAULT_DRAFTS, MOQT_VERSIONS, MOQTDraft, moqt_alpn_for_version,
    moqt_version_from_alpn, normalize_supported_drafts, parse_draft_spec,
    wire_control_type,
)


@pytest.mark.parametrize("draft", [15, 17, 19, 21, 23, 0xff000013, 0xff000015])
def test_profile_for_an_unknown_draft_fails_closed(draft):
    with pytest.raises(ValueError):
        profile_for(draft)


@pytest.mark.parametrize("draft", [15, 17, 19, 21, 23])
def test_wire_control_type_for_an_unknown_draft_fails_closed(draft):
    # PUBLISH_OK used to fall back to 0x1E, reserved from d20.
    with pytest.raises(ValueError):
        wire_control_type(draft, 0x1E)


def test_the_default_offer_is_every_even_draft_newest_first():
    assert MOQT_DEFAULT_DRAFTS == (22, 20, 18, 16, 14)
    assert normalize_supported_drafts(None) == [22, 20, 18, 16, 14]
    assert MOQTClient("h", 1).supported_drafts == [22, 20, 18, 16, 14]
    assert MOQT_VERSIONS == [0xff00000e, 0xff000010]


async def test_a_server_accepts_the_default_offer():
    assert MOQTServer("h", 1, "c", "k").supported_drafts == [22, 20, 18, 16, 14]
    s = MOQTServer("h", 1, "c", "k", supported_drafts=[22, 20, 18])
    assert s.supported_drafts == [22, 20, 18]


def test_draft_21_is_rejected():
    assert 21 not in {int(d) for d in MOQTDraft}
    for value in (21, [22, 21, 20]):
        with pytest.raises(ValueError, match="not supported"):
            normalize_supported_drafts(value)
        with pytest.raises(ValueError):
            MOQTClient("h", 1, supported_drafts=value)
    # parse_draft_spec only parses; the session constructors reject.
    for spec in ("21", "draft-21", "22,21,20"):
        with pytest.raises(ValueError, match="not supported"):
            normalize_supported_drafts(parse_draft_spec(spec))
    assert "moqt-21" not in [moqt_alpn_for_version(d) for d in MOQTDraft]


@pytest.mark.parametrize("draft", [20, 22])
def test_d20_and_d22_are_accepted_when_named(draft):
    assert int(MOQTDraft(draft)) == draft
    assert MOQTClient("h", 1, supported_drafts=draft).supported_drafts \
        == [draft]
    assert moqt_alpn_for_version(draft) == f"moqt-{draft}"
    assert moqt_version_from_alpn(f"moqt-{draft}") == 0xff000000 + draft


# The d20 columns that differ from d18.
_D20_SWITCHES = dict(
    param_table=20, error_table=20, publish_ok_expires_only=True,
    publish_done_subscription_ended=False, goaway_request_id=False,
    fetch_location_filter=True, fetch_timed_out_range=True,
    fetch_object_status=False, truncated_request_closes=True,
    request_fin_cancels_discovery=False, fill_parameters_counted=True,
    namespace_scoped=frozenset(
        {"SubscribeNamespace", "PublishNamespace", "SubscribeTracks"}))


def test_d22_only_flips_its_switches_on_the_d20_profile():
    d18, d20, d22 = (profile_for(d) for d in (18, 20, 22))
    assert dataclasses.replace(d18, draft=20, **_D20_SWITCHES) == d20
    assert dataclasses.replace(
        d22, draft=20, param_table=20, location_filter_typed=False,
        relay_forwards_publish_namespace=True) == d20
    assert (d18.location_filter_typed, d18.relay_forwards_publish_namespace) \
        == (False, True)
    assert (d22.location_filter_typed, d22.relay_forwards_publish_namespace) \
        == (True, False)
    for prof in (d20, d22):
        assert prof.vi64 and prof.control_uni_pair
        assert not prof.setup_carries_versions
        assert not prof.reply_has_request_id
