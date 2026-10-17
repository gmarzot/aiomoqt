"""draft-20 and draft-22 message codecs: the subscribe and publish path, then
FETCH."""
import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages import (
    Fetch, FetchOk, GoAway, MOQTMessage, Publish, PublishBlocked, PublishOk,
    PublishSkipped, PublishStateNotify, RequestOk, RequestUpdate, Subscribe,
    SubscribeDone, SubscribeTracks, TrackStatus)
from aiomoqt.messages.params import (
    D20RequestErrorCode, LocationFilter, LocationFilterType as LFT,
    RangeFilter, check_param_scope, range_filter_count, range_filter_error)
from aiomoqt.types import (
    D20MessageType, FilterType, MOQTProtocolViolation, ParamType, SubscribeDoneCode)
from aiomoqt.utils.buffer import Buffer

prof = profile_for


D20_DRAFTS = [20, 22]


def wire(msg, draft):
    return bytes(msg.serialize(prof=prof(draft)).data)


def body(raw):
    """Strip the vi64 type and the 16-bit length of a control message."""
    buf = Buffer(data=raw, vi64=True)
    buf.pull_vint()
    n = buf.pull_uint16()
    assert len(raw) - buf.tell() == n
    return raw[buf.tell():]


def parse(cls, raw_body, draft):
    buf = Buffer(data=raw_body, vi64=True)
    msg = cls.deserialize(buf, prof=prof(draft), buf_end=len(raw_body))
    assert buf.tell() == len(raw_body)
    return msg


def roundtrip(msg, draft):
    return parse(type(msg), body(wire(msg, draft)), draft)


def params_block(params, draft):
    buf = Buffer(capacity=512, vi64=True)
    MOQTMessage._serialize_params(buf, params, prof=prof(draft))
    return bytes(buf.data_slice(0, buf.tell()))


def unparams(raw, draft, scope=None):
    return MOQTMessage._deserialize_params(
        Buffer(data=raw, vi64=True), prof=prof(draft), buf_end=len(raw),
        scope=scope)


# -- SUBSCRIBE ----------------------------------------------------------

_NS = (b"n",)


@pytest.mark.parametrize("draft", D20_DRAFTS)
@pytest.mark.parametrize("ftype, start, end, form", [
    (FilterType.NEXT_GROUP_START, None, None, LocationFilter.relative(0)),
    (FilterType.LATEST_OBJECT, None, None, LocationFilter.next_object()),
    (FilterType.ABSOLUTE_START, (5, 2), None, LocationFilter.absolute(5, 2)),
    (FilterType.ABSOLUTE_RANGE, (5, 2), 9, LocationFilter.absolute(5, 2, 9)),
])
def test_subscribe_d18_filter_fields_ride_location_filter(
        draft, ftype, start, end, form):
    sg, so = start or (None, None)
    m = Subscribe(request_id=0, track_namespace=_NS, track_name=b"t",
                  filter_type=ftype, start_group=sg, start_object=so,
                  end_group=end)
    got = roundtrip(m, draft)
    assert got.location_filter == form
    assert (got.filter_type, got.start_group, got.start_object,
            got.end_group) == (ftype, sg, so, end)


@pytest.mark.parametrize("draft", D20_DRAFTS)
@pytest.mark.parametrize("form", [
    LocationFilter.relative(3),
    LocationFilter.absolute(5, 2, 9, 4),
], ids=["relative-3", "end-object"])
def test_subscribe_forms_without_a_d18_filter_type(draft, form):
    # No Filter Type says the same, so the d18 fields stay empty and
    # location_filter is the filter.
    m = Subscribe(request_id=0, track_namespace=_NS, track_name=b"t",
                  location_filter=form)
    got = roundtrip(m, draft)
    assert got.location_filter == form
    assert got.filter_type is None


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_subscribe_without_a_filter_is_unfiltered(draft):
    m = Subscribe(request_id=0, track_namespace=_NS, track_name=b"t")
    raw = body(wire(m, draft))
    assert raw == b"\x00\x01\x01n\x01t\x00"
    got = parse(Subscribe, raw, draft)
    assert got.location_filter is None and got.filter_type is None


