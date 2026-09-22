# AGENTS.md

`aiomoqt` — asyncio implementation of IETF MoQT (Media over QUIC Transport), layered on
`aiopquic` (picoquic binding). Pure Python; the transport is a binary wheel dependency.

## Layout

| Path | What |
|---|---|
| `aiomoqt/protocol.py` | session state machine, control + data plane. One 200 KB file |
| `aiomoqt/client.py`, `server.py` | `MOQTClient`; `MOQTServer.serve()` / `serve_dual()` |
| `aiomoqt/track.py` | `Track`, `PublishedTrack`, `SubscribedTrack`, `VideoTrack` |
| `aiomoqt/delivery.py`, `fanout.py` | `StreamMapping`, `SubgroupDelivery`, `FanoutDelivery` |
| `aiomoqt/messages/` | sans-I/O encode/decode; `messages/d18/` for draft-18 shapes |
| `aiomoqt/types.py`, `context.py` | draft numbers, error codes, `DraftProfile`, `profile_for()` |
| `aiomoqt/media/` | MSF catalog, LOC, CMSF/CMAF packaging |
| `aiomoqt/tools/` | CLI tools — bench, pub/sub media, interop relay, relay probe |
| `aiomoqt/tests/` | the pytest tree — **inside** the package |
| `tests/` | `release_regression_test.py` (tier runner) + `relays.json`. No pytest files |

## Setup

Python >= 3.12 (CI: 3.12 / 3.13 / 3.14 on linux and macos).

    uv pip install -e ".[test]"      # ".[dev]" adds ruff + ty

Editable is not optional. The loopback suites and standalone bench scripts resolve `certs/`
and sibling modules relative to the working tree; a non-editable install skips them with
"TLS certs not found in certs/". Version comes from `setuptools_scm` (git tags) — a shallow
clone without tags builds as `0.0.0+unknown`, which is why CI uses `fetch-depth: 0`.

`pytest` writes `certs/` on first run (`aiomoqt/tests/conftest.py`, best-effort via openssl).
The bench tools do not; generate them by hand — see README "Development".

## Tests

`tests/release_regression_test.py` is what CI runs. Tiers group suites; `--test-suite` runs
one suite directly, `--skip-suite` drops one from a selected tier.

    python tests/release_regression_test.py --test-tier unit --test-tier integration
    python tests/release_regression_test.py --test-suite loopback-fetch
    python tests/release_regression_test.py --test-tier interop --interop-parallel 4

`unit` = the whole pytest tree, one run. `integration` = tools, multi-process paths and the
draft × transport matrix over localhost. `interop` = live relays from `tests/relays.json`
(manual / weekly — it reaches public infrastructure; ask before running it). `bench` =
manual, measurement only. Exit 0 iff every suite passed; `[skip]` does not count as a pass.

Plain pytest works too (`pytest.ini`: `testpaths = aiomoqt/tests`, `asyncio_mode=auto`):

    python -m pytest -q aiomoqt/tests

Always `python -m pytest`, never the `pytest` binary — `-m` puts cwd on `sys.path` so an
installed copy in site-packages cannot shadow the source tree and silently skip tests.

CI (`.github/workflows/ci.yml`): `core` = unit + integration on the full matrix, macOS adding
`--skip-suite loopback-fetch` (the native QUIC close drain stalls ~10 s on an un-drained
fetch stream); `multi-proc` = `pub_server` against `sub_bench`; `peer-interop` = cloudflare
`moq-rs` plus the `ghcr.io/openmoq/moqx` image; `microbenchmark` is continue-on-error.
`moq-conformance.yml` scores us against moxygen's moq-test client, pinned in
`.github/moxygen-pin`. Zero objects delivered is a failure, never a pass — an assertion that
cannot find its results line fails loudly rather than vacuously.

## Lint and types

`[tool.ruff]` in `pyproject.toml`: `line-length = 100`, `select = ["E","F","W"]`,
`ignore = ["E501"]`. `ruff` and `ty` are `[dev]` extras, run locally; there is no CI lint job.
Do not run `ruff format` across existing files — it reflows code the change never touched.

The package ships `py.typed`, so downstream `mypy --strict` follows these annotations. Keep
externally-callable defs annotated. Checking types from the repo root reports hundreds of
errors in our own internals — that is mypy treating the package as local source, not a real
downstream result; check against an installed wheel in a clean venv instead. `aiopquic` ships
no `py.typed`, so QUIC-facing signatures degrade to `Any` downstream.

## Conventions

**CLI grid.** `aiomoqt/utils/cli.py` defines one flag grid every tool builds from: `-N`
namespace, `-T` trackname, `-s` object size, `-g` group size, `-P` streams, `-r` rate, `-t`
duration, `-i` interval, `-k` insecure, `-d` debug, `-D` datagram, `-p` port, `-Q` raw QUIC,
`-W` WebTransport. `-h` is deliberately not help — it stays free for hosts and URLs; `-?` is
help. Two rules, both enforced by `aiomoqt/tests/test_cli_grid.py`:

