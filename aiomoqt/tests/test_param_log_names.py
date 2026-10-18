"""Logged messages name their parameters from the negotiated draft's table:
0x04 is MAX_CACHE_DURATION at d14/d16 and RENDEZVOUS_TIMEOUT from d18."""
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages import PublishNamespace, Subscribe
from aiomoqt.messages.params import param_name
from aiomoqt.protocol import _MOQTSessionMixin


def _msg():
    return PublishNamespace(request_id=1, namespace=(b"ns",),
                            parameters={0x02: 100, 0x04: 5000})


@pytest.mark.parametrize("draft", [14, 16])
def test_pre_d18_names(draft):
    text = _msg().describe(profile_for(draft))
    assert "MAX_CACHE_DURATION=5000" in text
    assert "DELIVERY_TIMEOUT=100" in text
    assert "RENDEZVOUS_TIMEOUT" not in text


@pytest.mark.parametrize("draft", [18, 20, 22])
def test_d18_on_names(draft):
    text = _msg().describe(profile_for(draft))
    assert "RENDEZVOUS_TIMEOUT=5000" in text
    assert "OBJECT_DELIVERY_TIMEOUT=100" in text
    assert "MAX_CACHE_DURATION" not in text


def test_d20_filter_names():
    assert param_name(0x21, profile_for(18)) == "SUBSCRIPTION_FILTER"
    assert param_name(0x21, profile_for(20)) == "LOCATION_FILTER"
    assert param_name(0x23, profile_for(22)) == "FILL_PARAMETERS"
    assert param_name(0x23, profile_for(18)) == "0x23"


def test_str_keeps_draft_free_names():
    assert "MAX_CACHE_DURATION=5000" in str(_msg())


def test_session_renders_at_its_draft():
    fake = SimpleNamespace(_profile=profile_for(18))
    msg = Subscribe(request_id=0, track_namespace=(b"ns",), track_name=b"t",
                    parameters={0x04: 7})
    text = _MOQTSessionMixin._msgstr(fake, msg)
    assert "RENDEZVOUS_TIMEOUT=7" in text
    assert _MOQTSessionMixin._msgstr(fake, None) == "None"