def test_subscribe_exact_bytes_d20_vs_d22():
    m = Subscribe(request_id=2, track_namespace=_NS, track_name=b"t",
                  priority=200, forward=1, filter_type=FilterType.LATEST_OBJECT)
    head = b"\x02\x01\x01n\x01t\x03\x10\x01\x10\xc8\x01"
    assert body(wire(m, 20)) == head + b"\x02\x00\x00"
    assert body(wire(m, 22)) == head + b"\x05"


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_subscribe_d20_parameters(draft):
    params = {
        ParamType.DELIVERY_TIMEOUT: 100,
        0x04: 5000,                                     # RENDEZVOUS_TIMEOUT
        0x06: 200,                                      # SUBGROUP_DELIVERY_TIMEOUT
        0x23: {0x21: LocationFilter.relative(1), 0x0A: 50},
        0x25: [RangeFilter(0, ((0, 0),)), RangeFilter(1, ((2, None),))],
        0x28: RangeFilter(0, ((1, 9),), property_type=0x10),
        0x32: 0,                                        # NEW_GROUP_REQUEST
        0x35: 0,                                        # INCLUDE_PROPERTIES
    }
    m = Subscribe(request_id=0, track_namespace=_NS, track_name=b"t",
                  parameters=params)
    got = roundtrip(m, draft)
    want = dict(params)
    want[0x28] = [params[0x28]]       # range filters decode as lists
    assert got.parameters == want


@pytest.mark.parametrize("draft", D20_DRAFTS)
@pytest.mark.parametrize("ptype, value", [
    (0x08, 5),                         # EXPIRES
    (0x09, (1, 2)),                    # LARGEST_OBJECT
    (0x29, RangeFilter(0, ((1, 2),), property_type=2)),
    (0x34, (b"a",)),
], ids=["expires", "largest", "track-property-filter", "namespace-prefix"])
def test_subscribe_refuses_parameters_outside_its_scope(draft, ptype, value):
    raw = (b"\x00\x01\x01n\x01t" + params_block({ptype: value}, draft))
    with pytest.raises(MOQTProtocolViolation, match="not allowed in Subscribe"):
        parse(Subscribe, raw, draft)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_subscribe_tracks_takes_subscription_parameters(draft):
    # §10.20.1: any SUBSCRIBE parameter, plus the Track Property filter.
    params = {0x10: 0, 0x21: LocationFilter.next_object(),
              0x23: {}, 0x29: [RangeFilter(0, ((1, 1),), property_type=2)]}
    m = SubscribeTracks(request_id=0, namespace_prefix=_NS,
                        parameters=params)
    assert roundtrip(m, draft).parameters == params


# -- parameter block rules ------------------------------------------------

@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_unknown_parameter_closes_the_session(draft):
    with pytest.raises(MOQTProtocolViolation, match="unknown message parameter"):
        unparams(b"\x01\x24\x00", draft)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_repeated_parameter_rules(draft):
    # §10.2: a non-repeatable type twice closes the session.
    with pytest.raises(MOQTProtocolViolation, match="duplicate parameter 0x10"):
        unparams(b"\x02\x10\x01\x00\x01", draft)
    # AUTHORIZATION_TOKEN may repeat (§10.2.2): the values come as a list.
    raw = params_block({0x03: [b"one", b"two"]}, draft)
    # Two instances, the second at Type Delta 0; each a USE_VALUE Token.
    assert raw == bytes.fromhex("02 03 05 0300 6f6e65 00 05 0300 74776f")
    got = unparams(raw, draft)
    assert [bytes(t) for t in got[0x03]] == [b"one", b"two"]
    # A single token keeps its d18 shape.
    assert bytes(unparams(params_block({0x03: b"one"}, draft), draft)[0x03]) \
        == b"one"


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_include_properties_is_zero_or_one(draft):
    with pytest.raises(MOQTProtocolViolation, match="outside 0..1"):
        unparams(b"\x01\x35\x02", draft)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_largest_object_is_strictly_inline(draft):
    # d18 tolerates a length-prefixed Location from one peer; d20+ reads
    # two bare varints, so that form is refused.
    from aiomoqt.messages import SubscribeOk
    assert unparams(b"\x01\x09\x05\x03", draft) == {0x09: (5, 3)}
    prefixed = b"\x00\x01\x09\x02\x05\x03"   # alias 0, LARGEST_OBJECT len 2
    d18 = parse(SubscribeOk, prefixed, 18)
    assert (d18.largest_group_id, d18.largest_object_id) == (5, 3)
    with pytest.raises((MOQTProtocolViolation, RuntimeError)):
        parse(SubscribeOk, prefixed, draft)


