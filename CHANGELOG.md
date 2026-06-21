# Changelog

## v0.10.0 (unreleased)

**draft-18 support (beta).** aiomoqt now speaks draft-18 alongside d14/d16,
over both raw QUIC and WebTransport, built on a version-dispatch refactor
(per-session draft, `CONTROL_REGISTRY` + `DraftProfile` spine). Pairs with
aiopquic 0.3.8 (dep floor unchanged). Internal-API breaking; no public
class-surface change.

### draft-18 support (beta) — summary

The sections below trace how it was built phase by phase; this is the
final state of the release.

- **Offered by default**, newest-first with graceful fallback:
  `supported_drafts` defaults to `(18, 16, 14)`. No env gate — the
  development-time `AIOMOQT_ENABLE_D18` flag is removed.
- **All four corners live**: d14/d16/d18 × raw QUIC / WebTransport,
  validated by in-process loopback object round-trips; loopback perf
  parity d16↔d18 (2.85–3.09 Gbps, 0% loss).
- **Wire**: out-of-band negotiation (ALPN / WT-Protocol); control over a
  pair of uni streams (symmetric `SETUP` `0x2F00`); per-request bidi
  streams with replies that drop the in-band Request ID; vi64 varints
  across control bodies and the data plane; `SUBSCRIBE_NAMESPACE`
  renumbered `0x50` plus `SUBSCRIBE_TRACKS`/`PUBLISH_BLOCKED`.
- **Beta limitations**: subscribe/publish subscription-filter and
  largest-location still use the d16 nested form; d18 Fetch is pending;
  Track-Properties extensions encode as RFC9000 varints (correct for the
  small values in use). None of these affect d14/d16.

### Per-session draft dispatch (kills the process global)

- The process-global `context.moqt_version` and its
  `get/set_moqt_ctx_version` accessors are **deleted**. Draft flows as a
  required keyword-only argument through every `serialize` / `deserialize`,
  sourced from a per-session `self._draft` (threaded as the resolved
  `prof: DraftProfile` as of Tier B, below). Concurrent
  sessions on different drafts in one event loop no longer clobber each
  other (regression: `test_concurrent_versions`).
- **Version = int draft number throughout the high/mid layers.**
  `draft_version` and the new `supported_drafts` are plain draft numbers
  (14, 16); `self._draft`, the `draft=` kwarg, and the dispatch tables
  are int-keyed. The IETF hex code and ALPN strings are materialized
  only at the wire boundary (d14 CLIENT_SETUP `versions[]`, ALPN build)
  — they no longer bleed into higher layers. Subsumes the long-pending
  int-form refactor. Draft numbers are named by the `MOQTDraft` IntEnum
  (`DRAFT_14`/`DRAFT_16`), used as the dispatch-table keys and
  `supported_drafts` defaults instead of bare literals.
- `send_control_message(msg)` / `send_stream_message(stream_id, msg)`
  now take the message object and serialize internally at `self._draft`,
  so no send site threads `draft=` — the class of "forgot the draft
  kwarg at a send site" is structurally impossible. These are
  below-the-public-API, undocumented session methods (the high-level
  `subscribe`/`publish`/`fetch`/… wrap them); only a custom handler that
  hand-built a frame and called `send_control_message(buf)` directly is
  affected — now pass the message, not a pre-serialized Buffer.

### Two table-driven dispatch mechanisms

- `CONTROL_REGISTRY[draft]` — one complete control-message table per
  draft, built from deltas (d16 = d14 base + repurposed
  0x02/0x05/0x07/0x08/0x0E, minus the d14-only points d16 dropped).
  Replaces the `is_draft16_or_later()` branch + `MOQT_D16_OVERRIDE_REGISTRY`
  overlay. Built once at class definition, read-only at runtime (no
  shared mutable cross-session state). Dispatch is a single keyed lookup;
  an unknown code point for the draft raises `MOQTProtocolViolation`.
- `DraftProfile` / `PROFILES[draft]` — named per-draft capability table
  (`setup_carries_versions`, `params_delta_coded`). Recurring spec-delta
  behaviors read intent (`profile_for(draft).params_delta_coded`) instead
  of version arithmetic; localized one-off gates keep the thin
  `is_draft16_or_later(draft)` predicate (now required-arg).

### Two-attribute negotiation discipline

