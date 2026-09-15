#!/usr/bin/env bash
# Drive moxygen's moqtest_client through a set of draft-afrind-moq-test
# cases against a running relay, and report per-case pass/fail.
#
# Deliberately a subset of moxygen's own 56-case conformance_test.sh: the
# cases here are the ones a relay can satisfy without a group cache or
# joining FETCH, which our relay does not have yet. Widen it as the relay
# grows, and prefer adding a case over loosening one.
set -u

CLIENT=${CLIENT:-./harness/bin/moqtest_client}
URL=${URL:-https://127.0.0.1:4443/}
# 'quic' = raw QUIC, 'h3wt' = HTTP/3 + WebTransport. Matches how the
# relay under test was started.
TRANSPORT=${TRANSPORT:-quic}
# Confine negotiation to one draft so a failure names the draft it came
# from; empty would let the client offer everything.
DRAFT=${DRAFT:-18}
PASS=0
FAIL=0
FAILED_CASES=()

run_case() {
  local name="$1"; shift
  if timeout 60 "$CLIENT" --url="$URL" --transport="$TRANSPORT" \
       --versions="$DRAFT" "$@" >"/tmp/case-$$.log" 2>&1; then
    echo "  PASS  $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $name"
    sed -n '$p' "/tmp/case-$$.log" | sed 's/^/        /'
    FAIL=$((FAIL + 1))
    FAILED_CASES+=("$name")
  fi
}

echo "moq-test conformance — draft=$DRAFT url=$URL transport=$TRANSPORT"
echo

echo "Section 1 — forwarding preferences"
for fp in 0 1 2 3; do
  run_case "subscribe fp=$fp" --request=subscribe \
    --forwarding_preference="$fp" --last_group=2 --objects_per_group=5
done

echo
echo "Section 2 — object and group counts"
run_case "single object per group" --request=subscribe \
  --forwarding_preference=0 --last_group=2 --objects_per_group=1
run_case "many objects per group" --request=subscribe \
  --forwarding_preference=0 --last_group=1 --objects_per_group=20
run_case "custom start group" --request=subscribe \
  --forwarding_preference=0 --start_group=5 --last_group=7 \
  --objects_per_group=3
# One object per group over several groups: a relay whose forward loop dies
# at the first subgroup end drops every later group.
run_case "many single-object groups" --request=subscribe \
  --forwarding_preference=0 --last_group=5 --objects_per_group=1

echo
echo "Section 3 — object sizes"
run_case "tiny objects" --request=subscribe --forwarding_preference=0 \
  --last_group=1 --objects_per_group=5 --size_of_object_zero=10 \
  --size_of_object_greater_than_zero=10
run_case "large object zero" --request=subscribe --forwarding_preference=0 \
  --last_group=1 --objects_per_group=3 --size_of_object_zero=10240

if [ "${SKIP_PUBLISH:-0}" != "1" ]; then
  echo
  echo "Section 8 — publish mode (SUBSCRIBE_TRACKS discovery)"
  for fp in 0 1 2 3; do
    run_case "publish fp=$fp" --request=publish \
      --forwarding_preference="$fp" --last_group=2 --objects_per_group=5
  done
fi

echo
echo "Section 6 — extensions"
run_case "integer extension" --request=subscribe --forwarding_preference=0 \
  --last_group=1 --objects_per_group=5 --test_integer_extension=1
run_case "variable extension" --request=subscribe --forwarding_preference=0 \
  --last_group=1 --objects_per_group=5 --test_variable_extension=1
run_case "both extensions" --request=subscribe --forwarding_preference=0 \
  --last_group=1 --objects_per_group=5 --test_integer_extension=1 \
  --test_variable_extension=2

# Standalone FETCH: served by the origin SUT only (the relay has no cache).
# fp=3 is not a fetch case (the client refuses datagram preference for FETCH).
if [ "${FETCH:-0}" = "1" ]; then
  echo
  echo "Section 4 — standalone fetch"
  for fp in 0 1 2; do
    run_case "fetch fp=$fp" --request=fetch --forwarding_preference="$fp" \
      --last_group=2 --objects_per_group=5
    run_case "fetch fp=$fp markers" --request=fetch \
      --forwarding_preference="$fp" --last_group=2 --objects_per_group=5 \
      --send_end_of_group_markers
  done
  run_case "fetch partial range" --request=fetch --forwarding_preference=0 \
    --start_group=1 --start_object=2 --last_group=2 --objects_per_group=5
  run_case "fetch single object" --request=fetch --forwarding_preference=0 \
    --last_group=0 --objects_per_group=1
fi

rm -f "/tmp/case-$$.log"
echo
echo "----------------------------------------------------------------"
echo "  passed: $PASS   failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '  failed cases: %s\n' "${FAILED_CASES[*]}"
  exit 1
fi
