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
        uint8_params=frozenset()),
    MOQTDraft.DRAFT_16: DraftProfile(
        draft=MOQTDraft.DRAFT_16, setup_carries_versions=False,
        params_delta_coded=True, varint="rfc9000",
        control_uni_pair=False, reply_has_request_id=True,
        uint8_params=frozenset()),
    # draft-18 negotiates out-of-band (ALPN/WT-Protocol) like d16 and uses
    # delta-coded params, but forks the wire codec to vi64, runs control over
    # a pair of uni streams, and drops the Request ID from request replies.
    MOQTDraft.DRAFT_18: DraftProfile(
        draft=MOQTDraft.DRAFT_18, setup_carries_versions=False,
        params_delta_coded=True, varint="vi64",
        control_uni_pair=True, reply_has_request_id=False,
        uint8_params=frozenset({
            ParamType.FORWARD,
            ParamType.SUBSCRIBER_PRIORITY,
            ParamType.GROUP_ORDER,
        })),
}


def profile_for(draft: int) -> DraftProfile:
    """DraftProfile for a draft number or version code (normalized)."""
    return PROFILES[get_major_version(draft)]
