# AGENTS.md

Guide for agents using the installed `aiomoqt` package: an asyncio implementation of IETF MoQT
(Media over QUIC Transport) over `aiopquic` (picoquic binding).

## API boundary

Import from `aiomoqt.client` (`MOQTClient`), `aiomoqt.server` (`MOQTServer`), `aiomoqt.track`
(`PublishedTrack`, `SubscribedTrack`), `aiomoqt.delivery` (`StreamMapping`), `aiomoqt.media`
(its `__all__`), `aiomoqt.types` (`MOQTMessageType`, `ParamType`, `SetupParamType`,
`MOQTRequestError`), and `aiomoqt.messages` when building frames explicitly.

`aiomoqt.agent` (its `__all__`) is the agent-facing API: declarative specs that round-trip
through JSON, run by `AgentSession` as `Reader` / `Writer` handles. It is provisional through
0.13.x: names and fields may change without a deprecation cycle.

Do not reach into `_MOQTSessionMixin` or the other `_`-prefixed classes in `protocol.py`,
`session._*` attributes, `track._production` / `track._out` / `_start_generating`, or
`aiomoqt.utils.workers`. Prefer the track layer over hand-driving the session:
`PublishedTrack` owns stream setup, subgroup writing, pacing and the TX budget;
`SubscribedTrack` owns reassembly and FETCH/JOIN. The package ships `py.typed`.

## Drafts

MoQT drafts 14, 16, 18, 20 and 22 are supported; odd-numbered drafts are not. Draft 14 is
deprecated and will be removed in 0.15.0. Draft numbers are
plain ints (`draft=18`, `supported_drafts=[18, 16]`). Clients offer newest-first
`MOQT_DEFAULT_DRAFTS = (22, 20, 18, 16, 14)` unless told otherwise; tools take `--draft`.

## CLI tools

Console scripts: `moq-pub-bench`, `moq-sub-bench`, `moq-loopback-bench`, `moq-adaptive-bench`,
`moq-pub-server`, `moq-load-sim`, `moq-relay-probe`, `moq-interop-client`,
`moq-interop-relay`, `aiomoqt-versions`. Others run as modules: `python -m
aiomoqt.tools.pub_media` / `sub_media` (MSF media; `--input` ingest needs the `media` extra),
`aiomoqt.tools.moqtest_origin`.

Most tools share one flag grid from `aiomoqt.utils.cli` (listed in its module docstring):
`-N` namespace, `-T` trackname, `-s` object size, `-g` group size, `-P` streams, `-r` rate,
`-t` duration, `-i` interval, `-k` insecure, `-d` debug, `-D` datagram, `-p` port, `--draft`.
A dialed URL's scheme selects the transport: `moqt://host:port/path` is raw QUIC,
`https://host:port/path` or bare `host:port` is WebTransport. Listeners take `-Q` (raw QUIC),
`-W` (WebTransport), or both on one port. `moq-interop-client` follows the interop runner's
flags instead. `-h` (also `-?`, `--help`) prints help on every tool.

## Logging

Levels: ERROR, WARNING, INFO (one line per session event and control message), DEBUG (second
views, transport plumbing, per-stream lifecycle; bounded however many objects flow) and TRACE
(`aiomoqt.utils.logger.TRACE`, 5: per object, per chunk, per datagram, wire hex). Category
loggers, stable names: `aiomoqt.control`, `aiomoqt.data`, `aiomoqt.fetch`, `aiomoqt.transport`,
`aiomoqt.relay`; other lines use module loggers (`aiomoqt.protocol`, ...). aiomoqt loggers do not
propagate to the root logger: attach a handler with `aiomoqt.utils.logger.set_log_handler()`.

Tools take `--log-level {error,warning,info,debug,trace}` (`-d` = `debug`) and `--log
CAT=LEVEL[,...]`, e.g. `--log data=trace`. The `AIOMOQT_LOG` environment variable takes the same
spec, plus a bare `LEVEL` for every logger (`AIOMOQT_LOG=debug,data=trace`); tool flags win. In
code: `set_log_level(level)` and `set_logger_levels({"data": "trace"})`, or
`configure_logging(level, spec)`. `moq-interop-relay` steps INFO → DEBUG → TRACE → INFO on
SIGUSR1.

## Known traps

- TLS: clients verify the relay's certificate by default. A self-signed relay needs `-k` /
  `verify_tls=False`; a private CA goes in `ca_file=`.
- Congestion control: the default is `bbr` (BBRv3) everywhere: `MOQTClient`, `MOQTServer`,
  `aiomoqt-relay`, the tools and the examples, whichever aiopquic is installed.
  `congestion_control_algorithm=None` defers to a passed `configuration` or to aiopquic's own
  default. moqx defaults to BBR2. `--cc-algo` takes `bbr`, `bbr1`, `c4`, `cubic`, `dcubic`,
  `fast`, `newreno`, `prague` and `reno`. `--cc-options` / `congestion_control_options=`
  passes an option string to that algorithm, e.g. `Q0.02` for bbr's send quantum.
- Loopback timing: picoquic's BBRv3 is reported to freeze cwnd below 128 µs RTT (upstream
  #2118), not reproduced on the current pin. Use `cubic` or `newreno` for loopback timing runs.
- FETCH: a `PublishedTrack` keeps no object history unless given `fetch_history_bytes`, and
  refuses FETCH (NOT_SUPPORTED) without one; the MSF catalog track keeps 64 KiB.
- Ending a track: a `produce()` track sends End of Track after its last object, then
  PUBLISH_DONE TRACK_ENDED, when `produce()` returns or on `end()`; `end()` cancels `produce()`
  at its next await, so keep each object's write whole. A generator track (no `produce()`)
  resets its open streams on `end()` and sends no End of Track.
- `moq-relay-probe --relays-file` reads its own relay-list schema; use `--url` for a single probe.
- `aiomoqt-relay` (also `moq-interop-relay`) is a MoQT relay in Python: a bounded
  recent-object cache (8 groups / 32 MiB per track, all tracks under one `--cache-bytes` budget,
  least recently used evicted first; objects charged payload, properties and record overhead) for FETCH and JOIN; no authentication, authorization or rate limiting; a per-stream TX budget
  and a bounded send lane per subscriber, shedding whole groups (streams reset) from one that falls
  behind, and logging a `lane stalled` WARNING for one that makes no progress for 10 s;
  per-session caps on namespace requests and SUBSCRIBEs (`--max-namespaces-per-session`,
  `--max-subscriptions-per-session`), refused past them with EXCESSIVE_LOAD; tracks no subscriber
  has forwarded for `--pause-after` (60 s) paused upstream until the next one; objects past the
  OBJECT_DELIVERY_TIMEOUT or MAX_CACHE_DURATION, or live ones past `--lag-limit` (5 s) when
  neither is set, dropped with their subgroup reset; cached objects past MAX_CACHE_DURATION
  are fetched upstream, not served. Its module docstring has the details.
