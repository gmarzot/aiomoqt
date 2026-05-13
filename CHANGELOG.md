# Changelog

## v0.9.3 (2026-05-13)

Pairs with [aiopquic 0.3.1](https://pypi.org/project/aiopquic/0.3.1/);
dependency floor bumped from `aiopquic>=0.3.0` to `aiopquic>=0.3.1`.

Two stacked regressions that broke WT against every moxygen / mvfst
real relay starting in 0.9.0 (only loopback was being tested before
this release). Also picks up the receiver-side dispatch perf wins
from aiopquic 0.3.1 (Cython drain_rx coalescing, WT Queue allocation
bug, layering cleanup).

Sustained mp-loopback verification on Ryzen WSL2 (paired with
aiopquic 0.3.1, 30 s windows):

| Target | Delivered | avg lat (0.9.2 / 0.9.3) | Loss |
|---|---|---|---|
|  250 Mbps |  250 Mbps |  32 ms / **3.9 ms** | 0 |
|  500 Mbps |  506 Mbps | 567 ms / **8 ms**   | 0 |
|  980 Mbps |  987 Mbps | 3938 ms / **65 ms** | 0 |
| 1500 Mbps | 1.3-1.4 Gbps | — / ~150 ms       | 0 |

Cross-platform sanity on argo (Apple M4, native macOS):

| Target | Tx | Rx | avg lat |
|---|---|---|---|
| 250 Mbps  | 253 Mbps  | 261 Mbps  | **0.5-0.9 ms** |
| 500 Mbps  | 506 Mbps  | 526 Mbps  | **1-2 ms**     |

### Interop fixes (WT against real relays)

Both bugs were invisible in loopback testing because both ends of
the connection were aiomoqt and committed the same spec violations
symmetrically. Only real-relay interop exposed them.

**Bug 1 — `utils/url.py:91` stripped leading `/` from path**

`parsed.path.strip("/")` produced `:path: moq-relay` on the wire
instead of `:path: /moq-relay`. qh3 in 0.8.x silently re-prepended;
aiopquic does not (only normalizes empty path to "/"). Result:
HTTP/3 servers correctly rejected the malformed `:path`
pseudo-header. Fix: rstrip trailing only, then prepend `/` when
missing.

**Bug 2 — `WT-Available-Protocols` not advertised in d16+ WT**

Per moq-transport-16 §3.1: "MOQT uses ALPN in QUIC and
'WT-Available-Protocols' in WebTransport to perform version
negotiation." aiopquic was passing NULL for picowt_connect's
subprotocol arg (no `WT-Available-Protocols` header sent), so
moxygen had nothing to bind its negotiated `version_` to and fell
back to legacy CLIENT_SETUP version-array parsing, which d16
omits per spec — connection rejected.

Fix: aiopquic 0.3.1 plumbs `wt_available_protocols: list[str]`
through to `picowt_connect`. aiomoqt's `MOQTClient._connect_wt`
derives the subprotocol from the configured draft
(`["moqt-16"]` for d16+; nothing for d14 legacy WT).

**Bug 3 — Draft API conflated short int and full IETF hex**

CLI/example callers were passing the bare draft number (e.g.
`16`) into `MOQTClient(draft_version=...)`; the internal
`_moqt_version` stored that bare `16`, then compared it against
`MOQT_VERSIONS = [0xff00000e, 0xff000010]`. The comparison
always failed; ServerSetup was rejected "unsupported version
0x10".

Fix: new `moqt_version_from_draft(draft) -> int` helper in
`types.py` validates input is a recognized draft number and
normalizes to the IETF version code at the MOQTClient/MOQTServer
API boundary. Strict reject of unsupported drafts or hex form
with a clear error.

(Future refactor planned to keep draft as int throughout aiomoqt
internals, deferred from 0.9.3 — see memory note.)

### Verification matrix

All 4 corners green against local moqx (mvfst :4433 +
picoquic :4434) and public moqx-main.ci.openmoq.org:

| | raw QUIC | WebTransport |
|---|---|---|
| **d14** | ✅ pub-sub roundtrip, 0% loss | ✅ pub-sub roundtrip, 0% loss |
| **d16** | ✅ pub-sub roundtrip, 0% loss | ✅ pub-sub roundtrip, 0% loss |

Catalog-wide relay_probe (probed per-relay to avoid handshake
rate-limit artifacts): moqx-main, moxygen-fb, quicr-west,
red5-moq, moqtail, imquic, cloudflare-d14, cf-d16-interop all
respond on at least one supported transport/draft combo.

### Regression lockdown

New `aiomoqt/tests/test_interop_invariants.py` — 16 unit tests
(no network) locking down the specific wire-format invariants
where the 0.9.x bugs manifested:

- URL path leading-slash preservation
- `moqt_version_from_draft` validates input form
- ALPN derivation by draft number
- `MOQTClient` rejects unsupported drafts and hex wire form
- Supported-version set pinned

Test suite: 175 → 191 (16 new lockdowns).

### Compatibility

- `MOQTClient(draft_version=N)` now strictly accepts the draft
  number (e.g. `14`, `16`). Internal callers that previously
  passed the full IETF code (e.g. `MOQT_VERSION_DRAFT16 =
  0xff000010`) need to switch to the draft number. In-tree
  callers (tests, examples, microbench) already updated.
- No wire-format change. Existing `MOQT_VERSION_DRAFT14/16`
  constants still hold the IETF code values for internal use.

## v0.9.2 (2026-05-11)

Pairs with [aiopquic 0.3.0](https://pypi.org/project/aiopquic/0.3.0/);
dependency floor bumped from `aiopquic>=0.2.5` to `aiopquic>=0.3.0`.

The bulk of the v0.9.2 win lives in aiopquic 0.3.0 — receiver-side
durability + WT backpressure that converts data corruption under
slow-consumer conditions into honest latency growth. See the
aiopquic 0.3.0 CHANGELOG for the full architecture writeup.

aiomoqt-side changes in this release:

### Teardown-race fix (`_data_streams` tombstone set)

aiomoqt mp-loopback at end-of-bench produced 1-2 spurious
`stream-type parse failed: 0x1` rejections per run with the classic
missing-5-byte-SubgroupHeader signature. Diagnosed as a teardown
race: `_handle_subscribe_done` sent STOP_SENDING on streams still
actively receiving → publisher's reciprocal RESET_STREAM pop'd the
subscriber's `_data_streams[sid]` state → chunks already in flight
on the wire arrived after the pop → `_on_stream_data` created
fresh state with `parser=None` → parser read the next chunk's first
byte (an ObjectHeader's `0x01`, not a SubgroupHeader type byte)
and failed.

Fix: tombstone dict `_stream_torn_down` populated at every
state-pop site triggered by RESET / STOP_SENDING / UNSUBSCRIBE /
SubscribeDone / session close / natural task-done.
`_on_stream_data` checks this set first and silently drops chunks
for torn-down streams rather than recreating fresh state. Time-
based eviction (30 s window, swept at most once per second) bounds
memory in long-running sessions.

### `AIOMOQT_DESYNC_TRACE=1` debug aid

Env-gated TX / RX first-byte trace at `stream_write_drain` and
`_on_stream_data`. Resolved once at import time so the hot path is
a single Python bool test (effectively free when disabled). When
enabled, logs the first send / first chunk per stream-id with
head_hex so future similar desync investigations have an
out-of-the-box first-anchor.

### Honest sustained verification

60-second mp-loopback sustained runs against aiopquic 0.3.0 on
Ryzen WSL2 (`-P 2 -s 1024 -g 120 --mp-loopback`):

| Target | Delivered | avg latency | Loss |
|---|---|---|---|
|   83 Mbps |   83 Mbps |   5 ms | 0 / 0.6M objects |
|  249 Mbps |  249 Mbps |  32 ms | 0 / 1.8M objects |
|  490 Mbps |  493 Mbps | 567 ms | 0 / 3.6M objects |
|  980 Mbps |  534 Mbps (capped) | 3938 ms | 0 / 3.9M objects |

The 534 Mbps cap is aiomoqt's subscriber-side parse/dispatch CPU
saturation. At sub-saturation rates Phase B holds tight latency
floor; at-saturation Phase B trades latency for integrity (the
TCP trade-off). 0 lost objects across all rates — there are no
parse rejects at any sustained rate in this matrix.

Comparison to the pre-fix world (0.9.1 + aiopquic 0.2.7) at the
same harness and 15 s windows: 250M = 1-2 parse rejects per run,
500M = 2, 1000M = 4063, 1500M = 7130, 2000M = many.

### Smaller items

- `-g` / `--group-size` flag on `adaptive_bench` for tuning the
  publisher's group cadence.
- `MOQTStreamReject` raised on stream-type parse failures (the
  reject-not-abort path) lets a corrupt stream tear itself down
  without taking the whole session with it. Diagnostic-only in
  v0.9.2 (the underlying race is fixed); kept as defense-in-depth
  for whatever the next desync class turns out to be.
- README: dropped earlier suspect "5× drop" framing on
  loopback-vs-wire performance; linked the aiopquic perf breakdown
  for the honest numbers.

## v0.9.1 (2026-05-09)

`--mp-loopback` flag on `adaptive_bench` for multi-process
loopback testing (publisher + subscriber in separate processes,
real UDP loopback through kernel sockets). aiopquic floor bumped
to `>=0.2.5`. README cleanup.

## v0.9.0 (2026-05-06)

Transport migration to **aiopquic** (vendored picoquic-backed asyncio
QUIC) plus perf hardening. Pairs with [aiopquic 0.2.2](https://pypi.org/project/aiopquic/0.2.2/);
the dependency floor is `aiopquic>=0.2.2`.

### Release-blocker resolution

This release was held while a close-time segfault was investigated.
Root cause: double-close UAF in picoquic's `picoquic_close_ex`
wake-list path; fixed in aiopquic 0.2.2. aiomoqt 0.9.0 ships against
the fixed wheel — 0/100 segfaults on full pytest under release-
equivalent build (`-O3 -g`), down from 15/100 on the released 0.2.0
wheel before the fix.

### What changed beyond PR #12

- Dependency floor bumped from `aiopquic>=0.2.0` to `aiopquic>=0.2.2`
  (transitively gets the segfault fix, picoquic submodule bump
  including #2095 worker perf, WebTransport empty-path normalization,
  AB8/AB9 stress benches).
- `path` defaults updated across tests/examples: `"moq"` → `""`/`"/"`
  per the project convention. aiopquic 0.2.2 normalizes empty WT path
  to `/` so consumers don't need to think about it.
- `b7_framer.py` microbench added — TX framer hot-path measurement
  (proven NOT the bottleneck: 395K obj/s sync at 8KB single-stream).
- Adaptive bench polish: `--no-uvloop` flag, `fmt_bps` Mbps→Gbps
  cutover at 1 Gbps (was 500 Mbps), high-water = `rx delivered` (not
  commanded target), uvloop default, txQ ring-depth column,
  parallel `SubsActuator` shutdown, `mp.Queue.cancel_join_thread()`
  to avoid atexit hangs after worker terminate.

### Highlights

- **Switched transport** from qh3 to aiopquic. Major perf gain comes
  from aiopquic's pull-model TX + per-stream RX byte ring + canonical
  MAX_STREAM_DATA backpressure (no more silent drops under high load).
- **Framer-desync-under-load fixed.** The intermittent `framer desync:
  ext_len=...` parse failure that fired at sustained ≥0.5 Gbps to
  moxygen was a transport-level byte-conservation hazard in the qh3
  era; it does not reproduce on aiopquic 0.2.0. Verified by 240s `-P 4
  -t 240` adaptive_bench runs to ≥1.1 Gbps high-water with 0% loss
  across 47 samples.
- **MoQT dataclasses use `slots=True`** — measurable per-object
  hot-path savings on the parser side.
- **StreamChain rewritten in Cython** (now sourced from aiopquic);
  replaces the deque-of-memoryview chain accumulator on the receive
  path.

### API changes

- **Dependency:** `aiopquic>=0.2.0` (was `qh3`).
- **Endpoint vs path rename**: server-side `endpoint=` arg becomes
  `path=` to match the asyncio + aiopquic naming convention.
- **WebTransport bidi routing** (Phase 2): inbound bidi streams use
  `WebTransportStreamDataReceived` with a real WT stream id rather
  than the old strip-WT-header workaround. Cleaner, no longer
  duplicates control-stream logic.

### Adaptive bench (`aiomoqt.examples.adaptive_bench`)

- `-t / --duration SECONDS` hard cap.
- `--keylogfile PATH` plumbed through MP pub+sub workers (PATH.pub /
  PATH.sub for Wireshark TLS decryption).
- Jitter column + auto-discovered default trackname.
- Subscribers' `rx_mbps` aggregated correctly across MP workers
  (delta-snapshot bug fix).
- Sequence pub → sub spawn so the relay sees PUBLISH before SUBSCRIBE
  on `--mode subs`.
- Rate-split for `-P > 1`: each subgroup gets `target_mbps / P`
  (was emitting at 4× the commanded rate at -P 4 due to a forgotten
  divisor in the MP pub worker).
- Default priority 128 + duplicate-generator fix.
- `--debug` now streams subprocess logs to parent stderr.

### Hot-path strip

Removed all `AIOMOQT_DESYNC_*` env-gated forensic counters / rolling
CRCs / per-chunk hex tracing from the data-stream RX loop. They
existed during the framer-desync hunt; now that the bug is fixed in
the transport layer they're noise. Net `-170` lines from the RX hot
path. Parse-exception forensic dump (chain hex + last_obj_id +
bytes_total) preserved — only fires on actual failure.

Per-object / per-chunk `logger.debug` calls also stripped: even with
log-level filtering the f-string evaluation cost was measurable at
70K+ obj/sec.

### Other

- Control parser: replace `assert` with logged truncation handler so a
  malformed peer doesn't terminate the process.
- Logger no longer propagates aiomoqt logs to root (caller
  application's logger config wins).
- `.vscode/` untracked — per-developer IDE settings.
- `track.publish(forward=)` experimental flag for optimistic publish
  (NOT spec-supported — diagnostic only).

### Numbers (paired with aiopquic 0.2.0)

- adaptive_bench `-P 1 -t 240`: 1.1 Gbps high-water, 0% loss over 47
  samples through moxygen (mvfst).
- adaptive_bench `-P 4 -t 160`: 758 Mbps high-water, 0% loss over 31
  samples (previously crashed inside 10s with framer desync at
  -P 4).
- 179/179 aiomoqt tests pass.

### Removed / not-yet

- `qh3` transport path: removed.
- WebTransport server / multi-session H3 — still single-session-per-
  connection; multi-session deferred.
- moq-interop-runner Docker image: deferred.