- One letter, one meaning, across every tool and example. Re-using `-r` for anything but rate
  fails the cross-tool introspection test. `GRID_EXEMPT` is empty; the only tolerated
  collisions are enumerated in `KNOWN_GRID_VIOLATIONS`.
- **No feature toggles on the grid helpers.** `add_media(streams=False, datagram=True, ...)`
  is the anti-pattern: it centralizes the help text while each call site still picks its own
  subset and defaults, so the flags drift apart one level up. A tool needing a different
  subset gets a different group.

A dialed URL's scheme selects the transport (`moqt://` raw QUIC, `https://` or bare
`host:port` WebTransport) — there is no `-q`. Listeners use `-Q`/`-W`; both together serve one
port via per-connection ALPN dispatch.

**Fan-out layering.** Peer mechanics live in `PublishedTrack` and `aiomoqt/delivery.py`.
Packagers under `aiomoqt/media/` only package and number objects; `broadcast.py` is the single
exempt composer. `test_packaging_does_not_know_about_peers` greps the media tree for
`FanoutDelivery`, `add_session`, `_subs` and friends and fails if one appears.

**Drafts.** 14, 16 and 18 are all live. Draft numbers are plain ints everywhere
(`MOQTDraft`, the `draft=` kwarg, `supported_drafts`); the IETF code `0xff0000NN` and the ALPN
(`moq-00` for d14, `moqt-NN` for d16+) exist only at the wire boundary. Default offer is
newest-first `[18, 16, 14]`. Route behavior differences through `profile_for(draft)` /
`DraftProfile` in `context.py` rather than scattering version checks. New wire shapes need a
case in `test_wire_conformance.py` — including malformed ones, since the Cython and Python
codecs can agree on valid bytes while diverging on invalid ones.

**CHANGELOG.md** is hand-written and fine-grained: one short line per change under
`## Unreleased`, no narration. Commits are `area: short imperative summary`, e.g.
`relay: every request gets a terminal reply`. Comments say what the code does or why a
non-obvious choice is necessary, in as few words as possible — no issue references, no
"was X, now Y", no process narration. History belongs to git and the CHANGELOG, not the source.
Same standard for workflow YAML and Dockerfiles.

## API boundary

Import from `aiomoqt.client` (`MOQTClient`), `aiomoqt.server` (`MOQTServer`), `aiomoqt.track`
(`PublishedTrack`, `SubscribedTrack`), `aiomoqt.delivery` (`StreamMapping`), `aiomoqt.media`
(its `__all__`), `aiomoqt.types` (`MOQTMessageType`, `ParamType`, `SetupParamType`,
`MOQTRequestError`), and `aiomoqt.messages` when building frames explicitly.

Do not reach into `_MOQTSessionMixin` or the other `_`-prefixed classes in `protocol.py`,
`session._*` attributes, `track._production` / `track._out` / `_start_generating` (tests use
these; nothing else should), or `aiomoqt.utils.workers`. `protocol.py` is where the session
state machine belongs — extend it there instead of driving it from outside.

Prefer the track layer over hand-driving the session: `PublishedTrack` owns stream setup,
subgroup writing, pacing and the TX budget; `SubscribedTrack` owns reassembly and FETCH/JOIN.

## Known traps

- Default CC is `bbr1`, which spikes latency on paced flows (BBRv1 ProbeRTT, ~200 ms every
  10 s under CPU jitter). `--cc-algo` also takes `bbr` (v3), `cubic`, `newreno`, `dcubic`,
  `prague` (L4S/ECN) and `fast`. `bbr` is the likely future default and what moqx runs, but
  picoquic's v3 freezes cwnd below 128 µs RTT (upstream #2118) — that is loopback, so it is
  the one place not to use it. For clean timing measurements on loopback use `cubic` or
  `newreno`; loss-based CCs do collapse on the GIL-induced loss blips of a loaded host, so
  prefer them for timing, not for throughput.
- WebTransport datagram TX needs `aiopquic >= 0.4.1`. Against 0.4.0 — what PyPI serves, and
  what this tree floors to until 0.4.1 ships — `StreamMapping.DATAGRAM` fails over
  WebTransport; use `PER_GROUP`. Datagram RX works on both transports either way.
- `.github/aiopquic-pin` (`owner/repo@ref`) builds aiopquic from source so a PR can pair with
  an unreleased aiopquic change. Fine while paired — the `pin-guard` job hard-fails it on main
  or a release-labeled PR, because it would ship a dependency nobody can install.
- `relay_probe -f` and `tests/relays.json` use different schemas; use `--url` for single probes.
- `moq_interop_relay` is a conformance fixture, not a production relay: no group cache, no
  authz, no backpressure.

## When 0.12.0 lands

This file describes the 0.11.1 tree. Revisit the API-boundary section (a new top-level package
changes "what should I import"), the layout table, and the draft matrix if a newer draft
arrives.
