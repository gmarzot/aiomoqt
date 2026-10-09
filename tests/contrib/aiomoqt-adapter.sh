#!/usr/bin/env bash
# Driver adapter that lets moq-contribution-interop-runner start and score the
# aiomoqt publisher (aiomoqt.tools.pub_bench: a synthetic subgroup track).
# Contract: adapters/contract.schema.json (version 1).
#
# The options below describe pub_bench's command line, not MOQT expectations.
# Expected behavior comes only from the draft, via the runner's catalogs.
set -euo pipefail

fail() {
    printf 'aiomoqt adapter: %s\n' "$1" >&2
    exit 64
}

[[ "${MOQ_INTEROP_DRIVER_CONTRACT_VERSION:-}" == 1 ]] ||
    fail 'unsupported driver contract version'
request_file=${MOQ_INTEROP_DRIVER_REQUEST_FILE:-}
[[ -n "$request_file" && -r "$request_file" ]] || fail 'request file is unavailable'
python=${AIOMOQT_PYTHON:-}
[[ -n "$python" && -x "$python" ]] ||
    fail 'AIOMOQT_PYTHON must name a python with aiomoqt installed'

jq -e '
    .schema_version == 1 and
    (.draft == 18 or .draft == 21) and
    (.transport == "native_quic" or .transport == "webtransport") and
    (.scenario_id | type == "string" and length > 0) and
    (.endpoint | type == "string" and length > 0 and (contains("\n") | not)) and
    (.scenario_timeout_ms | type == "number" and . == floor and . >= 1 and . <= 3600000) and
    (.namespace_hex | type == "array" and length >= 1 and all(type == "string" and length > 0)) and
    (.track_name_hex | type == "string" and length > 0)
' "$request_file" >/dev/null || fail 'unsupported or malformed request'

