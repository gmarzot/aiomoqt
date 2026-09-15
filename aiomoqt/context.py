from dataclasses import dataclass

from aiomoqt.types import MOQTDraft, ParamType


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
        object_statuses=frozenset({0x0, 0x1, 0x3, 0x4})),
    MOQTDraft.DRAFT_16: DraftProfile(
        draft=MOQTDraft.DRAFT_16, setup_carries_versions=False,
        params_delta_coded=True, varint="rfc9000",
        control_uni_pair=False, reply_has_request_id=True,
        uint8_params=frozenset(),
        merged_datagram_layout=True,
        subgroup_type_mask=0x2F),
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
        subgroup_type_mask=0x6F),
}


def profile_for(draft: int) -> DraftProfile:
    """DraftProfile for a draft number or version code (normalized)."""
    return PROFILES[get_major_version(draft)]
