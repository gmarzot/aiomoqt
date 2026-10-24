from dataclasses import dataclass, replace

from enum import IntEnum
from types import MappingProxyType

from aiomoqt.types import (MOQTDraft, ParamType, RequestErrorCode,
                           StreamResetCode)


def get_major_version(version: int) -> int:
    """Draft number for a MoQT version code or a draft number.

    Accepts either the IETF version code (0xff00000e) or a plain draft
    number (14) and returns the draft number. Tolerant of both forms so
    a stray wire-level code can't silently mis-dispatch.
    """
    if version < 0x100:
        return version
    return version & 0x0000ffff


def is_draft16_or_later(version: int) -> bool:
    """Version-ordering predicate: True for draft-16 and later.

    Required argument — there is no process-global version. Used for
    localized "this field appeared in draft-16" cutoffs; recurring,
    named behaviors live in DraftProfile instead.
    """
    return get_major_version(version) >= MOQTDraft.DRAFT_16


@dataclass(frozen=True)
class DraftProfile:
    """Per-draft capability row: one column per spec-delta behavior
    that recurs across the wire codec. The whole version-variance
    surface is named in this one table — adding a draft is one row,
    moving a behavior's boundary is one cell. Columns are added as
    later drafts introduce behaviors that aren't a simple version
    cutoff.
    """
    draft: int
    setup_carries_versions: bool  # d14 negotiates versions in-band in SETUP
    params_delta_coded: bool      # d16+ KVP parameter keys are delta-encoded
    varint: str                   # "rfc9000" (d14/d16) | "vi64" (d18+)
    control_uni_pair: bool        # d18 control = pair of uni streams, not bidi
    reply_has_request_id: bool    # d18 drops Request ID from request replies
    uint8_params: frozenset       # message params whose VALUE is a fixed
                                  # uint8 (not a varint): d18 FORWARD 0x10 /
                                  # SUBSCRIBER_PRIORITY 0x20 / GROUP_ORDER
                                  # 0x22. Empty for d14/d16.
    location_params: frozenset = frozenset()
                                  # params whose VALUE is an inline Location
                                  # (group + object varints, no length prefix):
                                  # d18 LARGEST_OBJECT 0x09. Empty for d14/d16
                                  # (there LARGEST_OBJECT is length-prefixed).
    two_level_discovery: bool = False
                                  # d18 splits discovery: SUBSCRIBE_NAMESPACE
                                  # reports namespaces (NAMESPACE), and
                                  # SUBSCRIBE_TRACKS then asks one namespace
                                  # for its tracks (PUBLISH). d14/d16 fuse
                                  # both into SUBSCRIBE_NAMESPACE.
    merged_datagram_layout: bool = False
                                  # d16+ OBJECT_DATAGRAM type 0b00X0XXXX with
                                  # DEFAULT_PRIORITY 0x08 and STATUS 0x20
                                  # (status datagrams fold into the object
                                  # datagram); d14 keeps OBJECT_DATAGRAM_STATUS.
    subgroup_type_mask: int = 0x0F
                                  # flag bits a SUBGROUP_HEADER type carries
                                  # over 0x10: d16 adds DEFAULT_PRIORITY 0x20,
                                  # d18 adds FIRST_OBJECT 0x40.
    object_statuses: frozenset = frozenset({0x0, 0x3, 0x4})
                                  # Object Status values the draft defines;
                                  # d14 also has DOES_NOT_EXIST 0x1.
    param_table: int = 18
                                  # Message Parameter registry and scopes in
                                  # messages/params.py: 18, 20, 22.
                                  # Consulted from d18 on.
    location_filter_typed: bool = False
                                  # d22 LOCATION_FILTER (0x21) leads with a
                                  # Location Filter Type instead of a Length.
    relay_forwards_publish_namespace: bool = True
                                  # a relay forwards PUBLISH_NAMESPACE
                                  # downstream; d22 stops it.
    publish_ok_expires_only: bool = False
                                  # d20+ PUBLISH_OK carries only EXPIRES; the
                                  # subscription parameters ride REQUEST_UPDATE.
    publish_done_subscription_ended: bool = True
                                  # PUBLISH_DONE SUBSCRIPTION_ENDED (0x3)
                                  # exists; d20 removed it.
    goaway_request_id: bool = True
                                  # d18 GOAWAY may end with a Request ID; d20
                                  # removed it.
    fetch_location_filter: bool = False
                                  # d20+ FETCH has no Fetch Type, Start or End
                                  # and no Joining form: the range rides
                                  # LOCATION_FILTER, and End Location is
                                  # inclusive in FETCH and FETCH_OK.
    fetch_timed_out_range: bool = False
                                  # d20+ FETCH stream End of Timed-Out Range
                                  # marker (0x20C).
    fetch_object_status: bool = False
                                  # A zero Payload Length in a FETCH object is
                                  # followed by an Object Status (d14); d16+
                                  # FETCH objects carry none.
    error_table: int = 18
                                  # request error and stream reset code
                                  # tables below: 14, 16, 18, 20.
    padding: bool = True
                                  # PADDING streams and datagrams (d18+).
    namespace_scoped: frozenset = frozenset(
        {"SubscribeNamespace", "PublishNamespace"})
                                  # requests whose Redirect has no Track
                                  # Name (§10.6.1); d20 adds SUBSCRIBE_TRACKS.
    zero_delivery_timeout_closes: bool = False
                                  # d16 DELIVERY_TIMEOUT (0x02) of 0, as a
                                  # parameter or Track Property, closes.
    track_properties: bool = True
                                  # PUBLISH, SUBSCRIBE_OK and FETCH_OK end
                                  # with Track Properties; d14 has none.
    reserved_period_namespaces: bool = False
                                  # d18+: a namespace whose first field starts
                                  # with "." is reserved; never sent.
    request_fin_cancels_discovery: bool = True
                                  # d18: a requester FIN cancels its
                                  # SUBSCRIBE_NAMESPACE / SUBSCRIBE_TRACKS;
                                  # d20+ FIN is not a cancellation.
    truncated_request_closes: bool = False
                                  # d20+: a request stream FIN'd mid-message
                                  # closes the session (FIN is not a cancel).
    update_has_location: bool = False
                                  # d14 SUBSCRIBE_UPDATE carries Start, End
                                  # Group, Priority and Forward as fields;
                                  # d16+ REQUEST_UPDATE only parameters.

    @property
    def vi64(self) -> bool:
        """True when this draft's variable-length integers are vi64 (d18+).
        Tag a Buffer/StreamChain with this (buf.vi64 = prof.vi64) so its
        push_vint/pull_vint dispatch to the right codec in C."""
        return self.varint == "vi64"


