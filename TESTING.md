# Pre-Release Test Plan

This document is the checklist run before tagging a release. CI runs
the same tiers automatically on every PR to main (`unit` +
`integration`); the `interop` and `bench` tiers are owner-dispatched
or run locally.

Bench tools auto-generate unique tracknames from test parameters to
avoid stale-cache collisions on relays that key by (namespace, trackname).

---

## Test Tiers

| Tier | Network | CI-gated | Purpose |
|------|---------|----------|---------|
| `unit` | none | yes (PR) | pure-Python correctness; messages/track/buffer |
| `integration` | localhost | yes (PR) | pub/sub + setup + join + fetch over in-process qh3 |
| `interop` | public | manual / weekly | live relays in `tests/relays.json` |
| `bench` | localhost or relay | manual | adaptive throughput ceiling measurement |

Tier ≠ suite: a tier is just a named group of suites. The runner
accepts either `--test-tier <tier>` (runs every suite in the tier)
or `--test-suite <suite>` (runs a single suite, ignoring tier).

---

## Test Suites

### `unit` tier
- **`buffer`** — `tests/test_rebuf.py`; stream reassembly (object-boundary alignment, partial-chunk handling, subgroup reconstruction). Standalone script, not pytest.
- **`message`** — `pytest aiomoqt/tests/test_messages.py`; round-trip serialization for every MoQT control message across d14 and d16.
- **`track`** — `pytest aiomoqt/tests/test_track.py`; `PublishedTrack` / `SubscribedTrack` unit tests.

### `integration` tier
- **`loopback-setup`** — d14/d16 session handshake and `AUTH_TOKEN` parameter round-trip, in-process qh3.
- **`loopback-pub-sub`** — `loopback_bench` at fixed rate; asserts throughput > 0 and zero loss.
- **`loopback-join`** — `JOINING_SUBSCRIBE` with `ABSOLUTE` and `RELATIVE` fetch types.
- **`loopback-fetch`** — standalone `FETCH` variants: joining-relative, explicit range, invalid-range rejection, unknown request-id rejection.

### `interop` tier (per active relay × transport × draft)
- **`relay-ctrl-msg`** — 6 control-plane conformance cases (setup, announce, publish-namespace-done, subscribe-error, announce-subscribe, subscribe-before-announce). Error codes are validated to spec-defined values — `INTERNAL_ERROR (0x0)` does not pass a conformance gate.
- **`relay-pub-sub`** — 3-subscriber multi-sub bench against the relay; asserts N/N subscribers receive the published objects.
- **`relay-join`** — `SUBSCRIBE + JOINING_FETCH` probe (most relays do not implement this yet; disabled by default in the catalog).
- **`relay-fetch`** — standalone `FETCH` probe (same).

### `bench` tier (manual dispatch only; not PR-gated)
- **`loopback-adaptive-bench`** — ramps rate in steps, stops on loss / p99 latency growth / throughput shortfall, reports the last stable rate.

---

## Automated runner

`tests/release-regression-test.py` drives every tier. All commands are
one-liners and chainable:

```bash
# CI path — runs on every PR to main
python tests/release-regression-test.py --test-tier unit --test-tier integration

# Single tier
python tests/release-regression-test.py --test-tier unit
python tests/release-regression-test.py --test-tier interop

# Individual suite (bypasses tier grouping)
python tests/release-regression-test.py --test-suite message
python tests/release-regression-test.py --test-suite loopback-pub-sub --test-suite relay-ctrl-msg

# Interop in parallel across relays
python tests/release-regression-test.py --test-tier interop --interop-parallel 4

# Interop scoped to one relay (works on disabled entries too)
python tests/release-regression-test.py --test-tier interop --only moqx-main

# Custom catalog
python tests/release-regression-test.py --catalog /path/to/my-relays.json

# Adaptive bench
python tests/release-regression-test.py --test-tier bench
```

Exit 0 iff every non-skipped test passed. `[skip]` entries (per-relay
`disabled_suites` and fully `disabled` relays) do not contribute to
pass/fail counts. Per-test logs land in a temp directory printed at
the start and end of the run.

When `$GITHUB_STEP_SUMMARY` is set (GitHub Actions), the runner also
emits a markdown summary table to the run-summary page.

---

## Relay catalog (adding and probing)

Entries live in `tests/relays.json`. Full schema:

