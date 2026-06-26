# Changelog

## v0.10.4 (unreleased)

- **Cross-draft (d14/d16/d18) microbenchmarks.** New `b8_control_roundtrip`
  times encode+decode of SUBSCRIBE / SUBSCRIBE_OK / PUBLISH / PUBLISH_OK /
  REQUEST_UPDATE at all three drafts (each round-trip verified before timing),
  and `b7_framer --draft all` / `b1_parser --draft all` add cross-draft
  TX-framing and RX-parse comparisons. Each prints a relative-to-d16 summary.
  Measured: d16→d18 is parity (within ~2%) across control, framing, and parse —
  the cost step is d14→d16 (the KVP redesign). Also fixes a latent bug in
  `b7_framer` where `--draft` was ignored (the probes always built an RFC9000
  header); it now threads the per-draft profile so each draft encodes its own
  wire form.
- **Docs:** README "Developing against a locally built aiopquic" — host-tuned
  source build vs the portable PyPI wheel, the editable-first install order for a
  separate venv per repo, and how to verify the local build is actually in use.
- **`relay_probe`:** probes **draft-18** by default now (was 14/16 only) and
  adds **`--draft`** — either a single draft (`--draft 18`, to watch a relay's
  d18 rollout without the extra d14/d16 handshakes) or an ordered offer set in
  one session (`--draft 18,16`, order preserved) to see which draft the relay
  negotiates from a given preference list.

## v0.10.3 (unreleased)

- **draft-18 pub-sub object delivery fixed (REQUEST_UPDATE wire format).**
  draft-18 removed the `Existing Request ID` field from `REQUEST_UPDATE`
  (§10.9, Figure 12); draft-16 still carries it. Our `RequestUpdate` decoder
  read that field unconditionally, so when a subscriber arrived and the relay
  sent the *publisher* a `forward=1` `REQUEST_UPDATE`, the extra varint shifted
  the parse and a delta-coded parameter key byte was misread as a parameter
  length — a PROTOCOL_VIOLATION that closed the publisher session, which pruned
  the namespace and left every subscriber with "no such namespace" and 0
  objects. `existing_request_id` is now draft-gated (kept for d16, omitted for
  d18+ per the spec). This resolves the draft-18 pub-sub known limitation noted
  in v0.10.2. Verified live against a draft-18 moxygen relay across all four
  corners (d16/d18 × raw-QUIC / WebTransport): 3/3 subscribers, objects
  delivered. d14/d16 pub-sub is unchanged.

## v0.10.2 (unreleased)

- **draft-18 FETCH / joining-FETCH / FETCH_OK now use the request stream.**
  `fetch()`, the joining-fetch helper, and `fetch_ok()` were sending on the
  single control stream. At draft-18 every request opener must open its own
  bidirectional request stream (and its response returns on that stream), so
  current moxygen rejected a FETCH on the control stream with a
  PROTOCOL_VIOLATION right after SETUP. These paths now route through
  `_send_request` / `_send_reply` like SUBSCRIBE/PUBLISH. Verified live against
  a draft-18 moxygen relay: control 6/6 + `fetch` + `join` all pass. Pre-d18
  is unchanged (those routes fall back to the control stream). The d18
  parameter encoding (uint8 SUBSCRIBER_PRIORITY/GROUP_ORDER/FORWARD per
  §10.2.7/8/12) was already correct — the bug was purely the send path.
- **Known limitation (draft-18 pub-sub object delivery).** Control, FETCH, and
  JOIN interop at d18; object *delivery* does not yet — a publisher ends the
  publish at the `forward=1` trigger before streaming objects, so subscribers
  receive 0 objects. Tracked for 0.10.3. d14/d16 pub-sub is unaffected.

## v0.10.1 (unreleased)

- **Preference-ordered draft probe (interop client).** A single
  `moq_interop_client` invocation now probes its `supported_drafts` in
  preference order, pinning the first draft whose SETUP succeeds. One
  manifest entry therefore prefers d16 against d16-capable relays yet still
  reaches d18 against d18-only relays, with no d16→d18 regression. The
  no-`--draft` default is the preference list `[16, 14, 18]`.
