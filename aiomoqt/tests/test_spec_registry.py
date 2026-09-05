"""Per-draft control-message registry conformance.

Round-trip and loopback cannot catch a message type we send that the
negotiated draft does not define: our encoder and decoder agree, and a
lenient peer ignores the message instead of closing. That is how
PUBLISH_NAMESPACE_DONE (0x09) went out on every d18 publisher teardown
while every test passed.

The registries below are transcribed here directly from the message type
tables in each specification, independently of aiomoqt.types. The two
must agree; a typo in either surfaces as a disagreement rather than as
matching-but-wrong tables.
"""
import pytest

from aiomoqt.protocol import MOQTSessionQuic
from aiomoqt.types import CONTROL_MESSAGE_TYPES
from aiomoqt.context import profile_for
from aiomoqt.messages.base import MOQTMessage


# -- transcribed from the specs, by hand ------------------------------
# draft-ietf-moq-transport-14 §9.2 "MOQT Control Messages"
SPEC_D14 = {
    0x20: "CLIENT_SETUP",       0x21: "SERVER_SETUP",
    0x10: "GOAWAY",             0x15: "MAX_REQUEST_ID",
    0x1A: "REQUESTS_BLOCKED",   0x03: "SUBSCRIBE",
    0x04: "SUBSCRIBE_OK",       0x05: "SUBSCRIBE_ERROR",
    0x02: "SUBSCRIBE_UPDATE",   0x0A: "UNSUBSCRIBE",
    0x0B: "PUBLISH_DONE",       0x1D: "PUBLISH",
    0x1E: "PUBLISH_OK",         0x1F: "PUBLISH_ERROR",
    0x16: "FETCH",              0x18: "FETCH_OK",
    0x19: "FETCH_ERROR",        0x17: "FETCH_CANCEL",
    0x0D: "TRACK_STATUS",       0x0E: "TRACK_STATUS_OK",
    0x0F: "TRACK_STATUS_ERROR", 0x06: "PUBLISH_NAMESPACE",
    0x07: "PUBLISH_NAMESPACE_OK",
    0x08: "PUBLISH_NAMESPACE_ERROR",
    0x09: "PUBLISH_NAMESPACE_DONE",
    0x0C: "PUBLISH_NAMESPACE_CANCEL",
    0x11: "SUBSCRIBE_NAMESPACE",
    0x12: "SUBSCRIBE_NAMESPACE_OK",
    0x13: "SUBSCRIBE_NAMESPACE_ERROR",
    0x14: "UNSUBSCRIBE_NAMESPACE",
}

# draft-ietf-moq-transport-16 §9.2
SPEC_D16 = {
    0x20: "CLIENT_SETUP",       0x21: "SERVER_SETUP",
    0x10: "GOAWAY",             0x15: "MAX_REQUEST_ID",
    0x1A: "REQUESTS_BLOCKED",   0x07: "REQUEST_OK",
    0x05: "REQUEST_ERROR",      0x03: "SUBSCRIBE",
    0x04: "SUBSCRIBE_OK",       0x02: "REQUEST_UPDATE",
    0x0A: "UNSUBSCRIBE",        0x1D: "PUBLISH",
    0x1E: "PUBLISH_OK",         0x0B: "PUBLISH_DONE",
    0x16: "FETCH",              0x18: "FETCH_OK",
    0x17: "FETCH_CANCEL",       0x0D: "TRACK_STATUS",
    0x06: "PUBLISH_NAMESPACE",  0x08: "NAMESPACE",
    0x09: "PUBLISH_NAMESPACE_DONE",
    0x0E: "NAMESPACE_DONE",     0x0C: "PUBLISH_NAMESPACE_CANCEL",
    0x11: "SUBSCRIBE_NAMESPACE",
}

# draft-ietf-moq-transport-18 §10.2. Cancellation messages are gone:
# a request owns a bidi stream and withdrawal is RESET_STREAM /
# STOP_SENDING. 0x20/0x21 are RESERVED, not sendable.
SPEC_D18 = {
    0x2F00: "SETUP",            0x10: "GOAWAY",
    0x03: "SUBSCRIBE",          0x04: "SUBSCRIBE_OK",
    0x1D: "PUBLISH",            0x1E: "PUBLISH_OK",
    0x0B: "PUBLISH_DONE",       0x16: "FETCH",
    0x18: "FETCH_OK",           0x0D: "TRACK_STATUS",
    0x06: "PUBLISH_NAMESPACE",  0x50: "SUBSCRIBE_NAMESPACE",
    0x51: "SUBSCRIBE_TRACKS",   0x08: "NAMESPACE",
    0x0E: "NAMESPACE_DONE",     0x0F: "PUBLISH_BLOCKED",
    0x02: "REQUEST_UPDATE",     0x07: "REQUEST_OK",
    0x05: "REQUEST_ERROR",
}

