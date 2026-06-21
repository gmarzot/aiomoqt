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


def test_d18_selectable_without_env_var(monkeypatch):
    # d18 is no longer gated behind AIOMOQT_ENABLE_D18 — explicit selection
    # works with no env var set.
    monkeypatch.delenv("AIOMOQT_ENABLE_D18", raising=False)
    c = MOQTClient("localhost", 4433, supported_drafts=[18, 16])
    assert c.supported_drafts == (18, 16)
    c2 = MOQTClient("localhost", 4433, draft_version=18)
    assert c2.supported_drafts == (18,)


def test_default_client_offers_d18_on_equal_footing(monkeypatch):
    # d18 ships on equal footing with d16/d14: the default offer is
    # (18, 16, 14), newest-first, so a peer that speaks d18 negotiates it
    # and others fall back gracefully.
    monkeypatch.delenv("AIOMOQT_ENABLE_D18", raising=False)
    c = MOQTClient("localhost", 4433)
    assert c.supported_drafts == (18, 16, 14)


def test_d18_setup_options_kvp_roundtrip():
    # d18 Setup Options are count-less delta-coded KVPs (Figure 2) to the
    # message Length: even Type -> varint value, odd Type -> length+bytes.
    opts = {0x04: 100, 0x07: b"aiomoqt"}  # MAX_AUTH_TOKEN_CACHE_SIZE, IMPL
    buf = Buffer(capacity=64)
    MOQTMessage._serialize_kvp_to_end(buf, opts, prof=profile_for(18))
    n = buf.tell()
    raw = bytes(buf.data_slice(0, n))
    # No count prefix: the stream starts with the first delta key (0x04),
    # not a parameter count.
    assert raw[0] == 0x04
    rbuf = Buffer(data=raw)
    out = MOQTMessage._deserialize_kvp_to_end(rbuf, prof=profile_for(18), buf_end=n)
    assert out == opts


def test_d18_kvp_uses_vi64_not_rfc9000():
    # Value 100 (>63) distinguishes the codecs: vi64 encodes it in 1 byte
    # (0x64), RFC9000 in 2 (0x40 0x64). d18 must take the vi64 path.
    buf = Buffer(capacity=16)
    MOQTMessage._serialize_kvp_to_end(buf, {0x02: 100}, prof=profile_for(18))
    assert buf.tell() == 2  # key delta 0x02 (1B) + vi64 value 100 (1B)
    assert bytes(buf.data_slice(0, 2)) == bytes([0x02, 0x64])


def test_d18_replies_omit_request_id():
    from aiomoqt.messages.request import RequestOk, RequestError, RequestUpdate

    # RequestOk: request_id present in d16, absent in d18.
    ok16 = bytes(RequestOk(request_id=9, parameters={}).serialize(prof=profile_for(16)).data)
    ok18 = bytes(RequestOk(request_id=9, parameters={}).serialize(prof=profile_for(18)).data)
    assert len(ok18) < len(ok16)

    # RequestError: same.
    e16 = bytes(RequestError(request_id=9, error_code=1, reason="x").serialize(prof=profile_for(16)).data)
    e18 = bytes(RequestError(request_id=9, error_code=1, reason="x").serialize(prof=profile_for(18)).data)
    assert len(e18) < len(e16)

    # RequestUpdate carries BOTH Request ID and Existing Request ID in d18
    # (confirmed against the mvfst/moxygen relay); d18 just uses vi64.
    u18 = bytes(RequestUpdate(request_id=9, existing_request_id=7, parameters={}).serialize(prof=profile_for(18)).data)
    ru = RequestUpdate.deserialize(Buffer(data=u18[3:]), prof=profile_for(18), buf_end=len(u18) - 3)
    assert ru.request_id == 9
    assert ru.existing_request_id == 7


def test_d18_typed_param_values_are_uint8():
    # d18 encodes FORWARD/SUBSCRIBER_PRIORITY/GROUP_ORDER as fixed uint8,
    # not vi64. The divergence shows at value >= 64: vi64(128) = 2 bytes
    # (0x40 0x80), uint8(128) = 1 byte (0x80). Default priority is 128.
    from aiomoqt.types import ParamType
    buf = Buffer(capacity=16)
    MOQTMessage._serialize_params(
        buf, {ParamType.SUBSCRIBER_PRIORITY: 128}, prof=profile_for(18))
    raw = bytes(buf.data_slice(0, buf.tell()))
    # count(1) + delta-key 0x20 + uint8 value 0x80 = 3 bytes.
    assert raw == bytes([0x01, 0x20, 0x80])
    out = MOQTMessage._deserialize_params(
        Buffer(data=raw), prof=profile_for(18), buf_end=len(raw))
    assert out == {ParamType.SUBSCRIBER_PRIORITY: 128}