- **draft-18 Fetch.** `FETCH` / `FETCH_OK` now speak the d18 wire: vi64
  varints across the body, and `FETCH_OK` (a response) drops the in-band
  Request ID per §10.1 — the same vi64 + request-id-gate shape as the other
  OK replies, not a structural rewrite (the d18 "Location"/"End Location"
  fields are the same varint pairs as d16's start/end and
  largest_group/object). Round-trip matrix tests added for both. This
  clears the d18 Fetch beta limitation noted under v0.10.0.
- **Endpoint-keyed compat (interop client).** `moq_interop_client` now
  auto-selects the opt-in tolerances a known relay needs, keyed by host, so
  the moq-interop-runner (which passes only `RELAY_URL`, no per-column
  `COMPAT`) gets the right behavior without per-column config. First entry:
  the moq-rs draft-16 Cloudflare endpoint auto-enables `lenient-extensions`
  (it emits a truncated trailing-extensions block on `SUBSCRIBE`/
  `SUBSCRIBE_OK`), closing the cf-d16 pub-sub routing path. moq-rs advertises
  no `IMPLEMENTATION` setup param, so recognition is by endpoint; explicit
  `--compat` still adds to whatever the endpoint contributes. Default-off for
  all other hosts (no global leniency).

## v0.10.0 (unreleased)

**draft-18 support (beta).** aiomoqt now speaks draft-18 alongside d14/d16,
over both raw QUIC and WebTransport, built on a version-dispatch refactor
(per-session draft, `CONTROL_REGISTRY` + `DraftProfile` spine). Requires
aiopquic 0.3.9 (the vi64 buffer codec — `push_vint`/`pull_vint`); dep floor
`aiopquic>=0.3.8` → `aiopquic>=0.3.9`. Internal-API breaking; no public
class-surface change.

### draft-18 support (beta) — summary

The sections below trace how it was built phase by phase; this is the
final state of the release.

- **d18 is opt-in (beta).** The no-args default offers the **stable** set
  `(16, 14)`, newest-first — an auto session never negotiates onto the beta
  d18 wire, and d14 stays in the offer for d14-only peers. Select d18
  explicitly: `supported_drafts=18` or `supported_drafts=[18, 16, 14]`.
  No env gate — the development-time `AIOMOQT_ENABLE_D18` flag is
  removed.
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
- **Tests**: a d14/d16/d18 control-message round-trip matrix
  (`TestDraft18ControlMessages`) covers the vi64 codec, the uint8 param-value
  path, and reply-drops-request-id; alongside the d18 data-plane, setup,
  dispatch, and loopback object round-trips.

### Version selection — `supported_drafts` + `negotiated_draft`

- `MOQTClient` / `MOQTServer` take `supported_drafts`: an **int** (a single
  draft — offer only that ALPN), an **ordered list** (offer the set in the
  caller's preference order — highest mutual wins, and first-offered matters
  for relays that select the first ALPN rather than the highest), or **None**
  (the auto offer: the stable set, newest-first). It is stored as a non-empty
  list of draft numbers on `self.supported_drafts` — caller order is
  preserved for explicit lists; only the `None` auto-offer is sorted
  newest-first. The legacy `draft_version` param and field are **gone** (a
  single-element list is the pin).
- The per-session result is `session.negotiated_draft` (a draft number),
  derived from the resolved `DraftProfile` so the two can never drift; QUIC
  ALPN / WT-Protocol negotiation sets it.
- CLI: `--draft` accepts a single draft (`--draft 16`) or a comma list
  (`--draft 18,16,14`), via `parse_draft_spec`.

### Per-session draft dispatch (kills the process global)