```json
{
  "name": "short-id",
  "urls": {
    "raw-quic": "moqt://host:port",
    "h3-wt":    "https://host:port/endpoint"
  },
  "drafts": [14, 16],
  "pub_mode": "publish",
  "insecure": false,
  "disabled": false,
  "disabled_suites": ["relay-join", "relay-fetch"],
  "notes": "freeform"
}
```

| Field | Required | Meaning |
|-------|----------|---------|
| `name` | yes | id used in output and `--only` |
| `urls` | yes | map of `raw-quic` and/or `h3-wt` to full URL |
| `drafts` | yes | supported MoQT draft numbers |
| `pub_mode` | no (default `publish`) | `publish` sends only PUBLISH; `publish-ns` sends only PUBLISH_NAMESPACE; `publish-both` sends both (required by Cloudflare d14 moq-rs) |
| `insecure` | no (default `false`) | if `true`, pass `--tls-disable-verify` / `-k` to subprocess tools |
| `disabled` | no (default `false`) | if `true`, skip this relay entirely unless `--only` names it |
| `disabled_suites` | no | list of suite names to skip for this relay (e.g. `["relay-join", "relay-fetch"]`) |
| `notes` | no | freeform comment (not printed on skip lines) |

Adding a new relay — minimal recipe:

```bash
# 1. Add a stub entry
# 2. Probe it manually
python tests/release-regression-test.py --test-tier interop --only <your-name>
# 3. Based on results, tune disabled_suites or set disabled: true
```

---

## Manual runs (what the automated runner doesn't cover)

The runner covers `unit`, `integration`, `interop`, and `bench` tiers
in one invocation. The sections below are for ad-hoc investigation
against a local relay — useful during protocol work, not needed for a
release gate.

### Local relay pub/sub — d16 raw QUIC, 500 KB objects

Shell 1 (publisher):
```bash
python -m aiomoqt.examples.pub_bench moqt://moqx-local-000.marzresearch.net:4433 -s 500000 -t 120 -r 30 -g 30 -k --draft 16
```

Shell 2 (subscriber):
```bash
python -m aiomoqt.examples.sub_bench moqt://moqx-local-000.marzresearch.net:4433 -k --draft 16
```

Expected: ~116 Mbps, ~30 obj/s, zero loss, auto-discovered trackname.

### Local relay pub/sub — d16 raw QUIC, 1 KB × 4 streams

Shell 1:
```bash
python -m aiomoqt.examples.pub_bench moqt://moqx-local-000.marzresearch.net:4433 -s 1024 -t 120 -r 120 -g 60 -P 4 -k --draft 16
```

Shell 2:
```bash
python -m aiomoqt.examples.sub_bench moqt://moqx-local-000.marzresearch.net:4433 -k --draft 16
```

Expected: ~480 obj/s, ~4 Mbps, p50 latency ~1 ms.

### Local relay — d16 raw QUIC, max rate (congestion control)

Shell 1:
```bash
python -m aiomoqt.examples.pub_bench moqt://moqx-local-000.marzresearch.net:4433 -s 4096 -t 120 -g 1000 -P 4 -k --draft 16
```

Shell 2:
```bash
python -m aiomoqt.examples.sub_bench moqt://moqx-local-000.marzresearch.net:4433 -k --draft 16
```

Expected: sustained high throughput. Watch p99 latency and loss to
see where your local loop saturates.

---

## moq-interop-runner integration

aiomoqt ships a TAP v14-emitting client at
`aiomoqt.examples.moq_interop_client` (the same module used internally
by `relay-ctrl-msg`). The Dockerfile at the repo root builds an image
consumable by [englishm/moq-interop-runner](https://github.com/englishm/moq-interop-runner).
The published image is `ghcr.io/gmarzot/aiomoqt:<version>` and
`:latest`; our `implementations.json` entry is wired up in an upstream
PR. No local action is required to keep this path green — the release
workflow pushes a fresh image on every tag.

---

## Known Issues

- Occasional corrupt timestamp extension at very high object rates
  (reassembly buffer misalignment under stress). Filtered out in
  `BenchStats`; logged as a warning.
- moqx caches by `(namespace, trackname)`; reusing a trackname after a
  publisher size/rate change yields a payload mismatch on subsequent
  subscribers. Bench tools auto-generate unique tracknames per run to
  avoid this — if you hand-roll commands against moqx, use a fresh
  `--trackname` per run.