PROFILES = {
    MOQTDraft.DRAFT_14: DraftProfile(
        draft=MOQTDraft.DRAFT_14, setup_carries_versions=True,
        params_delta_coded=False, varint="rfc9000",
        control_uni_pair=False, reply_has_request_id=True,
        uint8_params=frozenset(),
        object_statuses=frozenset({0x0, 0x1, 0x3, 0x4}),
        error_table=14, padding=False, update_has_location=True,
        track_properties=False, fetch_object_status=True),
    MOQTDraft.DRAFT_16: DraftProfile(
        draft=MOQTDraft.DRAFT_16, setup_carries_versions=False,
        params_delta_coded=True, varint="rfc9000",
        control_uni_pair=False, reply_has_request_id=True,
        uint8_params=frozenset(),
        merged_datagram_layout=True,
        subgroup_type_mask=0x2F,
        error_table=16, padding=False,
        zero_delivery_timeout_closes=True),
    # draft-18 negotiates out-of-band (ALPN/WT-Protocol) like d16 and uses
    # delta-coded params, but forks the wire codec to vi64, runs control over
    # a pair of uni streams, drops the Request ID from request replies, and
    # splits track discovery into two requests.
    MOQTDraft.DRAFT_18: DraftProfile(
        draft=MOQTDraft.DRAFT_18, setup_carries_versions=False,
        params_delta_coded=True, varint="vi64",
        control_uni_pair=True, reply_has_request_id=False,
        uint8_params=frozenset({
            ParamType.FORWARD,
            ParamType.SUBSCRIBER_PRIORITY,
            ParamType.GROUP_ORDER,
        }),
        location_params=frozenset({ParamType.LARGEST_OBJECT}),
        two_level_discovery=True,
        merged_datagram_layout=True,
        subgroup_type_mask=0x6F,
        reserved_period_namespaces=True),
}

# d22 differs from d20 only in its switches.
# Every column not named here keeps the d18 value.
_D20 = replace(
    PROFILES[MOQTDraft.DRAFT_18], draft=MOQTDraft.DRAFT_20,
    param_table=20,
    error_table=20,
    namespace_scoped=frozenset(
        {"SubscribeNamespace", "PublishNamespace", "SubscribeTracks"}),
    publish_ok_expires_only=True,
    publish_done_subscription_ended=False,
    goaway_request_id=False,
    fetch_location_filter=True,
    fetch_timed_out_range=True,
    truncated_request_closes=True,
    request_fin_cancels_discovery=False)
PROFILES[MOQTDraft.DRAFT_20] = _D20
PROFILES[MOQTDraft.DRAFT_22] = replace(
    _D20, draft=MOQTDraft.DRAFT_22,
    param_table=22,
    location_filter_typed=True,
    relay_forwards_publish_namespace=False)


def profile_for(draft: int) -> DraftProfile:
    """DraftProfile for a draft number or version code (normalized).
    Raises ValueError for a draft without a profile."""
    major = get_major_version(draft)
    try:
        return PROFILES[major]
    except KeyError:
        raise ValueError(
            f"MoQT draft-{major} is not supported; "
            f"known drafts: {sorted(int(d) for d in PROFILES)}") from None