def test_d18_parameter_block_is_unchanged():
    # 0x23 (FILL_PARAMETERS) and 0x35 do not exist at d18; 0x21 is the
    # d18 SUBSCRIPTION_FILTER bytes.
    with pytest.raises(MOQTProtocolViolation, match="unknown"):
        unparams(b"\x01\x35\x00", 18)
    assert unparams(b"\x01\x21\x01\x02", 18) == {0x21: b"\x02"}


# -- PUBLISH / PUBLISH_OK / REQUEST_UPDATE (the forward handoff) ---------

@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_publish_ok_carries_only_expires(draft):
    raw = wire(PublishOk(request_id=None, expires=1000), draft)
    # A REQUEST_OK (§10.5): no Request ID, EXPIRES, empty Track Properties.
    assert raw == bytes.fromhex("07 0004 01 08 83e8")
    got = parse(PublishOk, body(raw), draft)
    assert got.expires == 1000 and got.forward is None


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_publish_ok_refuses_subscription_fields(draft):
    m = PublishOk(request_id=None, forward=1, priority=128,
                  filter_type=FilterType.LATEST_OBJECT)
    with pytest.raises(ValueError, match="REQUEST_UPDATE"):
        m.serialize(prof=prof(draft))


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_forward_handoff_rides_request_update(draft):
    ok = PublishOk(request_id=None, forward=1, priority=7, group_order=2,
                   filter_type=FilterType.LATEST_OBJECT)
    params = ok.subscription_params()
    assert params == {ParamType.FORWARD: 1,
                      ParamType.SUBSCRIBER_PRIORITY: 7,
                      ParamType.LOCATION_FILTER: LocationFilter.next_object()}
    upd = RequestUpdate(request_id=6, parameters=params)
    got = roundtrip(upd, draft)
    assert got.request_id == 6 and got.parameters == params
    check_param_scope(got.parameters, "RequestUpdate:Subscription",
                      prof=prof(draft))


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_d18_style_publish_ok_is_refused_at_d20(draft):
    # A d18 PUBLISH_OK (REQUEST_OK carrying FORWARD) is out of scope.
    raw = b"\x01\x10\x01"
    with pytest.raises(MOQTProtocolViolation, match="not allowed in RequestOk"):
        parse(RequestOk, raw, draft)
    with pytest.raises(MOQTProtocolViolation, match="not allowed in PublishOk"):
        parse(PublishOk, raw, draft)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_publish_ok_with_track_properties_is_refused(draft):
    with pytest.raises(MOQTProtocolViolation, match="Track Properties"):
        parse(PublishOk, b"\x00\x02\x05", draft)


def test_d18_publish_ok_bytes_unchanged():
    # FORWARD 1, SUBSCRIBER_PRIORITY 128, SUBSCRIPTION_FILTER [2],
    # GROUP_ORDER 1, as a REQUEST_OK.
    m = PublishOk(request_id=None, forward=1, priority=128, group_order=1,
                  filter_type=FilterType.LATEST_OBJECT)
    assert wire(m, 18) == bytes.fromhex("07 000a 04 1001 1080 010102 0101")


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_publish_carries_subscription_parameters(draft):
    params = {0x02: 10, 0x06: 20, 0x08: 30, 0x20: 9,
              0x21: LocationFilter.absolute(4, 0), 0x22: 2}
    m = Publish(request_id=1, track_namespace=_NS, track_name=b"t",
                track_alias=3, forward=0, largest_group_id=4,
                largest_object_id=7, parameters=params)
    got = roundtrip(m, draft)
    assert got.forward == 0
    assert (got.largest_group_id, got.largest_object_id) == (4, 7)
    assert got.parameters == params


