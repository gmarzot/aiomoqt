from dataclasses import dataclass

from aiomoqt.types import MOQTDraft


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
    cutoff (e.g. draft-18 vi64, uni control streams).
    """
    draft: int
    setup_carries_versions: bool  # d14 negotiates versions in-band in SETUP
    params_delta_coded: bool      # d16+ KVP parameter keys are delta-encoded


PROFILES = {
    MOQTDraft.DRAFT_14: DraftProfile(
        draft=MOQTDraft.DRAFT_14, setup_carries_versions=True,
        params_delta_coded=False),
    MOQTDraft.DRAFT_16: DraftProfile(
        draft=MOQTDraft.DRAFT_16, setup_carries_versions=False,
        params_delta_coded=True),
}


def profile_for(draft: int) -> DraftProfile:
    """DraftProfile for a draft number or version code (normalized)."""
    return PROFILES[get_major_version(draft)]