# Request error codes by name, per error_table. d14 has separate
# SUBSCRIBE_ERROR and FETCH_ERROR spaces; d16+ one REQUEST_ERROR space.
_E = RequestErrorCode
_D14_SUBSCRIBE_ERRORS = {
    _E.INTERNAL_ERROR: 0x0, _E.UNAUTHORIZED: 0x1, _E.TIMEOUT: 0x2,
    _E.NOT_SUPPORTED: 0x3, _E.DOES_NOT_EXIST: 0x4, _E.INVALID_RANGE: 0x5,
    _E.MALFORMED_AUTH_TOKEN: 0x10, _E.EXPIRED_AUTH_TOKEN: 0x12,
}
_D14_FETCH_ERRORS = {**_D14_SUBSCRIBE_ERRORS,
                     _E.INVALID_JOINING_REQUEST_ID: 0x7,
                     _E.MALFORMED_TRACK: 0x9}
_D16_ERRORS = {code: int(code) for code in (
    _E.INTERNAL_ERROR, _E.UNAUTHORIZED, _E.TIMEOUT, _E.NOT_SUPPORTED,
    _E.MALFORMED_AUTH_TOKEN, _E.EXPIRED_AUTH_TOKEN, _E.DOES_NOT_EXIST,
    _E.INVALID_RANGE, _E.MALFORMED_TRACK, _E.DUPLICATE_SUBSCRIPTION,
    _E.UNINTERESTED, _E.PREFIX_OVERLAP, _E.INVALID_JOINING_REQUEST_ID)}
_D18_ERRORS = {**_D16_ERRORS, **{code: int(code) for code in (
    _E.GOING_AWAY, _E.EXCESSIVE_LOAD, _E.NAMESPACE_TOO_LARGE,
    _E.UNSUPPORTED_EXTENSION, _E.REDIRECT)}}
_D20_ERRORS = {**{code: wire for code, wire in _D18_ERRORS.items()
                  if code not in (_E.DUPLICATE_SUBSCRIPTION,
                                  _E.INVALID_JOINING_REQUEST_ID)},
               _E.CONFLICTING_FILTERS: 0x35, _E.INVALID_FILTER: 0x36}
# {(error_table, is_fetch): {RequestErrorCode: wire}}
_REQUEST_ERRORS = MappingProxyType({
    (14, False): _D14_SUBSCRIBE_ERRORS, (14, True): _D14_FETCH_ERRORS,
    **{(16, f): _D16_ERRORS for f in (False, True)},
    **{(18, f): _D18_ERRORS for f in (False, True)},
    **{(20, f): _D20_ERRORS for f in (False, True)},
})
_ERROR_ALIASES = {"TRACK_DOES_NOT_EXIST": "DOES_NOT_EXIST"}

_R = StreamResetCode
_D14_RESETS = {code: int(code) for code in (
    _R.INTERNAL_ERROR, _R.CANCELLED, _R.DELIVERY_TIMEOUT, _R.SESSION_CLOSED)}
_STREAM_RESETS = MappingProxyType({
    14: _D14_RESETS,
    16: {**_D14_RESETS, _R.UNKNOWN_OBJECT_STATUS: 0x4,
         _R.MALFORMED_TRACK: 0x12},
    18: {code: int(code) for code in _R},
    20: {code: int(code) for code in _R},
})


def request_error_code(code: int, profile: DraftProfile,
                       fetch: bool = False) -> int:
    """Wire value of an error code for a REQUEST_ERROR, SUBSCRIBE_ERROR or
    FETCH_ERROR at this profile's draft. A named code (any *ErrorCode
    member) is looked up by name, INTERNAL_ERROR when the draft lacks it;
    a plain int is sent as given."""
    if not isinstance(code, IntEnum):
        return int(code)
    name = _ERROR_ALIASES.get(code.name, code.name)
    try:
        canonical = RequestErrorCode[name]
    except KeyError:
        return int(code)
    table = _REQUEST_ERRORS[(profile.error_table, fetch)]
    return table.get(canonical, 0)


def canonical_request_error(wire: int, profile: DraftProfile,
                            fetch: bool = False) -> int:
    """The RequestErrorCode a received error code means at this draft;
    a code the draft does not define comes back unchanged."""
    for canonical, value in _REQUEST_ERRORS[(profile.error_table,
                                             fetch)].items():
        if value == wire:
            return canonical
    return wire


def stream_reset_code(code: int, profile: DraftProfile) -> int:
    """Wire value of a RESET_STREAM / STOP_SENDING code at this draft. A
    StreamResetCode the draft lacks is INTERNAL_ERROR; other ints pass."""
    if not isinstance(code, StreamResetCode):
        return int(code)
    return _STREAM_RESETS[profile.error_table].get(code, 0)


def canonical_stream_reset(wire: int, profile: DraftProfile) -> int:
    """The StreamResetCode a received RESET_STREAM / STOP_SENDING code
    means at this draft; a code the draft does not define comes back
    unchanged."""
    for canonical, value in _STREAM_RESETS[profile.error_table].items():
        if value == wire:
            return canonical
    return wire
