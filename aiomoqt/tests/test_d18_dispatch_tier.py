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
from aiomoqt.messages.base import MOQTMessage
from aiomoqt.utils.buffer import Buffer


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


def test_draft18_profile_wire_columns():
    # d18 forks the wire codec to vi64, runs control over a uni-stream pair,
    # and drops the Request ID from request replies.
    prof = profile_for(18)
    assert prof.varint == "vi64"
    assert prof.control_uni_pair is True
    assert prof.reply_has_request_id is False


def test_d14_d16_profile_wire_columns_unchanged():
    # d14/d16 keep RFC9000 varints, a single bidi control stream, and
    # Request-ID-bearing replies — Phase 3 must not perturb them.
    for d in (14, 16):
        prof = profile_for(d)
        assert prof.varint == "rfc9000"
        assert prof.control_uni_pair is False
        assert prof.reply_has_request_id is True


def test_draft18_registry_slot_distinct():
    reg = _MOQTSessionMixin.CONTROL_REGISTRY
    assert MOQTDraft.DRAFT_18 in reg
    # Per-draft dicts are distinct objects — no shared mutable dispatch
    # state between a d16 and a d18 session in the same loop.
    assert reg[MOQTDraft.DRAFT_18] is not reg[MOQTDraft.DRAFT_16]


def test_d18_registry_setup_entry():
    from aiomoqt.messages.d18 import Setup
    from aiomoqt.types import MOQTMessageType
    reg18 = _MOQTSessionMixin.CONTROL_REGISTRY[MOQTDraft.DRAFT_18]
    cls, handler = reg18[MOQTMessageType.SETUP]  # 0x2F00
    assert cls is Setup
    assert handler.__name__ == "_handle_d18_setup"
    # d18 reserves the d16 CLIENT_SETUP/SERVER_SETUP code points.
    assert MOQTMessageType.CLIENT_SETUP not in reg18
    assert MOQTMessageType.SERVER_SETUP not in reg18
    # d16 still has the classic setup pair, unchanged.
    reg16 = _MOQTSessionMixin.CONTROL_REGISTRY[MOQTDraft.DRAFT_16]
    assert MOQTMessageType.CLIENT_SETUP in reg16
    assert MOQTMessageType.SERVER_SETUP in reg16


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


def test_d18_setup_options_kvp_roundtrip():
    # d18 Setup Options are count-less delta-coded KVPs (Figure 2) to the
    # message Length: even Type -> varint value, odd Type -> length+bytes.
    opts = {0x04: 100, 0x07: b"aiomoqt"}  # MAX_AUTH_TOKEN_CACHE_SIZE, IMPL
    buf = Buffer(capacity=64)
    MOQTMessage._serialize_kvp_to_end(buf, opts, draft=18)
    n = buf.tell()
    raw = bytes(buf.data_slice(0, n))
    # No count prefix: the stream starts with the first delta key (0x04),
    # not a parameter count.
    assert raw[0] == 0x04
    rbuf = Buffer(data=raw)
    out = MOQTMessage._deserialize_kvp_to_end(rbuf, draft=18, buf_end=n)
    assert out == opts


def test_d18_kvp_uses_vi64_not_rfc9000():
    # Value 100 (>63) distinguishes the codecs: vi64 encodes it in 1 byte
    # (0x64), RFC9000 in 2 (0x40 0x64). draft=18 must take the vi64 path.
    buf = Buffer(capacity=16)
    MOQTMessage._serialize_kvp_to_end(buf, {0x02: 100}, draft=18)
    assert buf.tell() == 2  # key delta 0x02 (1B) + vi64 value 100 (1B)
    assert bytes(buf.data_slice(0, 2)) == bytes([0x02, 0x64])


def test_d18_setup_message_wire_form():
    from aiomoqt.messages.d18 import Setup
    from aiomoqt.types import MOQTMessageType
    msg = Setup(options={0x07: b"aiomoqt"})  # MOQT_IMPLEMENTATION
    assert msg.type == MOQTMessageType.SETUP == 0x2F00
    buf = msg.serialize(draft=18)
    raw = bytes(buf.data_slice(0, buf.tell()))
    # Type 0x2F00 as vi64 = AF 00, then a 16-bit big-endian Length.
    assert raw[0:2] == bytes([0xAF, 0x00])
    payload_len = (raw[2] << 8) | raw[3]
    assert payload_len == len(raw) - 4


def test_d18_subscribe_ok_omits_request_id():
    from aiomoqt.messages.subscribe import SubscribeOk
    from aiomoqt.types import MOQTMessageType
    kw = dict(request_id=7, track_alias=1, content_exists=0)
    d16 = bytes(SubscribeOk(**kw).serialize(draft=16).data)
    d18 = bytes(SubscribeOk(**kw).serialize(draft=18).data)
    # d18 drops the Request ID varint -> strictly shorter wire form.
    assert len(d18) < len(d16)
    # And on the wire there is no Request ID to read back.
    rbuf = Buffer(data=d18)
    assert rbuf.pull_uint_vi64() == MOQTMessageType.SUBSCRIBE_OK
    plen = rbuf.pull_uint16()
    end = rbuf.tell() + plen
    msg = SubscribeOk.deserialize(rbuf, draft=18, buf_end=end)
    assert msg.request_id is None  # absent on the wire; injected by stream
    assert msg.track_alias == 1


def test_d18_setup_message_roundtrip():
    from aiomoqt.messages.d18 import Setup
    opts = {0x04: 0, 0x07: b"aiomoqt/0.10"}
    raw = bytes(Setup(options=opts).serialize(draft=18).data)
    # Mirror the control dispatch loop: consume vi64 type + uint16 length,
    # then deserialize the payload bounded by buf_end.
    rbuf = Buffer(data=raw)
    assert rbuf.pull_uint_vi64() == 0x2F00
    plen = rbuf.pull_uint16()
    end = rbuf.tell() + plen
    out = Setup.deserialize(rbuf, draft=18, buf_end=end)
    assert out.options == opts
