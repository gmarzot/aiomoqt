import contextvars
from aiomoqt.types import MOQT_CUR_VERSION

# Per-task version context — each asyncio task (session, probe) gets
# its own version without leaking to other concurrent sessions.
_moqt_version: contextvars.ContextVar[int] = contextvars.ContextVar(
    'moqt_version', default=MOQT_CUR_VERSION)

def get_moqt_ctx_version() -> int:
    return _moqt_version.get()

def set_moqt_ctx_version(version: int = MOQT_CUR_VERSION) -> int:
    _moqt_version.set(version)
    return version

def get_major_version(version: int) -> int:
    """Extract the draft number from a MoQT version code.

    E.g. 0xff00000e -> 14, 0xff000010 -> 16.
    """
    if (version & 0x00ff0000):
        return (version & 0x00ff0000) >> 16
    else:
        return (version & 0x0000ffff)

def is_draft16_or_later(version: int = None) -> bool:
    """Check if the given (or current) version is draft-16+.

    This is the primary branching predicate, following moxygen's pattern
    of `getDraftMajorVersion(version) >= 16`.
    """
    if version is None:
        version = _moqt_version.get()
    return get_major_version(version) >= 16