- `MOQTClient` / `MOQTServer` accept `supported_drafts: list[int]`
  alongside `draft_version`. A pinned `draft_version` normalizes to a
  1-tuple; the default offers `(16, 14)` newest-first (preference order).

### Full multi-version negotiation, both transports

Requires the aiopquic negotiation APIs (aiopquic 0.3.8). Completes the
fast-follow that was deferred above — both transports now negotiate the
draft in-band, on the client **and** the server, with no draft pin:

- **Raw QUIC.** A multi-draft client offers every supported ALPN; the
  server selects the highest mutual (aiopquic's `alpn_select_fn`); both
  ends lock `self._draft` from the negotiated ALPN (`ProtocolNegotiated`).
  A client offering several drafts now settles on a **lower** server draft
  (e.g. d14) too — the former picky-d14 limitation is gone. No common
  draft fails the connect promptly (clean `ConnectionError`) instead of
  hanging.
- **WebTransport.** The server advertises its drafts as
  `WT-Available-Protocols`; `MOQTServer` passes them via
  `serve_webtransport(wt_supported_protocols=...)`. Both client and
  server derive `self._draft` from the negotiated `WT-Protocol`
  (`negotiated_protocol`) instead of defaulting to `max(supported_drafts)`.
  No subprotocol negotiated → highest-supported fallback (session still
  opens; WT-Protocol is optional).
- `test_multi_version_handshake.py`: the three formerly-skipped matrix
  cells (multi-offer→lower draft, no-common-ALPN→clean fail, WT in-band
  read-back) are live and green.

### draft-18 dispatch tier (Phase 2 scaffold)

The structural slot for draft-18, slotting into the Phase 0.C spine — no
`is_draft18_or_later()` predicate, just one more row/key:

- `MOQTDraft.DRAFT_18 = 18` + `MOQT_VERSION_DRAFT18` (`0xff000012`);
  `moqt_version_from_draft` now validates the speakable set against
  `MOQTDraft` (so draft 18 is accepted) rather than the legacy d14 in-band
  list `MOQT_VERSIONS` (unchanged — d16+ negotiate via ALPN/WT-Protocol).
- `PROFILES[18]` (out-of-band negotiation, delta-coded params) and a
  distinct `CONTROL_REGISTRY[18]` (per-draft dict; no shared state with
  d14/d16). The registry starts from d16 and is amended with the d18 wire
  deltas as the d18 message classes land (`aiomoqt/messages/d18/`).
- **Gated during development** (removed at GA, below): `AIOMOQT_ENABLE_D18=1`
  was required to select draft 18 (`require_d18_enabled`) while the d18 wire
  was incomplete. The gate is gone in this release; d18 is offered by default.

### draft-18 data plane (Phase 4)

The d18 object wire, gated behind `AIOMOQT_ENABLE_D18`. d14/d16 paths are
byte-for-byte unchanged — the codec is selected per stream/datagram from
`DraftProfile.varint`, and the perf-critical fused object path uses the
vi64 twins that already shipped in aiopquic (Phase 1).

- **vi64 across the data plane**: the data-stream type, `SubgroupHeader`
  fields (Track Alias / Group ID / Subgroup ID), and the per-object fields
  switch to vi64 for d18. `SubgroupHeader` / `ObjectHeader` carry the draft
  and route the fused `parse/encode_object_subgroup_vi64` (hot path) or the
  vi64 field-by-field decode (Buffer slow path).
- **SUBGROUP_HEADER classification** generalized from hardcoded ranges to
  the bit-mask form `0b0XX1XXXX` (bit 4 set, bit 7 clear, SUBGROUP_ID_MODE
  != 0b11), covering the d18 ranges `0x50-0x5D` / `0x70-0x7D`, plus the new
  `FIRST_OBJECT` bit (0x40).
- **OBJECT_DATAGRAM relayout** (§11.3.1): form `0b00X0XXXX`, with
  `PROPERTIES 0x01 / END_OF_GROUP 0x02 / ZERO_OBJECT_ID 0x04 /
  DEFAULT_PRIORITY 0x08 / STATUS 0x20`. Status merges into the one
  `ObjectDatagram` family; STATUS+END_OF_GROUP and out-of-form types are
  rejected with PROTOCOL_VIOLATION.
- **PADDING** stream `0x132B3E28` / datagram `0x132B3E29` recognized and
  discarded.
- Codec round-trips covered by `test_d18_data_plane.py`. End-to-end object
  loopback + the `loopback_bench` d18≈d16 perf checkpoint are validated
  during interop/perf testing (Phase 7).

### draft-18 session establishment + control core (Phase 3)

The d18 control plane (raw QUIC first; WebTransport added later — see the
4-corner note below). d14/d16 paths are byte-for-byte unchanged — every
divergence reads a `DraftProfile` capability flag.

- **Control over a unidirectional stream pair** (§3.3) instead of a single
  bidirectional stream. Each peer opens one control write-uni beginning
  with `SETUP` and reads the peer's control read-uni; the read-uni is
  bound by peeking the leading vi64 for the `0x2F00` stream/message type
  and demuxed from data uni streams. `_control_write_stream_id` /
  `_control_read_stream_id` resolve to the single bidi id pre-d18 and to
  the distinct uni ids for d18 (`control_uni_pair` profile flag), so one
  branch point carries the whole divergence.
- **Symmetric `SETUP` (type `0x2F00`)** — `0x2F00` is both the control
  uni-stream type and the `SETUP` message type, written once at the stream
  head. Body is count-less delta-coded Key-Value-Pair Setup Options
  (§10.3) to the message Length: no version array (negotiated via ALPN),
  no parameter count, no `MAX_REQUEST_ID` option. The control Message Type
  is read/written as **vi64** for d18 (`AF 00`); the per-draft
  `CONTROL_REGISTRY` is the type authority (renumbered code points fall
  outside the contiguous enum). Length stays 16-bit.
- **Per-request bidirectional streams** (§3.3): each request (`SUBSCRIBE`,
  …) opens its own bidi stream; the response returns on that same stream.
  Responses **drop the Request ID** (`reply_has_request_id` flag) — they
  are demuxed positionally by stream, and the bound id is injected onto the
  parsed reply so handlers key on `msg.request_id` unchanged. Generalizes
  the d16 `SUBSCRIBE_NAMESPACE` bidi machinery to the request openers.
- Raw-QUIC d18 loopback: handshake over the uni pair +
  `SUBSCRIBE → SUBSCRIBE_OK → PUBLISH_DONE` round trip, replies carrying no
  Request ID.
- **Deferred to a later phase**: control-stream priority (needs new
  aiopquic transport surface) and the GOAWAY Timeout / new-Request-ID
  fields. (WebTransport d18 control landed — see below.)

### Spec-compliant bounded parsing

- `_deserialize_params` enforces the control-frame `Length` extent: a
  declared count or length that would read past it raises
  `MOQTProtocolViolation` (session close `PROTOCOL_VIOLATION`, spec §9)
  instead of a raw `BufferReadError`; the dispatcher catches it and
  closes cleanly.

### High-level API is version-agnostic

- The track abstraction (`aiomoqt/track.py`) holds no version knowledge:
  it reacts to logical message types (`isinstance(msg, RequestUpdate)`)
  and passes logical fields, letting the codec gate the wire by draft.
  Users do not encode version-specific handling.

### Hot path preserved

- The extensions/properties codec is draft-agnostic (removed the
  per-object global lookup); `ObjectHeader` and the per-object loop carry
  no draft argument. loopback_bench parity held across d14/d16 ×
  raw-QUIC/WT (~2.5–2.8 Gbps).

### Other

- Removed the unmaintained `red5_conformance` example and its test docs
  (Red5 Pro interop is untested/unverified; noted in README).

### draft-18 control message bodies (Phase 5)

- vi64 control bodies for the request / namespace / subscribe / publish
  families via a buffer-carried varint flavor: aiopquic `Buffer` /
  `StreamChain` take a `vi64` flag and `push_vint` / `pull_vint` dispatch
  the codec in C; the control dispatcher tags the buffer once per message
  from `DraftProfile.vi64`, so message bodies don't each re-resolve it.
- Even-typed parameter values that are a fixed `uint8` in d18 (FORWARD
  `0x10`, SUBSCRIBER_PRIORITY `0x20`, GROUP_ORDER `0x22`) encode as a byte,
  not a varint (`DraftProfile.uint8_params`). AUTH_TOKEN Token Type uses an
  `AuthTokenType` enum (no magic `0`).
- **Code-point renumbers**: `SUBSCRIBE_NAMESPACE` `0x11` → `0x50`; new
  `SUBSCRIBE_TRACKS` (`0x51`) and `PUBLISH_BLOCKED` (`0x0F`).
  `REQUEST_UPDATE` carries both the new and the Existing Request ID in d18
  (confirmed against the mvfst/moxygen relay).

### draft-18 GA

- The `AIOMOQT_ENABLE_D18` gate (`require_d18_enabled`) is **removed**;
  draft 18 is offered on equal footing with d14/d16. `MOQTClient` /
  `MOQTServer` default `supported_drafts` to `(18, 16, 14)` — newest-first,
  graceful fallback. Selecting d18 no longer needs an env var.

### draft-18 over WebTransport (the 4th corner)

- d18 control runs over WebTransport as well as raw QUIC. The receive demux
  and `stream_write` were already transport-agnostic; only bring-up was
  raw-QUIC-specific — it now opens the control write-uni via
  `open_uni_stream()` and per-request bidi streams via the async
  `create_stream()` path. All four corners (d14/d16/d18 × QUIC/WT) are
  exercised by a parametrized loopback object round-trip.

### Session owns version-awareness (Tier A + Tier B)

- **Tier A**: the session resolves its effective `DraftProfile` once at
  draft-lock. A `_draft` property-setter keeps `self._profile` in sync, so
  per-message code reads `self._profile` instead of calling
  `profile_for(self._draft)` each time.
- **Tier B**: `serialize` / `deserialize` take `prof: DraftProfile` instead
  of `draft: int`, across both control and data planes (the data-plane
  `SubgroupHeader` stores `prof`; `FetchObject` / `ObjectDatagram` take it).
  The codec receives the already-resolved profile and the message layer no
  longer calls `profile_for` — net fewer lines, fewer per-message lookups,
  and one abstraction to extend for future drafts. `is_draft16_or_later`
  stays for the monotonic d14↔d16 layout fork (now reads `prof.draft`);
  d14 remains fully supported.
- **Data-plane factory**: `session.subgroup_header(...)` — the data-plane
  mirror of `send_control_message` — bakes in `self._profile`, so the
  `Track` high-level API and apps never thread draft/profile. The dead
  `Track(draft=...)` constructor parameter is removed; version flows from
  the session (`MOQTClient(draft_version=)` negotiation → `self._draft`).

### GoAway

- `GoAway` gains a `MAX_URI_LENGTH` (8 KiB) bound; an over-long New Session
  URI raises with a trimmed "exceeds maximum length (8 KiB)" message on
  both encode and decode.

### Breaking (internal)

- `serialize(*, prof: DraftProfile)` / `deserialize(*, prof, buf_end)`
  require the keyword (was `draft: int`); the session threads its resolved
  `self._profile`. The data-plane `SubgroupHeader(draft=)`,
  `ObjectDatagram.serialize(draft=)`, and the `Track(draft=)` ctor parameter
  are removed (carry/resolve `prof` from the session instead).
  `context.moqt_version` + `get/set_moqt_ctx_version` removed;
  `is_draft16_or_later` / `get_major_version` require an argument;
  `draft_version` is a draft number (was the IETF hex code); session
  `_moqt_version` renamed `_draft`.

## v0.9.8 (unreleased)

Pairs with aiopquic 0.3.8; dep floor `aiopquic>=0.3.7` → `aiopquic>=0.3.8`
(needs 0.3.8's real-negotiated-ALPN reporting).

### Multi-version negotiation on auto-draft (raw QUIC)

- When no `--draft` / `draft_version` is given, the raw-QUIC client now offers **every supported version's ALPN**, newest first (`["moqt-16", "moq-00"]`), instead of only `moq-00` (draft-14). A draft-16-only peer negotiates `moqt-16`; a draft-14 peer falls back to `moq-00`; the session sets its version from whichever ALPN the peer **actually** selected (via aiopquic 0.3.8's real-negotiated-ALPN reporting — offering multiple ALPNs is only correct once the negotiated one is known, not assumed to be the first offered). Previously auto offered d14-only, so draft-16-only relays closed the connection (QUIC code 376 = `no_application_protocol`) before SERVER_SETUP — the dominant failure against d16-only peers in the public moq-interop-runner. Explicit `--draft` is unchanged. (WebTransport auto already resolves to the latest draft as of 0.9.7; d14-only WT peers still require explicit `--draft 14`.)
- Loopback fetch/join self-tests now pin an explicit draft (single ALPN per side) rather than relying on the accidental d14 auto-default; auto/multi-version negotiation is exercised against real relays. Server-side multi-ALPN acceptance (so an aiopquic server can take an auto client's multi-version offer) is deferred to the relay-column work.
- Known limitation: a draft-14-only server that selects the client's *first* offered ALPN and rejects (rather than choosing the common one) — e.g. some moq-rs draft-14 deployments — will reject the multi-version offer with `no_application_protocol`. Pass an explicit `--draft 14` for those peers. The clean fix is per-draft client registration (one pinned ALPN per target, the way moq-rs registers separate draft entries); auto offers all versions for the common case where the peer negotiates correctly.

## v0.9.7

Pairs with aiopquic 0.3.7; dep floor `aiopquic>=0.3.6` → `aiopquic>=0.3.7`.

### Saturation / stream-churn under load

- Two-budget producer model: per-stream `tx_max_inflight_bytes` (1 MiB) over aiopquic's aggregate `tx_max_queued_bytes` gate (4 MiB). Bounded RSS and latency under high group/stream churn on both transports.

### Teardown livelock fix

- Writes to a dead session/connection raise `CancelledError` instead of spinning, so producer tasks unwind promptly at shutdown (pairs with the aiopquic closed-connection suspend fix).

### Publisher lifecycle + liveness

- PUBLISH_DONE lifecycle guard: a track that has sent SubscribeDone no longer generates objects (ends "publish after done"); d16 request-id fallback when the subscribe request-id is absent.
- `keep_alive_interval` and `socket_buffer_size` passthrough to the transport.

### CLI uniformity (example tools)

- `--max-queued-bytes` / `--max-inflight-bytes` across the bench tools.
- Transport flag standardized to `-q` / `--quic` / `--use-quic`.
- `-h` = host, `-?` = help (mysql/psql convention).

### Interop

- Interop client per-case timeout hardening (absorbs WAN / cold-relay latency; ships in the GHCR image the public moq-interop-runner uses).
- Regression harness: per-relay `compat` forwarding; catalog cleanup (quicr-west → d16-only + `libquicr` compat; moqtail disabled pending a known WT path).

### Bench harness (adaptive_bench)

- Batched, self-healing subscriber workers: one process hosts K = `--step-subs` subscriptions and reopens individual drops internally — drives thousands of subscribers without one OS process per sub.
- Subs-mode publisher isolated into its own process (decoupled from the controller event loop).
- Three-timescale knobs (`--join-rate` / `--stagger` / `--interval`); loop-lag probe (`AIOMOQT_LOOP_LAG=1`); uvloop opt-in.

### Diagnostics / docs

- Diagnostic audit: removed the `AIOMOQT_MON` backpressure monitor and `DESYNC_TRACE`.
- Benchmark/performance content extracted to PERFORMANCE.md.

## v0.9.6

Pairs with [aiopquic 0.3.6](https://pypi.org/project/aiopquic/0.3.6/);
dep floor `aiopquic>=0.3.5` → `aiopquic>=0.3.6` (Lens B primitives,
per-stream typed pressure helpers, picoquic patches upstreamed,
ring-rename catch-up).

### Lens B stream_write_drain refactor + paced yield throttle

- `MOQTSession.stream_write_drain` shrunk from ~150 lines to ~55 by delegating wire-level mechanics to aiopquic's `send_stream_data_drained`. The per-stream sc->tx ring + edge-trigger drain event is in aiopquic where the rings live; aiomoqt only owns the application-layer **policy** (byte budget, latency target, cancellation). This is the backpressure-placement principle landed: aiopquic provides primitives, aiomoqt expresses intent.
- Paced-rate fall-through fix: when `track.rate` drops below the asyncio sleep precision floor (~50-200 µs on Linux/WSL2), the loop now yields `sleep(0)` 1-in-N (`_PACED_YIELD_EVERY = 32`) instead of skipping yields entirely. Skipping was starving `-P 8 -r 70K` runs and triggering BBR cwin collapse from spurious RTT spikes.
- `-r` is now an **AGGREGATE** rate across all subgroup streams (not per-stream). Per-stream emit cadence is `rate / num_subgroups`. CLI semantics now match the natural reading: `-r 80000 -P 8` means 80K obj/s total at 10K/s per stream.

### tx_max_inflight_bytes safety ceiling (16 MB default)

- `MOQTSession.DEFAULT_TX_MAX_INFLIGHT_BYTES = 16_000_000` ships as a producer-side byte-budget cap. Hysteresis: producer parks at `stream_tx_buf_used > HIGH`, resumes at `< HIGH // 2`. Without this, sustained max-rate sends could overrun aiopquic's tx ring and cascade memory growth. The 16 MB ceiling lets the typical user approach the API and get high performance with the least likelihood of crashing; advanced users can override via the constructor or `--max-inflight-bytes N` on `loopback_bench`.
- The byte-budget gate uses the typed primitive `stream_tx_buf_used(sid)` (introduced in aiopquic 0.3.6 Lens B), not a heuristic. Was a no-op in 0.3.5 because it queried the wrong primitive.

### Bounded sub_bench memory

- `sub_bench` previously held every per-object latency sample in an unbounded list, causing 5–18 GB RSS on long runs. Now keeps running stats (count, sum, sum-of-squares, min, max) + a reservoir-sampled subset (default 10K entries) for percentile estimation. RSS bounded regardless of duration.

### loopback_bench tracemalloc + post-shutdown counter dump

- `AIOMOQT_TRACEMALLOC=1` (off by default) starts tracemalloc with a baseline snapshot at +2 s after the subscriber connects and an end snapshot before cleanup. The diff localizes what GREW during steady-state operation — isolates Python-side allocation from aiopquic C-side rings.
- `AIOMOQT_TASK_DUMP=1` (off by default) also enables a final post-shutdown aiopquic counter dump after `quic_server.close()` + 500 ms drain delay. Standing diagnostic capability for future memory / lifecycle investigations.

### Catch up to aiopquic ring rename

- `tx_ring` / `rx_ring` references updated to `tx_event_ring` / `rx_event_ring` (matches aiopquic 0.3.6's nomenclature pass). `get_tx_drain_event` becomes `tx_event_ring_drain_event`.

### Utility

- `wait_cond_timeout(coro, timeout)` helper added; `track.wait_closed()` drops its `timeout` parameter (callers compose with `wait_cond_timeout` for clearer call-site semantics).

### CI hardening

- bash watchdog around `sub_bench` so the log is captured before SIGTERM hits the process.
- `pub_server -u` so the "ready on" banner reaches the log before grep races against it.
- Polling for `pub_server` ready instead of a fixed `sleep 1.5`.
- Interval table assertion replaces "Objects" summary check (interval table always emitted, summary block can be cut off by close-time signal).

### Known issues (deferred)

- Saturation-stress edge cases on the producer-side stream lifecycle (see aiopquic 0.3.6 Known Issues — pub-stream orphan at saturation, shutdown drain miss, `-P 8 -g 200` double-free). Sub-saturation workloads are clean; these manifest only at sustained max-rate.

## v0.9.5 (unreleased)

Pairs with [aiopquic 0.3.5](https://pypi.org/project/aiopquic/0.3.5/);
dep floor `aiopquic>=0.3.2` → `aiopquic>=0.3.5`.

### WT receiver bloat (RELEASE BLOCKER) — root cause + fix

`_WTSessionMixin._on_event` in `protocol.py` previously called `super()._on_event(ev_tuple)` for every WT event, then re-dispatched stream/datagram events via `quic_event_received` for the MoQT data path. The `super()` call enqueued `_EVT_WT_STREAM_DATA` / `_EVT_WT_STREAM_FIN` / `_EVT_WT_DATAGRAM` events into per-stream `asyncio.Queue` instances inside `WebTransportSession._stream_inbox` — but aiomoqt never consumes those queues (it routes via the QuicEvent translation path instead). Every queued event held a reference to the `memoryview` payload, which in turn pinned the underlying `aiopquic` `StreamChunk` alive. At 2.5 Gbps the leak grew ~60K chunks/sec → 5–18 GB RSS within 30–60 s.

Fix: skip `super()._on_event(ev_tuple)` for the event types we translate locally. Session-level events (ready / closed / draining / new_stream / tx_drained) still go through the base handler for their queue / Future signaling. After the fix: `chunks_alive_total` stays in single digits, RSS bounded, throughput unchanged (~2.3 Gbps), p99 latency drops from 384 ms (cliff) to 39 ms. Verified at 60 s sustained loopback with SIGUSR2 counter dumps.

### Per-session data-stream chain introspection

- `_MOQTSessionMixin._dump_data_streams(file=stderr)` walks `_data_streams` and reports per-stream `StreamChain` capacity + parse progress (`bytes_total`, `group_id`, `object_id`, parser type). Returns the dict for programmatic use.
- `aiomoqt.utils.taskdump`'s SIGUSR2 handler extended to walk live `MOQTSession` instances and emit the chain dump after the aiopquic counter dump. Tells you in one signal whether bytes are pinned at the chain layer (parser hot path) or downstream (was the diagnostic that localized the bloat above).

### WT-server d16 draft pin

`MOQTSessionWTServer.__init__` now calls `set_moqt_ctx_version(session.draft_version)` symmetrically with the raw-QUIC ALPN handler and the WT-client init path. Without it, the global `moqt_version` stayed at `MOQT_VERSION_DRAFT14` on the server, so `ClientSetup.deserialize` expected the d14 wire format (version list first) while a d16 client sent the d16 format (no version list) — observable as `BufferReadError` on the first incoming `ClientSetup`. Unblocks 2-proc WT d16; validated at 2.2-2.3 Gbps p99 75ms.

### Observability

- `--cc-algo` flag exposed on `MOQTServer` / `MOQTClient` and 11 example tools (`loopback_bench`, `pub_server`, `sub_bench`, `pub_bench`, `multi_sub_bench`, `adaptive_bench`, `relay_bench`, `pub_example`, `sub_example`, `server_example`, `join_example`). Passes through to `QuicConfiguration.congestion_control_algorithm`. Default BBR.
- `python -m aiomoqt.versions` / `aiomoqt-versions` documented in README ("Reporting issues" section). Chains through `aiopquic.versions` for full stack: aiomoqt + aiopquic + picoquic + picotls.

### Tooling

- `[tool.ruff] line-length = 100` retained as the standard (aiopquic now matches).

### Pressure-based yield in `stream_write_drain`

`PublishedTrack._generate_subgroup` previously yielded with
`asyncio.sleep(0)` every 64 objects on the unbounded-rate (`-r 0`)
path. On fast Python publish loops that count-based heuristic can
starve the picoquic worker between yields, capping throughput well
below the transport ceiling.

`MOQTSession.stream_write_drain` now performs a soft pressure-based
yield: after a successful (non-`BufferError`) write, it checks
`tx_pressure(stream_id)` and `await asyncio.sleep(0)` if pressure
exceeds 0.5. Every aiomoqt sender benefits — including third-party
publishers that drive the API directly, not just `PublishedTrack`.

The count-based `else: ... asyncio.sleep(0)` block in
`_generate_subgroup` is removed; bounded-rate (`-r N>0`) callers pace
via `asyncio.sleep(1/rate)` as before.

## v0.9.4 (2026-05-16)

Pairs with [aiopquic 0.3.2](https://pypi.org/project/aiopquic/0.3.2/);
dep floor `aiopquic>=0.3.1` → `aiopquic>=0.3.2`.

### `_extensions_decode` buf_end plumbing (d16 Track Extensions)

`_extensions_decode(with_length=False)` fell back to `buf.capacity`
(allocated size, not data length) when no end was supplied — read
past payload into uninitialized heap. Surfaced as a macOS test-order
flake where prior-test bytes shaped like a valid KVP overwrote the
real value. d16 §9.13 has no sequence terminator on Track
Extensions; the bound must come from the outer frame length.

Fix: `_extensions_decode` requires `buf_end` when `with_length=False`;
raises on overrun with diagnostic logs. Every control-message
`deserialize` now takes `buf_end: Optional[int] = None` uniformly;
the dispatcher always passes it.

### Perf

Single drain coroutine, `next_object_bytes` push API, event-driven
TX backpressure — paired with aiopquic 0.3.2's GSO send_length_max,
Cython `parse_object_subgroup`/`drain_rx_callback`, and SPSC
TX-drain wakeup.

### Test hygiene

Four stale WT `pytest.skip("WT fetch path returns empty results")`
blocks removed — the underlying picoquic_close UAF is fixed in
aiopquic 0.3.2. **195 passed / 0 skipped** (was 191/4).

### Verification

Pytest 195/0/0; regression unit + integration green; interop
`moqx-main` and `moxygen-fb` both 6/6 ctrl + 3/3 pub-sub on all
4 corners (d14/d16 × WT/raw-QUIC); linux + macOS argo confirmed.

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
