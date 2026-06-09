"""Version + build-info reporter for aiomoqt.

Usage:
    python -m aiomoqt.versions
    aiomoqt-versions                     # console-script entry point

Prints aiomoqt's installed version, plus the aiopquic version it's
linked against and the picoquic / picotls submodule revisions aiopquic
was built from. Useful for diagnosing version-pair mismatches across
the aiomoqt / aiopquic / picoquic stack.

Each line reads `name:  REV (PATH) [DATE]`: parens hold the install
path (abbreviated with ~), brackets the date — build date+time for a
package, pinned-commit date for a submodule. Either group is omitted
when not applicable (submodules carry no path).
"""
from __future__ import annotations

import os
import re
import sys
import time

from aiomoqt import __version__

_LABEL_W = 11


def _abbrev(path: str) -> str:
    """Replace a leading $HOME with ~ for a shorter display path."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _build_date(module) -> str | None:
    """Newest non-pyc file mtime under a package dir, as 'YYYY-MM-DD HH:MM'.
    Reflects compile time for editable native builds and install time for
    wheels; __pycache__/.pyc are skipped so it never reads 'now'."""
    root = os.path.dirname(os.path.abspath(module.__file__))
    newest = 0.0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
            except OSError:
                continue
    if not newest:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))


def _version(v: str) -> str:
    """Installed version with setuptools_scm's `.dYYYYMMDD` dirty-date
    stripped — that day-stamp is redundant with the bracketed build time."""
    return re.sub(r"\.d\d{8}", "", v)


def _meta(module) -> str:
    """Suffix after the version: ' (PATH) [BUILD]', where PATH is the
    abbreviated install dir and BUILD is the newest-file date+time.
    Either group is dropped when its value is unavailable."""
    out = ""
    src = _abbrev(os.path.dirname(module.__file__))
    if src:
        out += f" ({src})"
    built = _build_date(module)
    if built:
        out += f" [{built}]"
    return out


def print_versions(file=sys.stdout) -> None:
    import aiomoqt
    print(f"{'aiomoqt:':<{_LABEL_W}}{_version(__version__)}{_meta(aiomoqt)}", file=file)
    try:
        from aiopquic import versions as _aiopquic_versions
        _aiopquic_versions.print_versions(file=file)
    except ImportError as e:
        print(f"{'aiopquic:':<{_LABEL_W}}(not importable: {e})", file=file)


def main() -> int:
    print_versions()
    return 0


if __name__ == "__main__":
    sys.exit(main())
