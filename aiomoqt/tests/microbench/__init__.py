"""Microbenchmarks for aiomoqt and the aiopquic transport.

Each module is runnable as `python -m aiomoqt.tests.microbench.<name>`.
Numbers print as a single summary line per bench so they can be diffed
across branches. Most benches accept `--backend qh3|aiopquic` to pick
the transport; a few are pure-Python (parser-only) and ignore that.
"""