SPEC = {14: SPEC_D14, 16: SPEC_D16, 18: SPEC_D18}

# Removed in d18 — sending any of these is a wire violation there.
D18_REMOVED = {
    0x09: "PUBLISH_NAMESPACE_DONE", 0x0A: "UNSUBSCRIBE",
    0x0C: "PUBLISH_NAMESPACE_CANCEL", 0x17: "FETCH_CANCEL",
    0x15: "MAX_REQUEST_ID", 0x1A: "REQUESTS_BLOCKED",
    0x11: "SUBSCRIBE_NAMESPACE",  # renumbered to 0x50
    0x20: "CLIENT_SETUP", 0x21: "SERVER_SETUP",  # RESERVED in d18
}


class _Sess:
    """Minimal stand-in: the guard reads only negotiated_draft."""
    def __init__(self, draft):
        self.negotiated_draft = draft


class _Msg:
    def __init__(self, type_):
        self.type = type_


_guard = MOQTSessionQuic._assert_type_defined_by_draft


@pytest.mark.parametrize("draft", [14, 16, 18])
def test_runtime_table_matches_the_spec(draft):
    assert CONTROL_MESSAGE_TYPES[draft] == frozenset(SPEC[draft]), (
        f"draft-{draft} table disagrees with the spec transcription; "
        f"only in runtime: "
        f"{sorted(CONTROL_MESSAGE_TYPES[draft] - set(SPEC[draft]))}, "
        f"only in spec: "
        f"{sorted(set(SPEC[draft]) - CONTROL_MESSAGE_TYPES[draft])}")


@pytest.mark.parametrize("draft", [14, 16, 18])
def test_every_spec_type_is_sendable_on_its_draft(draft):
    for type_, name in SPEC[draft].items():
        _guard(_Sess(draft), _Msg(type_))  # must not raise


@pytest.mark.parametrize("type_,name", sorted(D18_REMOVED.items()))
def test_d18_refuses_types_it_does_not_define(type_, name):
    # The regression: 0x09 went out on every d18 publisher teardown.
    with pytest.raises(Exception) as exc:
        _guard(_Sess(18), _Msg(type_))
    assert "draft-18" in str(exc.value)


def test_d18_removed_types_remain_legal_on_older_drafts():
    # The guard must gate on the draft, not blanket-ban the type.
    _guard(_Sess(14), _Msg(0x09))
    _guard(_Sess(16), _Msg(0x09))
    _guard(_Sess(14), _Msg(0x0A))
    _guard(_Sess(16), _Msg(0x17))


# -- §1.4.3 Key-Value-Pair structural rule ----------------------------

@pytest.mark.parametrize("draft", [16, 18])
def test_serialized_params_obey_the_odd_even_length_rule(draft):
    """d16 §1.4.3: Length present only when Type is odd; even Type
    carries a bare varint. d18 §10.2: the Value encoding comes from each
    parameter's definition — LARGEST_OBJECT (0x09) is a Location, two
    bare varints, NO Length despite the odd type number.
    """
    prof = profile_for(draft)
    params = {
        0x02: 5000,          # even -> bare varint
        0x08: 1000,          # even -> bare varint
        0x09: ((7, 9) if draft == 18 else b"\x07\x09"),
    }
    from aiopquic.buffer import Buffer
    payload = Buffer(capacity=4096, vi64=prof.vi64)
    MOQTMessage._serialize_params(payload, params, prof=prof)
    raw = bytes(payload.data_slice(0, payload.tell()))

    # Independent reader: knows only the spec rules, not our classes.
    r = Buffer(data=raw, vi64=prof.vi64)
    count = r.pull_vint()
    prev = 0
    seen = {}
    for _ in range(count):
        key = r.pull_vint()
        if prof.params_delta_coded:
            key += prev
            prev = key
        if draft >= 18 and key == 0x09:
            seen[key] = (r.pull_vint(), r.pull_vint())  # §10.2 Location
        elif key % 2 == 0:
            seen[key] = r.pull_vint()
        else:
            n = r.pull_vint()
            seen[key] = r.pull_bytes(n)
    assert r.tell() == len(raw), "declared params did not consume the block"
    assert set(seen) == set(params)


