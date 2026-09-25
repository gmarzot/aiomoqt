"""Version reporter: git state for from-source installs only.

An editable install's dist metadata version is written once, at install
time, so it cannot track a branch switch or an uncommitted edit. The
reporter consults git to answer "what am I running" — and must not do so
for a published wheel, where the metadata version is already accurate and
an enclosing repo would otherwise be misattributed.

These pin that boundary. Everything is asserted against synthetic repos
rather than aiomoqt's own tree, so no test depends on the branch or
cleanliness of the checkout it runs in.
"""
import io
import shutil
import subprocess

import pytest

from aiomoqt import versions as V

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git not available")


def _repo(tmp_path, name, *, commit=True):
    """A minimal package tree that is its own git repo. Returns the
    package dir, which is what the reporter is handed."""
    root = tmp_path / name
    pkg = root / name.replace("-", "_")
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.1"\n')
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    if commit:
        subprocess.run(("git", "add", "-A"), cwd=root, check=True)
        subprocess.run(("git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"), cwd=root, check=True)
    return pkg


def _head(pkg):
    return subprocess.run(("git", "-C", str(pkg), "rev-parse", "HEAD"),
                          capture_output=True, text=True).stdout.strip()


class TestOwnSourceTree:
    """The guard that keeps an unrelated enclosing repo out of the line."""

    def test_matches_its_own_package(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        assert V._own_source_tree(str(pkg), "mypkg") is True

    def test_rejects_a_different_package(self, tmp_path):
        """A wheel installed into a venv inside someone else's project
        must not report that project's revision."""
        pkg = _repo(tmp_path, "someone-elses-app")
        assert V._own_source_tree(str(pkg), "aiomoqt") is False

    def test_rejects_a_non_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert V._own_source_tree(str(plain), "aiomoqt") is False

    def test_rejects_a_repo_without_pyproject(self, tmp_path):
        pkg = _repo(tmp_path, "nopyproject")
        (pkg.parent / "pyproject.toml").unlink()
        assert V._own_source_tree(str(pkg), "nopyproject") is False


class TestGitState:

    def test_reports_revision_and_branch(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        out = V._git_state(str(pkg), "0.0.1", "mypkg")
        assert out is not None and out.startswith(" git:")
        assert _head(pkg).startswith(
            out.split("git:")[1].split()[0].removesuffix("-dirty"))

    def test_none_outside_a_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert V._git_state(str(plain), "0.0.1", "mypkg") is None

    def test_none_for_an_unrelated_repo(self, tmp_path):
        pkg = _repo(tmp_path, "someone-elses-app")
        assert V._git_state(str(pkg), "0.0.1", "aiomoqt") is None

    def test_dirty_marks_uncommitted_edits(self, tmp_path):
        """The signal that a rebuilt-but-uncommitted tree is running."""
        pkg = _repo(tmp_path, "mypkg")
        clean = V._git_state(str(pkg), "0.0.1", "mypkg")
        assert "-dirty" not in clean
        (pkg / "__init__.py").write_text("# edited\n")
        assert "-dirty" in V._git_state(str(pkg), "0.0.1", "mypkg")

    def test_stale_when_version_names_another_commit(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        out = V._git_state(str(pkg), "0.1.0.dev3+gdeadbee", "mypkg")
        assert "STALE" in out

    def test_not_stale_when_version_names_head(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        out = V._git_state(str(pkg), f"0.1.0.dev3+g{_head(pkg)[:9]}", "mypkg")
        assert "STALE" not in out

    def test_no_version_hash_is_not_stale(self, tmp_path):
        """A plain release version names no commit, so it cannot disagree."""
        pkg = _repo(tmp_path, "mypkg")
        assert "STALE" not in V._git_state(str(pkg), "0.12.0", "mypkg")


class TestReporterDegradation:
    """print_versions must never fail, whatever the install shape.

    Assertions are scoped to the aiomoqt line: print_versions delegates
    to aiopquic's reporter, whose own module state these patches do not
    reach."""

    def _aiomoqt_line(self, monkeypatch):
        buf = io.StringIO()
        V.print_versions(file=buf)
        out = buf.getvalue()
        assert "aiomoqt:" in out
        return next(ln for ln in out.splitlines() if ln.startswith("aiomoqt:"))

    def test_without_git_falls_back_to_metadata(self, monkeypatch):
        monkeypatch.setattr(V, "_git", lambda *a, **k: None)
        assert "git:" not in self._aiomoqt_line(monkeypatch)

    def test_wheel_install_consults_no_git(self, monkeypatch):
        """A published wheel already carries an accurate build-time
        version; consulting git could only misattribute one."""
        monkeypatch.setattr(V, "_is_editable", lambda dist: False)
        called = []
        monkeypatch.setattr(V, "_git",
                            lambda *a, **k: called.append(a) or None)
        assert "git:" not in self._aiomoqt_line(monkeypatch)
        assert called == []
