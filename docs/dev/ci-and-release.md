# Setup, tests, CI and release

Detail behind the summary in `AGENTS.md`.

## Setup

Python >= 3.12 (CI: 3.12 / 3.13 / 3.14 on linux and macos).

    uv pip install -e ".[test,media]"      # ".[dev]" adds ruff + ty

`media` (PyAV) is optional; without it the media tests skip. `test` brings `jsonschema` for the
agent-spec tests.
Install editable: the loopback suites and bench scripts resolve `certs/` relative to the working
tree. `pytest` writes `certs/` on first run; for the bench tools see README "Development".
Version comes from `setuptools_scm` (git tags): a shallow clone without tags builds as
`0.0.0+unknown`, which is why CI uses `fetch-depth: 0`.

## Tests

`tests/release_regression_test.py` is what CI runs. Tiers group suites; `--test-suite` runs
one suite directly, `--skip-suite` drops one from a selected tier.

    python tests/release_regression_test.py --test-tier unit --test-tier integration
    python tests/release_regression_test.py --test-suite loopback-fetch

`unit` = the whole pytest tree, one run. `integration` = tools, multi-process paths and the
draft × transport matrix over localhost. `interop` = live relays from `tests/relays.json`.
`bench` = measurement only. `media-locmaf` is a manual suite (named with `--test-suite` only)
against Eyevinn's public moqlivemock. `moqdev-smoke`, also manual, runs moq-dev's relay smoke
(rust, js-web and js-bun clients) in Docker against `--moqdev-relays` (default
`aiomoqt-relay`); run it on moq-test. A run with no `--test-tier` or `--test-suite` selects
every tier, **including interop**. Exit is 1 iff some suite FAILed; SKIP and XFAIL (a failure on
a non-gating relay or draft) do not gate.

Network rule: the interop tier, a bare runner run, `relay_probe`, `moq_interop_client`,
`media-locmaf` and `moqdev-smoke` reach public infrastructure. Run them from CI or a remote
host, not a laptop.

Plain pytest works too (`pytest.ini`: `testpaths = aiomoqt/tests`, `asyncio_mode=auto`):

    python -m pytest -q aiomoqt/tests

Always `python -m pytest`, never the `pytest` binary: `-m` puts cwd on `sys.path` so an
installed copy in site-packages cannot shadow the source tree and silently skip tests.

Zero objects delivered is a failure, never a pass; an assertion that cannot find its results
line fails loudly rather than vacuously. Anything with per-track or per-request state gets a
test with at least two tracks on one session, over raw QUIC and WebTransport
(`test_multi_track_publish.py`, and through the relay in `test_relay_forwarding.py`).

## CI

- `ci.yml` (pull requests, push to main): `pin-guard`; `lint` (ruff); `types` (ty, report only);
  `core` = unit + integration on the full
  matrix, macOS adding `--skip-suite loopback-fetch` (the native QUIC close stalls ~10 s on an
  un-drained fetch stream); `multi-proc` = `pub_server` against `sub_bench`; `peer-interop` =
  cloudflare `moq-rs` plus the `ghcr.io/openmoq/moqx` image; `microbenchmark`
  (continue-on-error).
- `moq-conformance.yml` (push to `gmarzot-*` dev branches, dispatch): moxygen's moq-test client
  against our relay, pinned in `.github/moxygen-pin`. It never runs on main.
- `contrib-conformance.yml` (push to one dev branch, `ci:run-conformance` label, dispatch): our
  publisher under mondain's contribution runner (`.github/contrib-runner-pin`) against
  `tests/contrib/baseline.json`; report-only. Its push branch is hardcoded: bump it every dev
  cycle.
- `interop-regression.yml` (weekly, `ci:run-interop` label, dispatch): the interop tier.
- `release.yml` (dispatch): see below.

`.github/aiopquic-pin` (`owner/repo@ref`) builds aiopquic from source so a PR can pair with an
unreleased aiopquic change. `pin-guard` hard-fails it on main or a release-labeled PR.

## Release flow

1. Work lands on the dev branch `gmarzot-X.Y.Z`, which has an open tracking PR to main.
2. Release candidates: dispatch `release.yml` on the dev branch with `N.N.NrcN` (or `a`/`b`).
3. Final release: squash-merge the tracking PR to main, then dispatch `release.yml` on main with
   `N.N.N`.
   `verify` requires a green `ci.yml` run and a green `moq-conformance.yml` run on a commit with
   the identical tree; it does not re-run them. The wheel is built once and smoke-tested, then
   tagged and published. `dry_run` stops after the smoke test.
4. Merge main back into the dev branches. Start the next dev branch from the release tag and open
   its tracking PR.

Versions: setuptools_scm describes from the last final tag (rc, a and b tags are ignored) with
`release-branch-semver`, so a dev build reports the next minor, e.g. `0.14.0.devN` after 0.13.3;
N is not meaningful. Release tags sit on squash commits that are not ancestors of the dev
branch: check releases with `git diff <commit> <tag>`, not ancestry.