@pytest.mark.parametrize("draft", [14, 16, 18])
def test_unknown_subscription_filter_type_is_fatal(draft):
    """§5.1.2: subscription filter types are 0x1-0x4; any other value
    MUST close the session with PROTOCOL_VIOLATION. moxygen#225: a peer
    (moq-net) that sent LargestGroup=250 was correctly rejected — we
    must reject it too, not silently accept it.
    """
    from aiopquic.buffer import Buffer
    from aiomoqt.messages.subscribe import Subscribe
    from aiomoqt.types import MOQTProtocolViolation
    prof = profile_for(draft)
    # A well-formed SUBSCRIBE with filter_type 250 in place of a legal
    # one round-trips our encoder (which accepts the int), and MUST be
    # rejected on decode.
    good = Subscribe(request_id=(0 if draft >= 18 else 0),
                     track_namespace=(b"ns",), track_name=b"t",
                     filter_type=250,
                     priority=128, group_order=1, forward=1)
    raw = bytes(good.serialize(prof=prof).data)
    buf = Buffer(data=raw, vi64=prof.vi64)
    buf.pull_vint()
    mlen = buf.pull_uint16()
    with pytest.raises(MOQTProtocolViolation):
        Subscribe.deserialize(buf, prof=prof, buf_end=buf.tell() + mlen)


def test_d18_unknown_message_parameter_is_fatal():
    """§10.2: unknown Message Parameters cannot be skipped (the block
    is count-bounded) — the session closes. §14 reserves no grease in
    this registry, so no carve-out exists."""
    from aiopquic.buffer import Buffer
    from aiomoqt.types import MOQTProtocolViolation
    prof = profile_for(18)
    # count=1, Δtype=0x36 (not in §15.7), then arbitrary bytes
    buf = Buffer(data=bytes([0x01, 0x36, 0x00]), vi64=True)
    with pytest.raises(MOQTProtocolViolation):
        MOQTMessage._deserialize_params(buf, prof=prof, buf_end=3)


def test_d18_namespace_prefix_param_is_a_tuple():
    """§10.2.14: TRACK_NAMESPACE_PREFIX is Track-Namespace-encoded
    (field count + length-prefixed fields), not odd/even KVP."""
    from aiopquic.buffer import Buffer
    prof = profile_for(18)
    payload = Buffer(capacity=256, vi64=True)
    MOQTMessage._serialize_params(
        payload, {0x34: (b"live", b"cam")}, prof=prof)
    raw = bytes(payload.data_slice(0, payload.tell()))
    assert raw == bytes([0x01, 0x34, 0x02,
                         0x04]) + b"live" + bytes([0x03]) + b"cam"
    r = Buffer(data=raw, vi64=True)
    out = MOQTMessage._deserialize_params(r, prof=prof, buf_end=len(raw))
    assert out[0x34] == (b"live", b"cam")


@pytest.mark.parametrize("draft", [14, 16, 18])
def test_rx_dispatch_matches_the_spec_tables(draft):
    """RX dispatches exactly the types the spec defines for the draft —
    a type outside the table closes the session, never a silent skip
    or a stale handler from an earlier draft."""
    assert {int(k) for k in MOQTSessionQuic.CONTROL_REGISTRY[draft]} \
        == set(SPEC[draft])


def test_d18_receiving_a_dead_d16_type_closes_the_session():
    """Receiving PUBLISH_NAMESPACE_DONE (0x09) at d18 used to run the
    d16 handler — which closed the session with NO_ERROR: peer grease
    became a silent clean shutdown."""
    from aiomoqt.types import MOQTProtocolViolation
    s = object.__new__(MOQTSessionQuic)
    s.negotiated_draft = 18
    s._control_msg_overrides = {}
    for dead in (0x09, 0x0A, 0x0C, 0x15, 0x17, 0x1A, 0x11):
        with pytest.raises(MOQTProtocolViolation):
            s._get_control_entry(dead)