- The process-global `context.moqt_version` and its
  `get/set_moqt_ctx_version` accessors are **deleted**. Draft flows as a
  required keyword-only argument through every `serialize` / `deserialize`,
  sourced from the per-session `negotiated_draft` (threaded as the resolved
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

### Negotiation discipline — one `supported_drafts` list

- `MOQTClient` / `MOQTServer` accept a single `supported_drafts`
  (int | ordered list | None); a single-element list is the pin, and the
  default (`None`) offers the stable set `(16, 14)` newest-first. This
  consolidates an earlier two-attribute (`draft_version` + `supported_drafts`)
  iteration — the version fields were unified in the 0.9.13-pre
  reconciliation (see "Version selection" above; the per-session result is
  `negotiated_draft`).

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
  selecting d18 no longer needs an env var. d18 is **beta**, so it is
  **opt-in**, not auto-negotiated: `MOQTClient` / `MOQTServer` default
  `supported_drafts` to the stable `(16, 14)`; request d18 with
  `draft_version=18` (pin) or `supported_drafts=[18, 16, 14]` (offer). This
  keeps a no-args session on the stable wire and prevents a d18-capable peer
  from silently negotiating our beta wire on the public interop runner.

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
  `draft_version` is a draft number (was the IETF hex code) and now also
  accepts an ordered list (offer); the `supported_drafts` constructor param
  is removed (use `draft_version=[...]`); session `_moqt_version` renamed
  `_draft`.

## v0.9.12 (unreleased)

### SUBSCRIBE: omit default-valued params (conformant "conservative sender")

- `subscribe()` now defaults `priority` / `group_order` / `forward` to `None` = "unspecified", so the **d16 SUBSCRIBE omits them from the wire** and the relay applies the protocol default (forward=true, publisher group order, default priority). aiomoqt previously always emitted all four params (`SUBSCRIBER_PRIORITY` 0x20, `GROUP_ORDER` 0x22, `FORWARD` 0x10, `SUBSCRIPTION_FILTER` 0x21) — legal but non-idiomatic; moxygen/moqx/moq-rs all omit defaults. The extra params tripped **moqtail**'s stricter parser, which silently **dropped** our multi-param SUBSCRIBE — failing `subscribe-error`, `announce-subscribe`, and `subscribe-before-announce` against it. With the filter-only subscribe, **moqtail goes 3/6 → 6/6** and every other relay (moxygen / moqx / imquic / moq-dev-rs / loopback d14+d16) is unchanged. To force a param onto the wire, pass it explicitly — a default-equal value like `forward=1` is still sent when given. The d14 path (mandatory inline fields) substitutes defaults for `None`. The same omit-defaults applies to `fetch()` and `join()` (subscriber priority / group order, and join's subscribe `forward`).

### moq_interop_client: serve the forwarded SUBSCRIBE + tolerate pending-hold

- The `announce-subscribe` publisher now **serves the relay's forwarded SUBSCRIBE** (via `PublishedTrack`), matching moxygen/moqx/moq-rs, so forward-and-wait relays can complete the downstream `SUBSCRIBE_OK`. When a relay holds the subscription pending without an eager OK (a valid forward-and-wait model), the wait timeout is accepted as a pass and annotated (`# COMPAT`) rather than failing. A real `SUBSCRIBE_ERROR` still fails.

### moq_interop_client: tighten the error-code leniency (exclude INTERNAL_ERROR 0x0)

- The 0.9.11 "any structured error = rejection" default was too broad — it accepted `INTERNAL_ERROR (0x0)`, a server fault rather than a refusal. `subscribe-error` / `subscribe-before-announce` / `join` now reject `0x0` by default (accepted only under explicit `moq-rs`/`moq-dev` compat, which use `0` as their not-found code). Spec codes and other non-spec codes (e.g. 404) are unaffected.

### Release regression: flake resistance, macOS loopback-fetch, catalog trim

