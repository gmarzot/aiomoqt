from aiomoqt.types import MOQT_CUR_VERSION

# Global version context. Set per-session before serialization/parsing.
# Note: for concurrent sessions (e.g. relay probe), callers must
# set this before each session's setup sequence.
moqt_version = MOQT_CUR_VERSION


def _normalize_version(version: int) -> int:
    """Normalize a version to the full version code (0xff0000XX).

    Accepts either:
      - Draft number: 14, 16, etc.
      - Version code: 0xff00000e, 0xff000010, etc.

    Always returns the full version code.
    """
    if version < 256:
        # Draft number → version code
        return 0xff000000 | version
    return version


def get_moqt_ctx_version() -> int:
    return moqt_version


def set_moqt_ctx_version(version: int = MOQT_CUR_VERSION) -> int:
    global moqt_version
    moqt_version = _normalize_version(version)
    return moqt_version


def get_major_version(version: int = None) -> int:
    """Extract the draft number from a MoQT version code or draft number.

    E.g. 0xff00000e -> 14, 0xff000010 -> 16, 14 -> 14, 16 -> 16.
    """
    if version is None:
        version = moqt_version
    version = _normalize_version(version)
    return version & 0x0000ffff


def is_draft16_or_later(version: int = None) -> bool:
    """Check if the given (or current) version is draft-16+.

    Accepts draft numbers (16) or version codes (0xff000010).
    """
    return get_major_version(version) >= 16