@pytest.mark.parametrize("draft", D20_DRAFTS)
@pytest.mark.parametrize("scope, ok, bad", [
    ("RequestUpdate:Subscription", {0x21, 0x23, 0x25, 0x32}, 0x29),
    ("RequestUpdate:Fetch", {0x03, 0x20}, 0x10),
    ("RequestUpdate:SubscribeNamespace", {0x34}, 0x10),
    ("RequestUpdate:SubscribeTracks", {0x10, 0x29, 0x34}, 0x21),
    ("RequestOk:Publish", {0x08}, 0x09),
    ("RequestOk:TrackStatus", {0x09}, 0x08),
    ("RequestOk:RequestUpdate", {0x08, 0x09}, 0x10),
])
def test_per_request_scopes(draft, scope, ok, bad):
    check_param_scope(ok, scope, prof=prof(draft))
    with pytest.raises(MOQTProtocolViolation, match=scope):
        check_param_scope(ok | {bad}, scope, prof=prof(draft))


def test_per_request_scope_is_not_checked_before_d20():
    check_param_scope({0x10}, "RequestOk:Publish", prof=profile_for(18))


# -- PUBLISH_STATE_NOTIFY, PUBLISH_DONE, PUBLISH_SKIPPED, GOAWAY ----------

@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_publish_state_notify(draft):
    m = PublishStateNotify(forward=0, largest_group_id=3, largest_object_id=4)
    raw = wire(m, draft)
    assert raw == bytes.fromhex("22 0006 02 09 03 04 07 00")
    got = parse(PublishStateNotify, body(raw), draft)
    assert (got.forward, got.largest_group_id, got.largest_object_id) == (
        0, 3, 4)
    assert got.type == D20MessageType.PUBLISH_STATE_NOTIFY
    with pytest.raises(MOQTProtocolViolation, match="PublishStateNotify"):
        parse(PublishStateNotify, b"\x01\x20\x01", draft)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_publish_state_notify_location_filter(draft):
    m = PublishStateNotify(location_filter=LocationFilter.absolute(9, 0))
    assert roundtrip(m, draft).location_filter == LocationFilter.absolute(9, 0)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_publish_done_has_no_subscription_ended(draft):
    m = SubscribeDone(status_code=SubscribeDoneCode.SUBSCRIPTION_ENDED,
                      stream_count=0, reason="")
    with pytest.raises(ValueError, match="SUBSCRIPTION_ENDED"):
        m.serialize(prof=prof(draft))
    # d18's code swap stands: TOO_FAR_BEHIND 0x5, EXPIRED 0x6 on the wire.
    done = SubscribeDone(status_code=SubscribeDoneCode.EXPIRED,
                         stream_count=(1 << 64) - 1, reason="x")
    raw = wire(done, draft)
    assert body(raw)[0] == 0x06
    got = roundtrip(done, draft)
    assert got.status_code == SubscribeDoneCode.EXPIRED
    assert got.stream_count == (1 << 64) - 1


def test_publish_skipped_is_publish_blocked_renamed():
    assert PublishSkipped is PublishBlocked
    assert D20MessageType.PUBLISH_SKIPPED == 0x0F
    m = PublishSkipped(namespace_suffix=(b"s",), track_name=b"t")
    assert wire(m, 20) == wire(m, 18)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_goaway_has_no_request_id(draft):
    m = GoAway(new_session_uri="", timeout=5, request_id=9)
    assert wire(m, draft) == bytes.fromhex("10 0002 00 05")
    assert wire(m, 18) == bytes.fromhex("10 0003 00 05 09")
    got = parse(GoAway, b"\x00\x05", draft)
    assert got.request_id is None and got.timeout == 5
    # A trailing d18 Request ID is left unread: the dispatcher then
    # finds the body short of its Length and closes the session.
    buf = Buffer(data=b"\x00\x05\x09", vi64=True)
    GoAway.deserialize(buf, prof=prof(draft), buf_end=3)
    assert buf.tell() == 2


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_track_status_scope(draft):
    m = TrackStatus(request_id=1, track_namespace=_NS, track_name=b"t",
                    parameters={0x03: b"tok", 0x35: 0})
    got = roundtrip(m, draft)
    assert bytes(got.parameters[0x03]) == b"tok"
    assert got.parameters[0x35] == 0
    with pytest.raises(MOQTProtocolViolation, match="TrackStatus"):
        parse(TrackStatus, b"\x01\x01\x01n\x01t\x01\x10\x01", draft)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_request_ok_scope(draft):
    ok = RequestOk(request_id=None, parameters={0x08: 5, 0x09: (1, 2)})
    assert roundtrip(ok, draft).parameters == {0x08: 5, 0x09: (1, 2)}


