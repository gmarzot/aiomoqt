#!/usr/bin/env bash
# Run moxygen's full moq-test conformance suite against a relay and save
# the report under a stable name. Unlike moq-conformance.sh (a curated,
# gating subset), this runs every case the suite has and reports a score,
# so the number moves as the relay grows.
set -uo pipefail

URL=${URL:-https://127.0.0.1:4443/}
DRAFT=${DRAFT:-18}
TRANSPORT=${TRANSPORT:-quic}          # quic | h3wt
SUITE=${SUITE:?path to conformance_test.sh}
MOXDIR=${MOXDIR:?directory holding moxygen/moqtest/moqtest_client}
OUT=${OUT:-reports}

mkdir -p "$OUT"
args=("$URL" "$DRAFT")
[ "$TRANSPORT" = "quic" ] && args+=(Q)

# The suite writes its report into the working directory, named by
# timestamp; run it somewhere disposable and rename the result.
work=$(mktemp -d)
echo "=== full suite: draft $DRAFT over $TRANSPORT ==="
# Per-case lines as they land: the suite takes minutes, and a silent
# step is indistinguishable from a hung one.
(cd "$work" && MOXYGEN_DIR="$MOXDIR" SKIP_FETCH="${SKIP_FETCH:-0}" \
    bash "$SUITE" "${args[@]}") 2>&1 \
    | tee "$OUT/$DRAFT-$TRANSPORT-full.log" \
    | stdbuf -oL grep -E --line-buffered \
        "Test [0-9]+\]|PASSED|FAILED|SECTION [0-9]|Success Rate|Total Tests"
rc=${PIPESTATUS[0]}
report=$(ls -t "$work"/moqtest_conformance_report_*.txt 2>/dev/null | head -1)
if [ -z "$report" ]; then
    echo "::warning::no conformance report produced for $DRAFT/$TRANSPORT"
    exit 0
fi
mv "$report" "$OUT/$DRAFT-$TRANSPORT.txt"
echo "report: $OUT/$DRAFT-$TRANSPORT.txt (suite exit $rc)"
