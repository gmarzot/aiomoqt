"""sub_media keeps a track whose timing properties are malformed when
diagnosing (--analyze reports them); otherwise it cancels, as the library
default does (MOQT §12.1)."""
import sys

import pytest

from aiomoqt.tools import sub_media


@pytest.mark.parametrize("argv, policy", [
    ([], "cancel"),
    (["--analyze"], "report"),
    (["--analyze", "--timestamp-errors", "cancel"], "cancel"),
    (["--timestamp-errors", "report"], "report"),
])
def test_timestamp_policy(monkeypatch, argv, policy):
    monkeypatch.setattr(sys, "argv", ["sub_media", "moqt://localhost:4433", "-N", "a/b", *argv])
    assert sub_media._timestamp_policy(sub_media.parse_args()) == policy