# -- LocationFilter semantics ---------------------------------------------

@pytest.mark.parametrize("lf, fetch, want", [
    (LocationFilter.relative(0), False, ((11, 0), None)),
    (LocationFilter.relative(1), False, ((10, 0), None)),
    (LocationFilter.relative(50), False, ((0, 0), None)),
    (LocationFilter.next_object(), False, ((10, 6), None)),
    (LocationFilter.absolute(3, 1, 4), False, ((3, 1), (4, (1 << 64) - 1))),
    (LocationFilter.absolute(3, 1, 12, 2), True, ((3, 1), (10, 5))),
    (LocationFilter.none(), True, ((0, 0), (10, 5))),
    (LocationFilter.absolute(3, 1, 4, 2), True, ((3, 1), (4, 2))),
])
def test_location_filter_resolves_against_largest(lf, fetch, want):
    assert lf.resolve((10, 5), fetch=fetch) == want


def test_location_filter_resolves_with_nothing_published():
    assert LocationFilter.next_object().resolve(None) == ((0, 0), None)
    assert LocationFilter.relative(1).resolve(None) == ((0, 0), None)


@pytest.mark.parametrize("kwargs", [
    dict(type=LFT.RELATIVE_START),
    dict(type=LFT.NEXT_OBJECT, start_group=1),
    dict(type=LFT.ABSOLUTE_RANGE, start_group=5, start_object=0,
         end_group=4, end_object=0),
    dict(type=LFT.ABSOLUTE_START, start_group=1 << 64, start_object=0),
], ids=["missing-field", "extra-field", "backwards", "past-u64"])
def test_location_filter_rejects_shapes_no_draft_can_send(kwargs):
    with pytest.raises(ValueError):
        LocationFilter(**kwargs)


# -- Range Filter validation (INVALID_FILTER) ----------------------------

def test_range_filter_errors():
    ok = {0x25: [RangeFilter(0, ((1, 2),)), RangeFilter(1, ((1, 2),))],
          0x28: [RangeFilter(0, ((0, 1),), property_type=2)]}
    assert range_filter_error(ok) is None
    assert range_filter_count(ok) == 3
    assert range_filter_error(ok, max_ranges=2) is not None
    assert range_filter_error(ok, max_ranges=0) is not None
    dup = {0x25: [RangeFilter(0, ((1, 2),)), RangeFilter(0, ((5, 6),))]}
    assert "repeats SetID" in range_filter_error(dup)
    odd = {0x29: [RangeFilter(0, ((1, 2),), property_type=3)]}
    assert "odd property type" in range_filter_error(odd)
    prio = {0x27: [RangeFilter(0, ((200, 256),))]}
    assert "exceeds 255" in range_filter_error(prio)
    # OBJECTID_FILTER, SetID 0, Start 2^64-1, End delta 1: the End is
    # past 2^64-1, which refuses the request, not the session.
    big = (1 << 64) - 1
    raw = (b"\x01\x26\x0b\x00" + b"\xff" + big.to_bytes(8, "big") + b"\x01")
    got = unparams(raw, 20)
    assert got[0x26][0].ranges == ((big, big + 1),)
    assert "exceeds" in range_filter_error(got)
    assert D20RequestErrorCode.INVALID_FILTER == 0x36
    # Removal (zero length) carries no ranges and is never refused.
    assert range_filter_error({0x25: [RangeFilter(set_id=None)]}) is None


def test_range_filter_encoder_refuses_unsendable_ranges():
    buf = Buffer(capacity=64, vi64=True)
    with pytest.raises(ValueError, match="ascending"):
        RangeFilter(0, ((5, 6), (1, 2))).encode(buf, 0x25, prof=prof(20))
    with pytest.raises(ValueError, match="last Range"):
        RangeFilter(0, ((1, None), (5, 6))).encode(buf, 0x25, prof=prof(20))
    with pytest.raises(ValueError, match="property_type"):
        RangeFilter(0, ((1, 2),)).encode(buf, 0x28, prof=prof(20))