def test_d16_param_values_stay_varint():
    # d16 keeps RFC9000 varints for the same params: 128 -> 0x40 0x80.
    from aiomoqt.types import ParamType
    buf = Buffer(capacity=16)
    MOQTMessage._serialize_params(
        buf, {ParamType.SUBSCRIBER_PRIORITY: 128}, prof=profile_for(16))
    raw = bytes(buf.data_slice(0, buf.tell()))
    # count(1) + key 0x20 + 2-byte varint 0x40 0x80 = 4 bytes.
    assert raw == bytes([0x01, 0x20, 0x40, 0x80])
    out = MOQTMessage._deserialize_params(
        Buffer(data=raw), prof=profile_for(16), buf_end=len(raw))
    assert out == {ParamType.SUBSCRIBER_PRIORITY: 128}


def test_d18_typed_params_roundtrip_all_three():
    # FORWARD 0x10, SUBSCRIBER_PRIORITY 0x20, GROUP_ORDER 0x22 — all uint8.
    from aiomoqt.types import ParamType
    params = {
        ParamType.FORWARD: 1,
        ParamType.SUBSCRIBER_PRIORITY: 200,
        ParamType.GROUP_ORDER: 2,
    }
    buf = Buffer(capacity=32)
    MOQTMessage._serialize_params(buf, params, prof=profile_for(18))
    n = buf.tell()
    out = MOQTMessage._deserialize_params(
        Buffer(data=bytes(buf.data_slice(0, n))), prof=profile_for(18), buf_end=n)
    assert out == params


def test_d18_nonuint8_even_param_large_value_is_vi64():
    # A non-uint8 even param (DELIVERY_TIMEOUT 0x02) with value 100 must
    # go through the buffer's vi64 codec: 1 byte 0x64, NOT RFC9000's 2-byte
    # 0x40 0x64. Guards buffer-mode tagging for values >= 64 on the ordinary
    # (non-typed) param path — small-value tests can't see this.
    from aiomoqt.types import ParamType
    buf = Buffer(capacity=16)
    MOQTMessage._serialize_params(
        buf, {ParamType.DELIVERY_TIMEOUT: 100}, prof=profile_for(18))
    raw = bytes(buf.data_slice(0, buf.tell()))
    assert raw == bytes([0x01, 0x02, 0x64])  # count, delta-key 0x02, vi64(100)
    out = MOQTMessage._deserialize_params(
        Buffer(data=raw), prof=profile_for(18), buf_end=len(raw))
    assert out == {ParamType.DELIVERY_TIMEOUT: 100}


def test_d16_nonuint8_even_param_large_value_is_rfc9000():
    # Same param/value on d16 stays RFC9000 (2-byte 0x40 0x64) — the
    # buffer-mode change must not perturb pre-d18.
    from aiomoqt.types import ParamType
    buf = Buffer(capacity=16)
    MOQTMessage._serialize_params(
        buf, {ParamType.DELIVERY_TIMEOUT: 100}, prof=profile_for(16))
    raw = bytes(buf.data_slice(0, buf.tell()))
    assert raw == bytes([0x01, 0x02, 0x40, 0x64])
    out = MOQTMessage._deserialize_params(
        Buffer(data=raw), prof=profile_for(16), buf_end=len(raw))
    assert out == {ParamType.DELIVERY_TIMEOUT: 100}


def test_d18_request_update_large_ids_round_trip_vi64():
    # RequestUpdate ids go through the buffer's push_vint. With values >= 64
    # the vi64 vs RFC9000 forms diverge, so this confirms the payload buffer
    # was tagged vi64: request_id 100 -> 0x64 (1B), existing 200 -> 0x80C8.
    from aiomoqt.messages.request import RequestUpdate
    raw = bytes(RequestUpdate(
        request_id=100, existing_request_id=200,
        parameters={}).serialize(prof=profile_for(18)).data)
    body = raw[3:]  # after type (1B, 0x02) + 16-bit Length
    assert body[0] == 0x64
    assert body[1:3] == bytes([0x80, 0xC8])
    ru = RequestUpdate.deserialize(
        Buffer(data=body, vi64=True), prof=profile_for(18), buf_end=len(body))
    assert ru.request_id == 100
    assert ru.existing_request_id == 200


def test_d18_publish_namespace_large_request_id_vi64():
    # Control-message BODY integers (not just params) must be vi64 in d18.
    # request_id 100 (>=64) discriminates: vi64 0x64 (1B) vs RFC9000 0x40
    # 0x64 (2B). Deserialize is fed a vi64-tagged buffer, mirroring the
    # dispatch chokepoint that tags every control buffer before deserialize.
    from aiomoqt.messages.namespace import PublishNamespace
    msg = PublishNamespace(
        request_id=100, namespace=(b"ex", b"meeting"), parameters={})
    raw = bytes(msg.serialize(prof=profile_for(18)).data)
    body = raw[3:]  # after type (1B, 0x06) + 16-bit Length
    assert body[0] == 0x64  # vi64(100), not RFC9000's 0x40
    rbuf = Buffer(data=body, vi64=True)
    out = PublishNamespace.deserialize(rbuf, prof=profile_for(18), buf_end=len(body))
    assert out.request_id == 100
    assert out.namespace == (b"ex", b"meeting")


