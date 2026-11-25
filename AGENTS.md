# AGENTS.md

Contributor guide for `aiomoqt`, an asyncio implementation of IETF MoQT on `aiopquic` (picoquic
binding). `aiomoqt/AGENTS.md` ships in the wheel for agents using the installed package; keep its
API boundary and traps consistent with this file.

Read when the task touches it:
- `docs/dev/layout.md`: what lives where
- `docs/dev/conventions.md`: CLI grid, logging, drafts, routing and layering in full; lint and types
- `docs/dev/ci-and-release.md`: test tiers, CI workflows, pins, release flow, versions
- `TESTING.md`: tiers, suites, relay catalog, manual runs

## Setup

    uv pip install -e ".[test,media]"      # ".[dev]" adds ruff + ty

Python >= 3.12. Install editable: the loopback suites and benches resolve `certs/` from the
working tree.

## Tests

    python tests/release_regression_test.py --test-tier unit --test-tier integration
    python tests/release_regression_test.py --test-suite loopback-fetch
    python -m pytest -q aiomoqt/tests

- `python -m pytest`, never the `pytest` binary: an installed copy can shadow the source tree
  and silently skip tests.
- A runner run with no `--test-tier` or `--test-suite` includes interop. The interop tier,
  `relay_probe`, `moq_interop_client`, `media-locmaf` and `moqdev-smoke` reach public
  infrastructure: run them from CI or a remote host, not a laptop.
- Zero objects delivered is a failure, never a pass. An assertion that cannot find its results
  line fails loudly.
- Per-track or per-request state gets a test with two tracks on one session, over raw QUIC and
  WebTransport (`test_multi_track_publish.py`; through the relay, `test_relay_forwarding.py`).

## Rules

- Work lands on the dev branch `gmarzot-X.Y.Z`, which has a tracking PR to main.
- `protocol.py` owns the session state machine: extend it there, don't drive it from outside.
- Drafts 16, 18, 20, 22, as plain ints; wire codes and ALPN only at the wire boundary. Branch on
  `profile_for(draft)` / `DraftProfile`, not version checks. A new wire shape needs a
  `test_wire_conformance.py` case, malformed input included.
- Track and request messages route through the session's track and request tables
  (`PublishedTrack.attach()`, `_bind_request()`), not `register_handler`, which holds one
  handler per message type for the whole session.
- Peer mechanics live in `PublishedTrack` and `delivery.py`. Packagers under `aiomoqt/media/`
  only package and number objects; `broadcast.py` is the one exempt composer.
- CLI flags come from the grid in `aiomoqt/utils/cli.py`; a short flag means one thing across all
  tools and examples (`test_cli_grid.py`). Entry points use `cli.run`, not `asyncio.run`.
- Logging: INFO is one line per session event; DEBUG's volume does not grow with object count;
  per-object output is TRACE, behind `isEnabledFor(TRACE)` on hot paths. Use the category
  loggers (`aiomoqt.control`, `.data`, `.fetch`, `.transport`, `.relay`, `.moq_fabric`).
- ruff must pass. Never run `ruff format` across existing files.
- Keep externally-callable defs annotated (`py.typed` ships).
- One short CHANGELOG line per user-visible change; commits `area: short imperative summary`;
  comments say what or why in few words, never history.
