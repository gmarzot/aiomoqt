"""Version + build-info reporter for aiomoqt.

Usage:
    python -m aiomoqt.versions
    aiomoqt-versions                     # console-script entry point

Prints aiomoqt's installed version, plus the aiopquic version it's
linked against and the picoquic / picotls submodule SHAs aiopquic
was built from. Useful for diagnosing version-pair mismatches across
the aiomoqt / aiopquic / picoquic stack.
"""
from __future__ import annotations

import os
import sys

from aiomoqt import __version__


def print_versions(file=sys.stdout) -> None:
    import aiomoqt
    src = os.path.dirname(aiomoqt.__file__)
    print(f"aiomoqt  {__version__}", file=file)
    print(f"         {src}", file=file)
    try:
        from aiopquic import versions as _aiopquic_versions
        _aiopquic_versions.print_versions(file=file)
    except ImportError as e:
        print(f"aiopquic (not importable: {e})", file=file)


def main() -> int:
    print_versions()
    return 0


if __name__ == "__main__":
    sys.exit(main())