def test_d16_publish_namespace_request_id_stays_rfc9000():
    # Same message on d16 keeps RFC9000: request_id 100 -> 0x40 0x64.
    from aiomoqt.messages.namespace import PublishNamespace
    msg = PublishNamespace(
        request_id=100, namespace=(b"ex",), parameters={})
    raw = bytes(msg.serialize(prof=profile_for(16)).data)
    body = raw[3:]
    assert body[0:2] == bytes([0x40, 0x64])  # rfc9000(100)
    out = PublishNamespace.deserialize(
        Buffer(data=body), prof=profile_for(16), buf_end=len(body))
    assert out.request_id == 100


def test_d18_subscribe_namespace_renumbered_0x50_no_options():
    from aiomoqt.messages.namespace import SubscribeNamespace
    msg = SubscribeNamespace(request_id=1, namespace_prefix=(b"ex",),
                             subscribe_options=7, parameters={})
    raw = bytes(msg.serialize(prof=profile_for(18)).data)
    # d18 type is vi64 0x50 (1 byte); 0x50 >= 64 so RFC9000 would be 2 bytes.
    assert raw[0] == 0x50
    plen = (raw[1] << 8) | raw[2]
    body = raw[3:3 + plen]
    out = SubscribeNamespace.deserialize(
        Buffer(data=body, vi64=True), prof=profile_for(18), buf_end=len(body))
    assert out.request_id == 1
    assert out.namespace_prefix == (b"ex",)
    assert out.subscribe_options == 0  # d18 dropped it (was set to 7)


def test_d16_subscribe_namespace_keeps_0x11_and_options():
    from aiomoqt.messages.namespace import SubscribeNamespace
    msg = SubscribeNamespace(request_id=1, namespace_prefix=(b"ex",),
                             subscribe_options=7, parameters={})
    raw = bytes(msg.serialize(prof=profile_for(16)).data)
    assert raw[0] == 0x11  # unchanged code point in d16
    plen = (raw[1] << 8) | raw[2]
    body = raw[3:3 + plen]
    out = SubscribeNamespace.deserialize(
        Buffer(data=body), prof=profile_for(16), buf_end=len(body))
    assert out.subscribe_options == 7  # d16 keeps it


def test_d18_subscribe_tracks_round_trip():
    from aiomoqt.messages.namespace import SubscribeTracks
    msg = SubscribeTracks(request_id=3, namespace_prefix=(b"ex", b"m"),
                          parameters={})
    raw = bytes(msg.serialize(prof=profile_for(18)).data)
    assert raw[0] == 0x51  # vi64 type
    plen = (raw[1] << 8) | raw[2]
    body = raw[3:3 + plen]
    out = SubscribeTracks.deserialize(
        Buffer(data=body, vi64=True), prof=profile_for(18), buf_end=len(body))
    assert out.request_id == 3
    assert out.namespace_prefix == (b"ex", b"m")


def test_d18_publish_blocked_round_trip():
    from aiomoqt.messages.namespace import PublishBlocked
    msg = PublishBlocked(namespace_suffix=(b"p100",), track_name=b"clock")
    raw = bytes(msg.serialize(prof=profile_for(18)).data)
    assert raw[0] == 0x0F  # vi64 type (also < 64, single byte)
    plen = (raw[1] << 8) | raw[2]
    body = raw[3:3 + plen]
    out = PublishBlocked.deserialize(
        Buffer(data=body, vi64=True), prof=profile_for(18), buf_end=len(body))
    assert out.namespace_suffix == (b"p100",)
    assert out.track_name == b"clock"


def test_d18_registry_namespace_renumber():
    from aiomoqt.types import MOQTMessageType, D18MessageType
    from aiomoqt.messages.namespace import (
        SubscribeNamespace, SubscribeTracks, PublishBlocked)
    reg18 = _MOQTSessionMixin.CONTROL_REGISTRY[MOQTDraft.DRAFT_18]
    assert MOQTMessageType.SUBSCRIBE_NAMESPACE not in reg18  # old 0x11 gone
    assert reg18[D18MessageType.SUBSCRIBE_NAMESPACE][0] is SubscribeNamespace
    assert reg18[D18MessageType.SUBSCRIBE_TRACKS][0] is SubscribeTracks
    assert reg18[D18MessageType.PUBLISH_BLOCKED][0] is PublishBlocked
    reg16 = _MOQTSessionMixin.CONTROL_REGISTRY[MOQTDraft.DRAFT_16]
    assert MOQTMessageType.SUBSCRIBE_NAMESPACE in reg16  # d16 keeps 0x11