# -- FETCH ----------------------------------------------------------------

@pytest.mark.parametrize("draft", D20_DRAFTS)
@pytest.mark.parametrize("end, want", [
    ((None, None), LocationFilter.absolute(1, 2)),
    ((4, 0), LocationFilter.absolute(1, 2, 4)),
    ((4, 6), LocationFilter.absolute(1, 2, 4, 5)),
], ids=["to-largest", "whole-end-group", "end-object"])
def test_fetch_d18_range_fields_convert(draft, end, want):
    # d14-d18 End: last Object + 1, Object 0 the whole group. d20: an
    # inclusive LOCATION_FILTER.
    m = Fetch(fetch_type=1, request_id=0, namespace=_NS, track_name=b"t",
              start_group=1, start_object=2, end_group=end[0],
              end_object=end[1])
    got = roundtrip(m, draft)
    assert got.location_filter == want
    assert (got.start_group, got.start_object, got.end_group,
            got.end_object) == (1, 2) + end


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_fetch_relative_and_unfiltered(draft):
    m = Fetch(fetch_type=1, request_id=0, namespace=_NS, track_name=b"t",
              location_filter=LocationFilter.relative(2))
    got = roundtrip(m, draft)
    assert got.location_filter == LocationFilter.relative(2)
    assert got.start_group is None and got.end_group is None
    # No filter: {0, 0} through Largest Object.
    raw = b"\x00\x01\x01n\x01t\x00"
    got = parse(Fetch, raw, draft)
    assert got.location_filter is None
    assert (got.start_group, got.start_object, got.end_group) == (0, 0, None)
    assert got.fetch_type == 1


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_joining_fetch_does_not_exist(draft):
    m = Fetch(fetch_type=2, request_id=0, joining_request_id=0,
              joining_start=1)
    with pytest.raises(ValueError, match="Joining FETCH"):
        m.serialize(prof=prof(draft))


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_fetch_parameters(draft):
    params = {0x03: b"tok", 0x0A: 0, 0x26: [RangeFilter(0, ((0, 3),))],
              0x35: 1}
    m = Fetch(fetch_type=1, request_id=0, namespace=_NS, track_name=b"t",
              subscriber_priority=None, group_order=None, start_group=0,
              start_object=0, end_group=1, end_object=0, parameters=params)
    got = roundtrip(m, draft)
    assert bytes(got.parameters.pop(0x03)) == b"tok"
    del params[0x03]
    assert got.parameters == params
    with pytest.raises(MOQTProtocolViolation, match="not allowed in Fetch"):
        parse(Fetch, b"\x00\x01\x01n\x01t\x01\x10\x01", draft)


@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_fetch_ok_refuses_what_d20_cannot_say(draft):
    with pytest.raises(ValueError, match="inclusive"):
        FetchOk(request_id=None, largest_group_id=3,
                largest_object_id=0).serialize(prof=prof(draft))
    with pytest.raises(ValueError, match="no parameters"):
        FetchOk(request_id=None, largest_group_id=3, largest_object_id=1,
                parameters={0x22: 1}).serialize(prof=prof(draft))


# -- data plane: only FETCH changed ---------------------------------------

@pytest.mark.parametrize("draft", D20_DRAFTS)
def test_subgroup_and_datagram_headers_match_d18(draft):
    # §11.3.1/§11.4.2 restate the d18 type values as Type Flags.
    from aiomoqt.messages import ObjectDatagram, SubgroupHeader
    for kw in (dict(), dict(default_priority=True, end_of_group=True),
               dict(extensions_present=True, subgroup_id_mode=0)):
        hdrs = [SubgroupHeader(track_alias=3, group_id=200, subgroup_id=1,
                               prof=prof(d), **kw) for d in (18, draft)]
        assert bytes(hdrs[0].serialize().data) == \
            bytes(hdrs[1].serialize().data)
    for dg in (ObjectDatagram(track_alias=3, group_id=200, object_id=9,
                              payload=b"p", end_of_group=True),
               ObjectDatagram(track_alias=3, group_id=1, object_id=0,
                              status=3)):
        assert bytes(dg.serialize(prof(18)).data) == \
            bytes(dg.serialize(prof(draft)).data)
