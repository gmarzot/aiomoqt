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
# also told to send one. This only selects messages to emit.
flow=--pub-ns
case "$scenario_id" in
    publish-track-under-single-period-namespace|\
    application-publish-track-in-session-namespace|\
    publish-distinct-content-tracks-in-same-scope|\
    publish-two-simultaneous-tracks)
        flow=--pub-both ;;
esac

timeout_seconds=$(((timeout_ms + 999) / 1000))
# The runner's certificate is self-signed for the test; pub_bench has no
# trust-file option, so verification is skipped (-k).
exec "$python" -m aiomoqt.tools.pub_bench "$endpoint" \
    -N "$namespace" -T "$track" --draft 18 -k "$flow" \
    -s 64 -g 10 -r 50 -t "$timeout_seconds" --no-stats