def test_d18_subscribe_large_request_id_vi64():
    # Proven-path message: request_id >= 64 must be vi64 in the body.
    from aiomoqt.messages.subscribe import Subscribe
    msg = Subscribe(request_id=100, track_namespace=(b"ns",),
                    track_name=b"clock")
    raw = bytes(msg.serialize(prof=profile_for(18)).data)
    body = raw[3:]  # after type 0x03 (1B) + 16-bit Length
    assert body[0] == 0x64  # vi64(100), not RFC9000 0x40 0x64
    out = Subscribe.deserialize(
        Buffer(data=body, vi64=True), prof=profile_for(18), buf_end=len(body))
    assert out.request_id == 100
    assert out.track_namespace == (b"ns",)
    assert out.track_name == b"clock"


def test_d18_subscribe_ok_large_track_alias_vi64():
    # SubscribeOk drops request_id in d18; body starts with track_alias.
    # 300 discriminates: vi64 0x81 0x2C vs RFC9000 0x41 0x2C.
    from aiomoqt.messages.subscribe import SubscribeOk
    msg = SubscribeOk(request_id=7, track_alias=300)
    raw = bytes(msg.serialize(prof=profile_for(18)).data)
    body = raw[3:]
    assert body[0:2] == bytes([0x81, 0x2C])  # vi64(300)
    out = SubscribeOk.deserialize(
        Buffer(data=body, vi64=True), prof=profile_for(18), buf_end=len(body))
    assert out.request_id is None  # d18 omits it
    assert out.track_alias == 300


def test_d18_publish_large_ids_vi64():
    from aiomoqt.messages.publish import Publish
    msg = Publish(request_id=100, track_namespace=(b"ns",),
                  track_name=b"clock", track_alias=200)
    raw = bytes(msg.serialize(prof=profile_for(18)).data)
    body = raw[3:]  # after type 0x1D (1B) + 16-bit Length
    assert body[0] == 0x64  # vi64(100) request_id
    out = Publish.deserialize(
        Buffer(data=body, vi64=True), prof=profile_for(18), buf_end=len(body))
    assert out.request_id == 100
    assert out.track_alias == 200


def test_d18_setup_message_wire_form():
    from aiomoqt.messages.d18 import Setup
    from aiomoqt.types import MOQTMessageType
    msg = Setup(options={0x07: b"aiomoqt"})  # MOQT_IMPLEMENTATION
    assert msg.type == MOQTMessageType.SETUP == 0x2F00
    buf = msg.serialize(prof=profile_for(18))
    raw = bytes(buf.data_slice(0, buf.tell()))
    # Type 0x2F00 as vi64 = AF 00, then a 16-bit big-endian Length.
    assert raw[0:2] == bytes([0xAF, 0x00])
    payload_len = (raw[2] << 8) | raw[3]
    assert payload_len == len(raw) - 4


def test_d18_subscribe_ok_omits_request_id():
    from aiomoqt.messages.subscribe import SubscribeOk
    from aiomoqt.types import MOQTMessageType
    kw = dict(request_id=7, track_alias=1, content_exists=0)
    d16 = bytes(SubscribeOk(**kw).serialize(prof=profile_for(16)).data)
    d18 = bytes(SubscribeOk(**kw).serialize(prof=profile_for(18)).data)
    # d18 drops the Request ID varint -> strictly shorter wire form.
    assert len(d18) < len(d16)
    # And on the wire there is no Request ID to read back.
    rbuf = Buffer(data=d18)
    assert rbuf.pull_uint_vi64() == MOQTMessageType.SUBSCRIBE_OK
    plen = rbuf.pull_uint16()
    end = rbuf.tell() + plen
    msg = SubscribeOk.deserialize(rbuf, prof=profile_for(18), buf_end=end)
    assert msg.request_id is None  # absent on the wire; injected by stream
    assert msg.track_alias == 1


def test_d18_setup_message_roundtrip():
    from aiomoqt.messages.d18 import Setup
    opts = {0x04: 0, 0x07: b"aiomoqt/0.10"}
    raw = bytes(Setup(options=opts).serialize(prof=profile_for(18)).data)
    # Mirror the control dispatch loop: consume vi64 type + uint16 length,
    # then deserialize the payload bounded by buf_end.
    rbuf = Buffer(data=raw)
    assert rbuf.pull_uint_vi64() == 0x2F00
    plen = rbuf.pull_uint16()
    end = rbuf.tell() + plen
    out = Setup.deserialize(rbuf, prof=profile_for(18), buf_end=end)
    assert out.options == opts
