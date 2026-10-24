# AGENTS.md

Contributor guide for the `aiomoqt` repository. `aiomoqt` is an asyncio implementation of IETF
MoQT (Media over QUIC Transport), layered on `aiopquic` (picoquic binding). Pure Python; the
transport is a binary wheel dependency.

`aiomoqt/AGENTS.md` is a separate, shorter guide that ships in the wheel, for agents using the
installed package. Keep its API boundary and traps consistent with this file.

## Layout

| Path | What |
|---|---|
| `aiomoqt/protocol.py` | session state machine, control + data plane (~300 KB) |
| `aiomoqt/client.py`, `server.py` | `MOQTClient`; `MOQTServer.serve()` / `serve_dual()` |
| `aiomoqt/track.py` | `Track`, `PublishedTrack`, `SubscribedTrack`, `VideoTrack` |
| `aiomoqt/delivery.py` | `StreamMapping`, `ObjectHistory`, `SubgroupDelivery`, `FanoutDelivery` |
| `aiomoqt/fanout.py` | `FanoutPublisher`: one MSF broadcast on several relays |
| `aiomoqt/messages/` | sans-I/O encode/decode; `messages/d18/`, `messages/d20/` for draft-specific shapes (d22 shares d20's) |
| `aiomoqt/agent/` | declarative, JSON-round-tripping specs and `AgentSession` / `Reader` / `Writer` |
| `aiomoqt/types.py`, `context.py` | draft numbers, error codes, `DraftProfile`, `profile_for()` |
| `aiomoqt/media/` | MSF catalog, `broadcast.py` composer, LOC, CMAF, LOCMAF packaging; ingest (PyAV, DASH, MPEG-TS) and synthetic sources |
| `aiomoqt/tools/` | CLI tools: pub/sub/loopback/adaptive benches, `pub_server`, `load_sim`, `pub_media` / `sub_media`, `relay_probe`, `moq_interop_client`, `moq_interop_relay` (ersatz relay), `moqtest_origin`, `relay_status`; `scenarios/` for `load_sim` |
| `aiomoqt/utils/` | CLI grid (`cli.py`), stats, URL parsing, logging, bench workers |
| `aiomoqt/examples/` | minimal pub, sub, join and server examples; ship in the wheel |
| `aiomoqt/versions.py` | `aiomoqt-versions`: aiomoqt, aiopquic, picoquic and picotls build info |
| `aiomoqt/tests/` | the pytest tree, **inside** the package dir, left out of the wheel |
| `tests/` | `release_regression_test.py` (tier runner) + `relays.json`; `contrib/` = contribution-runner sweep; `moqdev-smoke/` = moq-dev's relay smoke in Docker. No pytest files |
| `deploy/aiomoqt-relay/` | compose, nginx and runbook for the public ersatz relay |
| `docs/` | bench and demo runbooks |

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

- `ci.yml` (pull requests, push to main): `pin-guard`; `core` = unit + integration on the full
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
2. Squash-merge the tracking PR to main.
3. Dispatch `release.yml` on main with the version (`N.N.N`, optionally `a`/`b`/`rcN`).
   `verify` requires a green `ci.yml` run and a green `moq-conformance.yml` run on a commit with
   the identical tree; it does not re-run them. The wheel is built once and smoke-tested, then
   tagged and published. `dry_run` stops after the smoke test.
4. Merge main back into the dev branch, then open the next dev branch and its tracking PR.

Version trap: release tags sit on squash commits that are not ancestors of the dev branch, so
dev builds report a stale base such as `0.13.0rc3.devN`. Check releases with
`git diff <commit> <tag>`, not ancestry.

## Lint and types

`[tool.ruff]` in `pyproject.toml`: `line-length = 100`, `select = ["E","F","W"]`,
`ignore = ["E501"]`. `ruff` and `ty` are `[dev]` extras, run locally; there is no CI lint job.
Do not run `ruff format` across existing files: it reflows code the change never touched.

The package ships `py.typed`, so downstream `mypy --strict` follows these annotations. Keep
externally-callable defs annotated. Checking types from the repo root reports hundreds of
errors in our own internals; check against an installed wheel in a clean venv instead.
`aiopquic` ships no `py.typed`, so QUIC-facing signatures degrade to `Any` downstream.

## Conventions

**CLI grid.** `aiomoqt/utils/cli.py` defines the shared flag grid; its module docstring lists
it. `test_cli_grid.py` checks every tool in `TOOLS` and every example: a grid short flag means
only its grid meaning, and any short flag means one thing across all of them. There are no
exceptions; `moq_interop_client`'s `-r/-t/-l/-v` are the interop runner's interface, listed in
`EXTERNAL_CONTRACT` and tested against it. The `add_*` groups take no toggles: arguments set
defaults, never which flags exist (also tested). A dialed URL's
scheme selects the transport (`moqt://` raw QUIC, `https://` or bare `host:port`
WebTransport); listeners use `-Q`/`-W`, and both together serve one port via ALPN dispatch.
`-h`, `-?` and `--help` print help on every tool and example (`cli.add_help`; the test requires
all three). Write `-h` in docs: `-?` is a shell glob.

**Fan-out layering.** Peer mechanics live in `PublishedTrack` and `aiomoqt/delivery.py`.
Packagers under `aiomoqt/media/` only package and number objects; `broadcast.py` is the single
exempt composer. `test_packaging_does_not_know_about_peers` greps the media tree for
`FanoutDelivery`, `add_session`, `_subs` and friends and fails if one appears.

**Per-track routing.** `register_handler` holds one handler per message type for the whole
session, so a track that registers one takes it from every other track. Messages addressed to a
track or a request (SUBSCRIBE, PUBLISH_OK, updates) route through the session's track and
request tables (`PublishedTrack.attach()`, `_bind_request()`), which take precedence over app
handlers. `test_published_tracks_take_no_session_handler_slot` enforces this for `track.py`.

**Drafts.** 14, 16, 18, 20 and 22 are all live; odd-numbered drafts are not supported. Draft
numbers are plain ints everywhere (`MOQTDraft`, the `draft=` kwarg, `supported_drafts`); the
IETF code `0xff0000NN` and the ALPN (`moq-00` for d14, `moqt-NN` for d16+) exist only at the
wire boundary. Default offer is newest-first `MOQT_DEFAULT_DRAFTS = (22, 20, 18, 16, 14)`.
Route behavior differences through `profile_for(draft)` / `DraftProfile` in `context.py`
rather than scattering version checks. New wire shapes need a case in
`test_wire_conformance.py`, including malformed ones: aiopquic's Cython codecs and our Python
codecs can agree on valid bytes while diverging on invalid ones.

**Logging.** INFO tells a session's story, one line per event: setup (`setup complete:
draft-N`) and close; each control message where it crosses the wire (`MOQT send:` out,
`control message parsed:` in); and request or track outcomes no message carries (cancellations,
refusals, forward-state changes, `Track:` and `relay:` summaries). DEBUG is a second view of a
message already logged, transport plumbing, and anything per object or per data stream. WARNING
is a tolerated peer deviation or a bound hit; ERROR ends a session. `MOQTServer(quiet_peers=…)`
logs a health probe's sessions at DEBUG; the relay's SIGUSR1 toggles DEBUG at runtime.

**CHANGELOG, commits, comments.** One short CHANGELOG line per user-visible change; commits
`area: short imperative summary`; comments say what or why in few words, never history.

## API boundary and traps

The import boundary, internals not to reach into, and user-facing traps are in
`aiomoqt/AGENTS.md`. `protocol.py` is where the session state machine belongs: extend it there
instead of driving it from outside.
