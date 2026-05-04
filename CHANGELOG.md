# Changelog

## v0.9.0 (2026-05-04)

Transport migration to **aiopquic** (vendored picoquic-backed asyncio
QUIC) plus perf hardening. Pairs with [aiopquic 0.2.0](https://pypi.org/project/aiopquic/0.2.0/);
the dependency floor is `aiopquic>=0.2.0`.

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
