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

import json
import os
import re
import sys
import time
from importlib import metadata as _md
from pathlib import Path

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


def _dist(name: str):
    """Return the importlib.metadata Distribution for `name`, preferring
    a site-packages `*.dist-info` over a source-tree `*.egg-info` when
    both are discoverable. importlib.metadata.distribution() returns
    the first match in sys.path order, which loses to stale egg-info
    left behind by older editable installs; explicit enumeration with
    a dist-info bias defends against that shadowing."""
    candidates = [
        d for d in _md.distributions()
        if (d.metadata["Name"] or "").lower() == name.lower()
    ]
    if not candidates:
        return None

    def _key(d):
        loc = getattr(d, "_path", None)
        is_dist_info = loc is not None and str(loc).endswith(".dist-info")
        ver = d.version or ""
        mtime = 0.0
        if loc is not None:
            p = Path(str(loc))
            for fname in ("RECORD", "METADATA"):
                try:
                    mtime = max(mtime, (p / fname).stat().st_mtime)
                except OSError:
                    continue
        return (is_dist_info, ver, mtime)

    candidates.sort(key=_key, reverse=True)
    return candidates[0]


def _is_editable(dist) -> bool:
    """True iff `dist` is a PEP 660 editable install. Reads
    `direct_url.json` written at install time. Falls back to False
    (treat as wheel) when the file or expected fields are absent —
    legacy installs without direct_url.json are treated as wheels."""
    try:
        raw = dist.read_text("direct_url.json")
    except (OSError, AttributeError):
        return False
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return bool(data.get("dir_info", {}).get("editable", False))


def _dist_install_date(dist) -> str | None:
    """Install timestamp from a Distribution: mtime of RECORD (written
    last by pip), else METADATA. Returns 'YYYY-MM-DD HH:MM' or None
    when neither is readable."""
    loc = getattr(dist, "_path", None)
    if loc is None:
        return None
    p = Path(str(loc))
    for fname in ("RECORD", "METADATA"):
        try:
            return time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime((p / fname).stat().st_mtime),
            )
        except OSError:
            continue
    return None


def _source_newest_mtime(root: str) -> str | None:
    """Newest non-pyc file mtime under `root`. Reflects source-edit
    time for pure-Python editable installs and Cython rebuild time
    for native editable installs."""
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


def _build_date(module, name: str | None = None) -> str | None:
    """Best-effort install/build timestamp for a package.

    Wheel installs: dist-info RECORD mtime (the moment pip wrote the
    install) — answers 'when was this version installed' for bug
    reports. Editable installs: newest non-pyc mtime under the package
    dir — answers 'is the running code stale' for active development.
    `name` defaults to the top-level package of `module`."""
    if name is None:
        name = module.__name__.split(".")[0]
    dist = _dist(name)
    if dist is not None and not _is_editable(dist):
        stamped = _dist_install_date(dist)
        if stamped is not None:
            return stamped
    root = os.path.dirname(os.path.abspath(module.__file__))
    return _source_newest_mtime(root)


def _version(v: str) -> str:
    """Installed version with setuptools_scm's `.dYYYYMMDD` dirty-date
    stripped — that day-stamp is redundant with the bracketed build time."""
    return re.sub(r"\.d\d{8}", "", v)


def _meta(module, name: str | None = None) -> str:
    """Suffix after the version: ' (PATH) [BUILD]'. PATH is the
    abbreviated install dir (module.__file__'s parent — the actually-
    imported code, which for editable installs is the source tree and
    for wheels is site-packages/<pkg>). BUILD is the dist-info RECORD
    mtime for wheel installs, newest source mtime for editable installs.
    Either group is dropped when its value is unavailable."""
    out = ""
    src = _abbrev(os.path.dirname(module.__file__))
    if src:
        out += f" ({src})"
    built = _build_date(module, name)
    if built:
        out += f" [{built}]"
    return out


def print_versions(file=sys.stdout) -> None:
    import aiomoqt
    dist = _dist("aiomoqt")
    ver = dist.version if dist is not None else __version__
    print(f"{'aiomoqt:':<{_LABEL_W}}{_version(ver)}{_meta(aiomoqt, 'aiomoqt')}", file=file)
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