def test_publish_done_codes_swap_on_the_d18_wire():
    """§15.10.3 Table 19: TOO_FAR_BEHIND=0x5 and EXPIRED=0x6 at d18 —
    the reverse of d16. Canonical enum keeps d16 values; the codec
    swaps at the wire, both directions."""
    from aiopquic.buffer import Buffer
    from aiomoqt.messages.subscribe import SubscribeDone
    from aiomoqt.types import SubscribeDoneCode

    def wire_code(draft, code):
        prof = profile_for(draft)
        raw = bytes(SubscribeDone(request_id=1, status_code=code,
                                  stream_count=0, reason="").serialize(
                                      prof=prof).data)
        r = Buffer(data=raw, vi64=prof.vi64)
        r.pull_vint()
        r.pull_uint16()
        if prof.reply_has_request_id:
            r.pull_vint()
        return r.pull_vint()

    assert wire_code(18, SubscribeDoneCode.TOO_FAR_BEHIND) == 0x5
    assert wire_code(18, SubscribeDoneCode.EXPIRED) == 0x6
    assert wire_code(16, SubscribeDoneCode.TOO_FAR_BEHIND) == 0x6
    assert wire_code(16, SubscribeDoneCode.EXPIRED) == 0x5
    assert wire_code(18, SubscribeDoneCode.TRACK_ENDED) == 0x2

    prof = profile_for(18)
    raw = bytes(SubscribeDone(
        request_id=1, status_code=SubscribeDoneCode.TOO_FAR_BEHIND,
        stream_count=0, reason="").serialize(prof=prof).data)
    r = Buffer(data=raw, vi64=True)
    r.pull_vint()
    mlen = r.pull_uint16()
    rt = SubscribeDone.deserialize(r, prof=prof, buf_end=r.tell() + mlen)
    assert rt.status_code == SubscribeDoneCode.TOO_FAR_BEHIND


def test_d16_status_datagram_types_parse():
    """d16 Figure 27 already has the merged layout: STATUS bit 0x20 is
    legal (types 0x24-0x2D) and must not read a payload."""
    from aiopquic.buffer import Buffer
    from aiomoqt.messages.data import ObjectDatagram
    from aiomoqt.types import ObjectStatus
    prof = profile_for(16)
    # type 0x24 = STATUS | ZERO_OBJECT_ID: alias, group, priority, status
    wire = bytes([7, 3, 128, int(ObjectStatus.END_OF_GROUP)])
    buf = Buffer(data=wire)
    msg = ObjectDatagram.deserialize(buf, len(wire), type_val=0x24,
                                     prof=prof)
    assert msg.status == ObjectStatus.END_OF_GROUP
    assert (msg.track_alias, msg.group_id, msg.object_id) == (7, 3, 0)
    assert msg.payload == b''


def test_subgroup_header_default_priority_omits_the_byte():
    """§11.4.2: DEFAULT_PRIORITY (0x20) means the Priority field is
    absent; re-serializing a received header must not emit it."""
    from aiomoqt.messages.data import SubgroupHeader
    prof = profile_for(18)
    hdr = SubgroupHeader(track_alias=1, group_id=0, subgroup_id=0,
                         publisher_priority=200, default_priority=True,
                         prof=prof)
    raw = bytes(hdr.serialize().data)
    assert raw[0] & 0x20
    # type, alias, group, subgroup — and NO priority byte
    assert len(raw) == 4


def test_fetch_ok_d18_omits_group_order_param():
    """§10.2.8: GROUP_ORDER may appear in SUBSCRIBE, PUBLISH_OK or
    FETCH — never FETCH_OK; a receiver MUST close on a parameter in a
    message it isn't defined for (§10.2.1)."""
    from aiopquic.buffer import Buffer
    from aiomoqt.messages.fetch import FetchOk
    prof = profile_for(18)
    raw = bytes(FetchOk(request_id=1, group_order=2).serialize(prof=prof).data)
    r = Buffer(data=raw, vi64=True)
    r.pull_vint()                        # Type
    mlen = r.pull_uint16()
    end = r.tell() + mlen
    r.pull_uint8()                       # End of Track (no request id at d18)
    r.pull_vint()                        # Largest Group
    r.pull_vint()                        # Largest Object
    assert r.pull_vint() == 0            # parameter count: GROUP_ORDER dropped
    assert r.tell() == end               # no track extensions either


