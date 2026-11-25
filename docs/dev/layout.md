# Layout

What lives where in the `aiomoqt` repository.

| Path | What |
|---|---|
| `aiomoqt/protocol.py` | session state machine, control + data plane (~300 KB) |
| `aiomoqt/client.py`, `server.py` | `MOQTClient`; `MOQTServer.serve()` / `serve_dual()` |
| `aiomoqt/track.py` | `Track`, `PublishedTrack`, `SubscribedTrack`, `VideoTrack` |
| `aiomoqt/delivery.py` | `StreamMapping`, `ObjectHistory`, `SubgroupDelivery`, `SendLane`, `FanoutDelivery` |
| `aiomoqt/fanout.py` | `FanoutPublisher`: one MSF broadcast on several relays |
| `aiomoqt/moq_fabric/` | moq-fabric mesh (moqx, LAPS) as a leaf node: wire codec, track hashes, `FabricNode`, d18 inner framing (`inner.py`), SNS per session (`ib.py`); `moq-fabric-probe` (`tools/moq_fabric_probe.py`) observes and originates; `moq-fabric-bench` (`tools/moq_fabric_bench.py`) sends and measures a track through the fabric |
| `aiomoqt/messages/` | sans-I/O encode/decode; `messages/d18/`, `messages/d20/` for draft-specific shapes (d22 shares d20's) |
| `aiomoqt/agent/` | declarative, JSON-round-tripping specs and `AgentSession` / `Reader` / `Writer` |
| `aiomoqt/cat.py` | CAT credentials in the format moqx verifies: mint, verify, grants |
| `aiomoqt/types.py`, `context.py` | draft numbers, error codes, `DraftProfile`, `profile_for()` |
| `aiomoqt/media/` | MSF catalog, `broadcast.py` composer, LOC, CMAF, LOCMAF packaging, MPEG-TS packets as MSFTS (`msfts.py`, PCR playout and TS analysis in `tsplayout.py`); ingest (PyAV, DASH, MPEG-TS, TS over UDP) and synthetic sources; `abr.py` ABR ladder on cymedia |
| `aiomoqt/tools/` | CLI tools: pub/sub/loopback/adaptive benches, `pub_server`, `load_sim`, `pub_media` / `sub_media`, `abr_ingest`, `relay_probe`, `moq_interop_client`, `moq_interop_relay` (aiomoqt-relay), `moqtest_origin`, `relay_status`; `scenarios/` for `load_sim` |
| `aiomoqt/utils/` | CLI grid (`cli.py`), stats, URL parsing, logging, bench workers |
| `aiomoqt/examples/` | minimal pub, sub, join and server examples; ship in the wheel |
| `aiomoqt/versions.py` | `aiomoqt-versions`: aiomoqt, aiopquic, picoquic and picotls build info |
| `aiomoqt/tests/` | the pytest tree, **inside** the package dir, left out of the wheel |
| `tests/` | `release_regression_test.py` (tier runner) + `relays.json`; `contrib/` = contribution-runner sweep; `moqdev-smoke/` = moq-dev's relay smoke in Docker. No pytest files |
| `deploy/aiomoqt-relay/` | compose, nginx and runbook for the public aiomoqt-relay |
| `docs/` | bench and demo runbooks; CAT auth flows (`cat-auth.md`) |