draft=$(jq -r '.draft' "$request_file")
# aiomoqt speaks drafts 14, 16 and 18; refuse rather than guess.
[[ "$draft" == 18 ]] || fail "draft $draft is not supported by aiomoqt"
transport=$(jq -r '.transport' "$request_file")
endpoint=$(jq -r '.endpoint' "$request_file")
timeout_ms=$(jq -r '.scenario_timeout_ms' "$request_file")
scenario_id=$(jq -r '.scenario_id' "$request_file")
if [[ "$transport" == webtransport ]]; then
    [[ "$endpoint" == https://* ]] || fail 'WebTransport requires an https endpoint'
else
    [[ "$endpoint" == moqt://* ]] || fail 'native QUIC requires a moqt endpoint'
fi

hex_to_text() {
    local hex=$1 text
    [[ "$hex" =~ ^([0-9a-f]{2})+$ ]] || fail 'malformed hex in request'
    text=$(printf '%b' "$(sed 's/../\\x&/g' <<<"$hex")")
    [[ "$text" =~ ^[[:print:]]+$ ]] || fail 'non-printable name in request'
    printf '%s' "$text"
}
namespace=
while IFS= read -r field_hex; do
    field=$(hex_to_text "$field_hex")
    [[ "$field" != */* ]] || fail 'namespace field contains a slash'
    namespace+="${namespace:+/}$field"
done < <(jq -r '.namespace_hex[]' "$request_file")
track=$(hex_to_text "$(jq -r '.track_name_hex' "$request_file")")

# --pub-ns announces the namespace and serves the runner's SUBSCRIBE. The
# scenarios below observe a publisher-originated PUBLISH, so the publisher is
# also told to send one, or only that. This only selects messages to emit.
flow=(--pub-ns)
case "$scenario_id" in
    publish-track-under-single-period-namespace|\
    application-publish-track-in-session-namespace)
        flow=(--pub-both) ;;
    initiate-track-publication|\
    publish-track-namespace-fields|\
    receive-reason-phrase-length-over-1024|\
    receive-publish-request-ok-with-track-properties|\
    receive-subscribe-before-outstanding-publish-response|\
    publish-with-multiple-message-parameter-types|\
    publish-with-multiple-configured-parameters|\
    publish-existing-track-after-observed-object-publication|\
    receive-request-update-ok-with-track-properties)
        flow=() ;;
esac

# Track contents the scenario's fixture contract asks for: Group 7 with
# Object 9 already published, a track with nothing published, objects sent
# as datagrams, or two subgroup streams open at once.
content=()
case "$scenario_id" in
    fetch-known-first-object-with-nonzero-group-and-object-ids|\
    fetch-multiple-published-groups-in-each-explicit-order|\
    retrieve-same-object-at-distinct-times|\
    retrieve-same-object-with-different-subscribe-publish-ok-and-fetch-parameters|\
    subscribe-to-track-after-observed-object-publication|\
    publish-existing-track-after-observed-object-publication|\
    accepted-subscription-update-after-observed-object-publication|\
    accepted-track-status-after-observed-object-publication|\
    publish-and-retrieve-same-object-and-track-immutable-properties|\
    repeat-immutable-property-with-alternative-varint-encodings-available|\
    publish-object-with-immutable-properties|\
    receive-fetch-start-beyond-largest-published-object|\
    joining-fetch-after-forward-enabled-and-track-advanced|\
    cancel-fetch-request-with-open-data-stream|\
    reject-request-update-for-open-fetch|\
    publish-objects-before-within-and-after-subscription-range|\
    publish-with-multiple-message-parameter-types|\
    publish-with-multiple-configured-parameters)
        content=(--prefill 10) ;;
    receive-joining-fetch-for-track-with-no-published-objects|\
    receive-standalone-fetch-for-track-with-no-published-objects)
        content=(--no-objects) ;;
    fetch-object-previously-observed-as-datagram)
        content=(-D) ;;
    cancel-subscribe-with-multiple-open-subgroups)
        content=(-P 2) ;;
esac

# Control messages the publisher originates for the scenarios that wait
# for them.
control=()
case "$scenario_id" in
    observe-publisher-client-goaway|\
    send-new-request-after-publisher-control-goaway|\
    publisher-control-goaway-with-pending-request-at-cutoff)
        control=(--goaway-after 1) ;;
    publisher-queries-track-status-before-resuming-publication|\
    publisher-recovery-track-status-*)
        control=(--track-status) ;;
    publisher-ends-subscription-with-no-data-streams|\
    finish-subscription-with-open-object-streams)
        control=(--end-after 1) ;;
    receive-request-update-ok-with-track-properties)
        control=(--publish-update) ;;
    publish-and-withdraw-namespace-during-discovery)
        control=(--withdraw-after 1) ;;
    publish-two-simultaneous-tracks)
        control=(--second-track) ;;
    publish-distinct-content-tracks-in-same-scope)
        control=(--second-track --forward 1) ;;
esac

# A 64-byte token cache holds the runner's 20-byte alias entries and stays
# below its 80-byte oversize-registration probe; the overflow probe needs
# one under 17 bytes, so none is advertised.
token_cache=64
[[ "$scenario_id" != register-request-token-exceeding-advertised-cache-size ]] ||
    token_cache=0

timeout_seconds=$(((timeout_ms + 999) / 1000))
# The runner's certificate is self-signed for the test; pub_bench has no
# trust-file option, so verification is skipped (-k). The runner answers only
# a PUBLISH_NAMESPACE without parameters, so it carries no token. The refused
# tokens are the credentials sweep.py gives the runner: invalid
# (MALFORMED_AUTH_TOKEN), expired (EXPIRED_AUTH_TOKEN) and denied
# (UNAUTHORIZED).
exec "$python" -m aiomoqt.tools.pub_bench "$endpoint" \
    -N "$namespace" -T "$track" --draft 18 -k "${flow[@]}" --auth-token '' \
    -s 64 -g 10 -r 50 -t "$timeout_seconds" --no-stats "${content[@]}" \
    "${control[@]}" \
    --token-cache "$token_cache" \
    --token-reject 1:696e76616c6964:0x4 \
    --token-reject 1:65787069726564:0x5 \
    --token-reject 0:64656e696564:0x1
