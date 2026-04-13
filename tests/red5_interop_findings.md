# Red5 MoQT Interop Findings

Target: `red5-moq-test.marzresearch.net:4433`
Date: 2026-04-12
aiomoqt version: v0.7.0-rc (dev branch)

## d14 (ALPN: moq-00)

- CLIENT_SETUP/SERVER_SETUP: **works**
- SUBSCRIBE → SUBSCRIBE_OK: **works** (content_exists=1, largest=(0,1))
- Data delivery: **FAILS** — zero server-initiated uni streams after SUBSCRIBE_OK
- FETCH: **ignored** — Red5 sends no response (no FETCH_OK, no FETCH_ERROR)
- ABSOLUTE_START filter: SUBSCRIBE_OK with content_exists=0 (inconsistent with LATEST_OBJECT response)
- pcap confirmed: only stream 0 (bidi control) in entire capture

## d16 (ALPN: moqt-16)

- CLIENT_SETUP/SERVER_SETUP: **works** (version via ALPN)
- SUBSCRIBE_NAMESPACE → REQUEST_OK: **works**
- subscribe_options field: **ignored** — same behavior for 0 (PUBLISH), 1 (NAMESPACE), 2 (both)
- NAMESPACE messages (§9.21): **never sent** — subscriber cannot discover tracks
- PUBLISH messages (§9.13): **never sent** — subscriber cannot establish track aliases
- Data delivery: Red5 pushes catalog data immediately on uni stream with
  unestablished track_alias=1, before subscriber has subscribed to any track
- Catalog data parses correctly: SubgroupHeader type 0x31 (DEFAULT_PRIORITY +
  extensions), 190-byte JSON payload with streaming format metadata

## Non-conformances per draft-ietf-moq-transport-16

1. **§6.1 (line 1633-1634)**: "The recipient [of SUBSCRIBE_NAMESPACE] will send
   any relevant NAMESPACE, NAMESPACE_DONE or PUBLISH messages" — Red5 sends
   none of these

2. **§9.25 (line 4098-4099)**: Subscribe Options field "Allows subscribers to
   request PUBLISH (0x00), NAMESPACE (0x01), or both (0x02)" — Red5 ignores
   this field entirely

3. **§9.25 (line 4106-4113)**: "the publisher will send matching NAMESPACE
   messages on the response stream... Also, any matching PUBLISH messages
   without an Established Subscription will be sent on the control stream" —
   Red5 does neither

4. **§10.4.2 (line 4453)**: "If an endpoint receives a subgroup with an unknown
   Track Alias, it MAY abandon the stream" — Red5 sends data with
   track_alias=1 that was never established via PUBLISH, forcing the subscriber
   to handle an unknown alias race that shouldn't occur

5. **d14 data delivery**: SUBSCRIBE_OK with content_exists=1 but zero uni streams
   opened — publisher accepts subscription but never delivers data

## What works

- QUIC handshake with both moq-00 and moqt-16 ALPNs
- TLS with Let's Encrypt certificates
- CLIENT_SETUP/SERVER_SETUP in both drafts
- SUBSCRIBE_NAMESPACE/REQUEST_OK in d16
- SubgroupHeader type 0x31 wire format (catalog data parses correctly)
- LOC-formatted catalog JSON is well-formed and matches MSF spec
