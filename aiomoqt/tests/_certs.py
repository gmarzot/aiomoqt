"""Canonical TLS test-cert locations, resolved independent of import mode.

The loopback suites need a self-signed cert at ``<repo>/certs/``. Anchoring
that path on a module's ``__file__`` is fragile: the same test module can be
imported under two identities (a pytest-collected top-level module and, via a
cross-import, ``aiomoqt.tests.<mod>``), and a non-editable install adds a third
copy under site-packages — each with a different ``__file__`` and therefore a
different ``certs/`` dir. That divergence silently skips suites whose certs
"can't be found" even though they exist.

Resolution order (first hit wins):
  1. ``AIOMOQT_TEST_CERTS_DIR`` — exported by ``conftest.pytest_configure``
     from the pytest rootdir, so every importer agrees regardless of how the
     module was loaded.
  2. walk upward from this file to the first parent containing ``certs/``.
  3. the conventional ``<repo>/certs`` fallback (used when neither applies).
"""
import os

import pytest


def _resolve_certs_dir():
    env = os.environ.get("AIOMOQT_TEST_CERTS_DIR")
    if env:
        return os.path.realpath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    while True:
        candidate = os.path.join(cur, "certs")
        if os.path.isdir(candidate):
            return os.path.realpath(candidate)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.realpath(os.path.join(here, "..", "..", "certs"))


CERTS_DIR = _resolve_certs_dir()
CERT = os.path.join(CERTS_DIR, "cert.pem")
KEY = os.path.join(CERTS_DIR, "key.pem")

requires_certs = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)