def test_fetch_group_delta_direction_comes_from_our_request():
    """d18 FETCH_OK carries no GROUP_ORDER, so the group-delta decode
    direction (§11.4.4.1) is the order requested in our own FETCH."""
    from aiomoqt.protocol import _MOQTSessionMixin
    from aiomoqt.messages.fetch import Fetch
    from aiomoqt.messages.data import FetchHeader
    from aiomoqt.types import FetchType, GroupOrder
    s = object.__new__(_MOQTSessionMixin)
    fetch = Fetch(request_id=5, fetch_type=FetchType.STANDALONE,
                  group_order=GroupOrder.DESCENDING,
                  namespace=(b"ns",), track_name=b"t")
    s._subscriptions = {5: [fetch]}
    s._fetch_stream_by_request = {}
    s._data_streams = {}
    header = FetchHeader(request_id=5)
    s._admit_fetch_stream(9, header)
    assert header._group_order == GroupOrder.DESCENDING


def test_goaway_d18_wire_has_timeout_and_request_id():
    """§10.4 Figure 7: URI Length, URI, Timeout, [Request ID (control-
    stream form)] — all vi64. Hand-derived bytes, not a round-trip."""
    from aiopquic.buffer import Buffer
    from aiomoqt.messages.session_setup import GoAway
    prof = profile_for(18)
    msg = GoAway(new_session_uri="x", timeout=5, request_id=3)
    assert bytes(msg.serialize(prof=prof).data) == \
        bytes.fromhex("10000401780503")

    buf = Buffer(data=bytes.fromhex("10000401780503"), vi64=True)
    buf.pull_vint()                      # Type 0x10
    mlen = buf.pull_uint16()
    end = buf.tell() + mlen
    rt = GoAway.deserialize(buf, prof=prof, buf_end=end)
    assert (rt.new_session_uri, rt.timeout, rt.request_id) == ("x", 5, 3)


def test_goaway_pre_d18_wire_is_uri_only():
    from aiomoqt.messages.session_setup import GoAway
    prof = profile_for(16)
    msg = GoAway(new_session_uri="")
    assert bytes(msg.serialize(prof=prof).data) == bytes.fromhex("10000100")


def test_largest_object_accepts_cloudflare_prefixed_form():
    """Leniency golden: Cloudflare draft-18-interop SUBSCRIBE_OK,
    2026-09-02. Their LARGEST_OBJECT carries a Length prefix — a §10.2
    deviation (Location = two bare varints) we accept on receive.
    Our re-encode is the conformant inline form, not their bytes.
    """
    from aiopquic.buffer import Buffer
    from aiomoqt.messages.subscribe import SubscribeOk
    wire = bytes.fromhex("040006000109020e30")
    prof = profile_for(18)
    buf = Buffer(data=wire, vi64=prof.vi64)
    buf.pull_vint()                      # message Type
    msg_len = buf.pull_uint16()          # message Length
    end = buf.tell() + msg_len
    msg = SubscribeOk.deserialize(buf, prof=prof, buf_end=end)
    assert (msg.largest_group_id, msg.largest_object_id) == (14, 48)
    assert buf.tell() == end, "left bytes unconsumed"
    assert bytes(msg.serialize(prof=prof).data) == \
        bytes.fromhex("0400050001090e30")


def test_largest_object_matches_a_moxygen_frame():
    """Golden: moxygen d18 SUBSCRIBE_OK, live capture 2026-09-02.
    Conformant inline Location (§10.2.11) followed by Track Properties.
    0.11.0rc2 read the group as a Length, ate the first Properties
    byte, and closed the session blaming the peer.
    """
    from aiopquic.buffer import Buffer
    from aiomoqt.messages.subscribe import SubscribeOk
    wire = bytes.fromhex("04000701010902052201")
    prof = profile_for(18)
    buf = Buffer(data=wire, vi64=prof.vi64)
    buf.pull_vint()                      # message Type
    msg_len = buf.pull_uint16()          # message Length
    end = buf.tell() + msg_len
    msg = SubscribeOk.deserialize(buf, prof=prof, buf_end=end)
    assert msg.track_alias == 1
    assert (msg.largest_group_id, msg.largest_object_id) == (2, 5)
    assert buf.tell() == end, "left bytes unconsumed"
    assert bytes(msg.serialize(prof=prof).data) == wire
