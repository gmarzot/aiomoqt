# Conventions

The full rules behind the summary in `AGENTS.md`.

**CLI grid.** `aiomoqt/utils/cli.py` defines the shared flag grid; its module docstring lists
it. `test_cli_grid.py` checks every tool in `TOOLS` and every example: a grid short flag means
only its grid meaning, and any short flag means one thing across all of them. There are no
exceptions; `moq_interop_client`'s `-r/-t/-l/-v` are the interop runner's interface, listed in
`EXTERNAL_CONTRACT` and tested against it. The `add_*` groups take no toggles: arguments set
defaults, never which flags exist (also tested). A dialed URL's
scheme selects the transport (`moqt://` raw QUIC, `https://` or bare `host:port`
WebTransport); listeners use `-Q`/`-W`, and both together serve one port via ALPN dispatch.
`-t` defaults to 0, no limit, on every tool and example (also tested); a tool may add an exit condition with `--exit COND`.
`-h`, `-?` and `--help` print help on every tool and example (`cli.add_help`; the test requires
all three). Write `-h` in docs: `-?` is a shell glob.
Entry points run their coroutine with `cli.run`, not `asyncio.run`: Ctrl-C cancels the main
task from the event loop, between callbacks (`test_cli_run.py`). Tools that install their own
SIGINT handler are listed there.

**Fan-out layering.** Peer mechanics live in `PublishedTrack` and `aiomoqt/delivery.py`.
Packagers under `aiomoqt/media/` only package and number objects; `broadcast.py` is the single
exempt composer. `test_packaging_does_not_know_about_peers` greps the media tree for
`FanoutDelivery`, `add_session`, `_subs` and friends and fails if one appears.

**Per-track routing.** `register_handler` holds one handler per message type for the whole
session, so a track that registers one takes it from every other track. Messages addressed to a
track or a request (SUBSCRIBE, PUBLISH_OK, updates) route through the session's track and
request tables (`PublishedTrack.attach()`, `_bind_request()`), which take precedence over app
handlers. `test_published_tracks_take_no_session_handler_slot` enforces this for `track.py`.

**Drafts.** 16, 18, 20 and 22 are all live; odd-numbered drafts are not supported.
Draft numbers are plain ints everywhere (`MOQTDraft`, the `draft=` kwarg, `supported_drafts`); the
IETF code `0xff0000NN` and the ALPN (`moqt-NN`) exist only at the
wire boundary. Default offer is newest-first `MOQT_DEFAULT_DRAFTS = (22, 20, 18, 16)`.
Route behavior differences through `profile_for(draft)` / `DraftProfile` in `context.py`
rather than scattering version checks. New wire shapes need a case in
`test_wire_conformance.py`, including malformed ones: aiopquic's Cython codecs and our Python
codecs can agree on valid bytes while diverging on invalid ones.

**Logging.** INFO tells a session's story, one line per event: setup (`setup complete:
draft-N`) and close; each control message where it crosses the wire (`MOQT send:` out,
`control message parsed:` in); and request or track outcomes no message carries (cancellations,
refusals, forward-state changes, `Track:` and `relay:` summaries). DEBUG is a second view of a
message already logged, transport plumbing, per-stream lifecycle (open, close, reset) and request
or track bookkeeping; its volume does not grow with the object count. TRACE (`TRACE = 5` in
`aiomoqt.utils.logger`, registered by name only) is per object, per chunk, per datagram and wire
hex: emit it as `log.log(TRACE, …)`, behind `log.isEnabledFor(TRACE)` on hot paths. WARNING is a
tolerated peer deviation, a bound hit, or a session we close for the peer's error; ERROR is a
session we end for our own failure (INTERNAL_ERROR).

Category loggers, stable names: `aiomoqt.control` (control messages, requests), `aiomoqt.data`
(subgroup streams, datagrams, objects in and out), `aiomoqt.fetch` (FETCH streams and objects),
`aiomoqt.transport` (QUIC and WebTransport events, stream ops, ALPN), `aiomoqt.relay` (the
relay), `aiomoqt.moq_fabric` (moq-fabric sessions). A line that belongs to none uses its
module logger. Tools take `--log-level {error,warning,info,debug,trace}` (`-d`/`--debug` =
`debug`) and `--log CAT=LEVEL[,…]` from the
grid (`add_logging`, `apply_logging`); `AIOMOQT_LOG` takes the same spec, where a bare `LEVEL`
sets every logger, and the flags win. `MOQTServer(quiet_peers=…)` logs a health probe's sessions
at DEBUG. The relay's SIGUSR1 steps INFO → DEBUG → TRACE → INFO; its `--control` socket's
`POST /debug` toggles DEBUG.

**CHANGELOG, commits, comments.** One short CHANGELOG line per user-visible change; commits
`area: short imperative summary`; comments say what or why in few words, never history.

## Lint and types

`[tool.ruff]` in `pyproject.toml`: `line-length = 100`, `select = ["E","F","W"]`,
`ignore = ["E501"]`, with per-file ignores for the star-import modules. CI's `lint` job runs a
pinned ruff and must pass. `types` runs a pinned `ty` and reports a count above
`.github/ty-baseline` without failing the run; lower the baseline as diagnostics are fixed.
Do not run `ruff format` across existing files: it reflows code the change never touched.

The package ships `py.typed`, so downstream `mypy --strict` follows these annotations. Keep
externally-callable defs annotated. Checking types from the repo root reports hundreds of
errors in our own internals; check against an installed wheel in a clean venv instead.
`aiopquic` ships no `py.typed`, so QUIC-facing signatures degrade to `Any` downstream.