- **Interop tier retries transient flakes.** A failing relay/suite is retried up to 2 extra times; a genuine fail fails every attempt, a flake recovers and is annotated `(flaky: recovered on attempt N)`. External-relay/network jitter (timeouts, the `multi_sub_bench` fixed publisher-register wait racing a relay's namespace registration) no longer reads as a real failure.
- **macOS skips `loopback-fetch`** (new `--skip-suite` flag): on macOS the native QUIC connection close stalls ~10s when a fetch stream is left un-drained at session exit — confirmed on a real macOS box (argo) that it's the aiopquic/picoquic close drain over loopback, *not* pytest teardown as first assumed — tipping loopback-fetch over its timeout. The fetch logic is platform-independent (covered on Linux) and the bench matrix covers macOS loopback delivery; tracked as an aiopquic follow-up.
- **Catalog trim (#24):** disabled the no-answer / unsupported suites that will never pass — `cloudflare-d14` `relay-fetch` and `cf-d16-interop` `relay-join`/`relay-fetch`. Verified against the implementations rather than inferred from the timeout: both `cloudflare/moq-rs` and `itzmanish/moq-rs` READMEs list FETCH as "Not Soon", and the relay neither forwards nor errors a FETCH (open upstream issues cloudflare/moq-rs#57 passthrough, #58 error). Genuine conformance fails are kept (cf-d16 pub-sub routing, quicr-west fetch code=0).

## v0.9.11

### moq_interop_client: accept any structured error as a valid rejection

- `subscribe-error`, `subscribe-before-announce`, and `join` now accept **any** structured error response (SUBSCRIBE_ERROR / REQUEST_ERROR, any code) as a valid "relay refused the request" outcome **by default** — the interop assertion is that the relay *rejected*; the exact error code is now *noted*, not required. This lifts relays that reject correctly but with non-spec codes (e.g. moq-dev-rs's HTTP-style `404 "Broadcast not found"`) on the public runner, where no per-relay `--compat` flag is passed. A timeout / transport error still fails (a structured rejection ≠ silence), and spec-defined codes (TRACK_DOES_NOT_EXIST 0x04/0x10) still pass cleanly without annotation. The trailing-extensions check stays strict.

### interop catalog: cf-d16-interop compat=lenient-extensions

- Tolerate moq-rs draft-16's truncated trailing-extensions block on the `cf-d16-interop` regression target, recovering `relay-ctrl-msg` to 6/6 (COMPAT-annotated). `join`/`fetch` and `pub-sub` remain genuine fails (see issue #24). Affects our own interop regression only.

## v0.9.10

Pairs with aiopquic 0.3.8 (no aiopquic change). Bug-fix release.

### adaptive_bench: BW mode fixes (relay mode broken since 0.9.7)

- **BW relay mode delivered no data.** BW (bandwidth-ramp) mode against a relay (`-r URL`, no `--mode subs`) connected and published (`forward=0`) but **never started a subscriber**, so the relay never sent `SUBSCRIBE_UPDATE forward=1`, `rx` stayed 0, and the ramp aborted on dead-air. Root cause: the subs-mode isolated publisher (added in 0.9.7) reused the `pub_proc` variable and reset it to `None` *before* the BW start-gate, dropping BW relay runs into the subs-publisher branch — which starts a publisher but no subscriber (subscribers there come from `SubsActuator`, absent in BW mode). The subs publisher now uses distinct `subs_pub_proc` / `subs_pub_stop` names so the BW gate fires and starts both pub and sub. Also fixes the `--mp-loopback` `relay_url=None` crash (same fall-through). Subs mode and BW single-process loopback were unaffected.
- **`--mp-loopback` self-test version mismatch.** The mp-loopback self-test delivered no data and logged `_Dispatcher._drain` → `BufferReadError: read out of bounds`: its loopback-server created a `MOQTServer` with no draft (→ draft-14) while the WT subscriber auto-resolved to draft-16, so the server mis-parsed the d16 `CLIENT_SETUP` params. The loopback-server now honors a `draft`, and the self-test pins server + sub to a matching version (`--draft`, default draft-16). Verified across auto / d14 / d16.

### moq_interop_client: auto-draft fallback now covers WebTransport

- The auto-draft fallback added in 0.9.9 (probe the multi-version offer; on SETUP failure pin draft-14) was guarded to raw QUIC only, so a draft-14-only relay that stalls the d16 SETUP **over WebTransport** still failed every case. The fallback now covers **both transports**. Verified against the public moq-rs draft-14 endpoint: WT goes **0/6 → 6/6** (docker + remote), taking the moq-rs interop column from 6/18 to 18/18. draft-16 WT relays are unaffected — the probe succeeds and no fallback fires (it only adds one connect).

### moq_interop_relay: optional dual-transport (two-listener) mode

- New `--quic-port N` runs a second raw-QUIC listener alongside the default WebTransport listener in one process, sharing the global namespace table. One hosted instance can then back **both** a `remote-webtransport` (`--port`) and a `remote-quic` (`--quic-port`) endpoint in the interop runner — without same-port dual-ALPN (which would require aiopquic-level per-ALPN stack dispatch). The single-port docker registration is unchanged (one transport). The module docstring's prior claim of "both on the same port via two parallel listeners" was inaccurate and is corrected.

### Release regression: loopback draft × transport matrix

- The integration tier (which PR + main CI run) now includes `loopback_bench` smokes across **draft-14 / draft-16 × raw-QUIC / WebTransport** and an `adaptive_bench --mp-loopback` (multi-process BW pub+sub) smoke per draft. This guards our own tools against the regression classes that slipped through 0.9.7–0.9.9: session-setup / ALPN breaks (caught by the loopback_bench matrix) and the multi-process publisher/subscriber start path (caught by `--mp-loopback`, which the in-process loopback never exercised). No external relay required.

## v0.9.9 (unreleased)

Pairs with aiopquic 0.3.8 (no aiopquic change). Targeted follow-up to
v0.9.8 fixing bench-tool regressions; examples only, no core library
change.

### loopback_bench: explicit draft (raw-QUIC auto fix)

- `loopback_bench` gains `-D` / `--draft` (default **14**), applied to **both** the loopback publisher (server) and subscriber (client) so the raw-QUIC ALPN (`moq-00` for d14, `moqt-NN` for d16+) and the WT version match per side. Previously it passed no draft, so in raw-QUIC mode the auto client offered `["moqt-16", "moq-00"]` while the auto server offered only `moq-00`; aiopquic's server-side ALPN selection rejected the asymmetric offer and the connection closed with QUIC code 376 (`no_application_protocol`) before SERVER_SETUP — the publisher rejecting its own subscriber. This is the same "pin an explicit draft (single ALPN per side)" fix already applied to the loopback fetch/join self-tests in v0.9.8; `loopback_bench` was missed. Server-side multi-ALPN acceptance remains deferred to the version-negotiation refactor.

### adaptive_bench subs-mode controller

- **Shortfall settle gating:** a freshly-added group (one worker hosting K = `--step-subs` self-healing slots) is judged for shortfall only once **mature** (past its startup grace), so the controller no longer stalls the ramp the instant it adds a group whose K subs are still handshaking. A group that never delivers is still caught by the starve reaper.
- **Group-quantized ramp:** the subs-mode step is rounded to whole groups (floor = `--step-subs`, not 1), so ramps/probes move one whole self-healing group at a time — no 1-subscriber crawl, and no sticky sub-group holds after a back-off.
- **Setpoint quantized:** the targeted count snaps to whole groups (`workers × K`), so the reported Target equals the capacity actually hosted instead of reading below it.
- **Mature-only latency:** the p90/mean that drive the SLA back-off count only mature workers. A joining group's catch-up burst (old send-timestamps → seconds-scale p90, seen against backlog-replaying relays) no longer trips a false back-off; sustained congestion still registers once a worker matures.

### moq_interop_client: auto-draft ALPN fallback

- In auto mode (no `--draft`) on raw QUIC, the client probes the multi-version ALPN offer once and, if the handshake fails, pins **draft-14** (`moq-00`) for the run (surfaced as a `# note:` in the TAP output). A draft-14-only relay that picks the client's first offered ALPN (`moqt-16`) and refuses — rather than choosing the common `moq-00` — closes with QUIC 376 or stalls the handshake; the fallback recovers it. Verified against the public moq-rs draft-14 (Cloudflare) endpoint: auto goes **0/6 → 6/6**. Explicit `--draft` and WebTransport are unchanged. The 0.9.8 multi-version order was a global tradeoff (won d16-only relays, lost d14-strict moq-rs/xquic); this restores the d14 columns without giving up the d16 wins. General per-draft client negotiation remains the version-negotiation refactor's job.
- `--draft` also reads a `DRAFT` env var (matching the existing `RELAY_URL` / `TESTCASE` env interface); the ALPN probe's handshake timeout is 5 s (d16 relays clear it in ~0.3 s).

### Interop regression catalog (relays.json)

- `relay-join` / `relay-fetch` no longer skipped wholesale: the suites run on every reachable relay so results are honest — pass where supported, fail where not — instead of hidden behind `disabled_suites`. Verified passing on moqx / moxygen / imquic (d14+d16) and cloudflare-d14 (join); the rest report real fails/timeouts.

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
