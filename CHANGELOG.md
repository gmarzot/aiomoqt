# Changelog

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
