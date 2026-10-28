# probe_alpn.py — throwaway forced-ALPN relay prober.
# Forces ONE ALPN (overriding aiomoqt.types/client.moqt_alpn_for_version) and
# runs relay_probe against the shared relay, to learn which ALPNs it accepts.
# Used for env-freeze.md §8; not part of the harness.
#
"""Throwaway: probe the shared moqx relay with a forced ALPN string.

Patches aiomoqt's version->ALPN mapping so the client offers exactly one
candidate ALPN, then runs relay_probe's normal --url path. Used to discover
which ALPN set the running (Oct-22 build) relay actually accepts.

usage: probe_alpn.py ALPN DRAFT
"""
import sys

CAND = sys.argv[1]
DRAFT = sys.argv[2]

import aiomoqt.types as T  # noqa: E402
import aiomoqt.client as C  # noqa: E402

T.moqt_alpn_for_version = lambda v: CAND
C.moqt_alpn_for_version = lambda v: CAND

sys.argv = ["relay_probe", "--url", "moqt://127.0.0.1:9668", "--draft", DRAFT, "-k"]

from aiomoqt.tools.relay_probe import cli  # noqa: E402

cli()
