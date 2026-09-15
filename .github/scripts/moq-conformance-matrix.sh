#!/usr/bin/env bash
# Local dual-SUT conformance matrix: {relay, origin} x {18, 16} x {quic, h3wt},
# driving moxygen's moqtest_client through moq-conformance.sh. Starts
# moqtest_server and each SUT itself (children are killed on exit), one SUT
# process per role serving both drafts and both transports on one port.
#
#   bash .github/scripts/moq-conformance-matrix.sh            # everything
#   SUTS=origin DRAFTS=18 bash .github/scripts/moq-conformance-matrix.sh
set -u
HARNESS=${HARNESS:-$HOME/Projects/moq/openmoq/moqx/.scratch/moxygen-install/bin}
CLIENT=${CLIENT:-$HARNESS/moqtest_client}
SERVER=${SERVER:-$HARNESS/moqtest_server}
PORT=${PORT:-4443}
UP_PORT=${UP_PORT:-9999}
CERT=${CERT:-certs/cert.pem}
KEY=${KEY:-certs/key.pem}
SUTS=${SUTS:-"relay origin"}
DRAFTS=${DRAFTS:-"18 16"}
TRANSPORTS=${TRANSPORTS:-"quic h3wt"}
OUT=${OUT:-/tmp/moq-conformance-$(date +%Y%m%d-%H%M%S)}
SCRIPT=$(dirname "$0")/moq-conformance.sh
mkdir -p "$OUT"

SUT_PID=""
UP_PID=""
cleanup() {
  [ -n "$SUT_PID" ] && kill "$SUT_PID" 2>/dev/null
  [ -n "$UP_PID" ] && kill "$UP_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

wait_port() {
  for _ in $(seq 1 50); do
    ss -uln 2>/dev/null | grep -q ":$1 " && return 0
    sleep 0.2
  done
  return 1
}

"$SERVER" --port "$UP_PORT" --quic --cert "$CERT" --key "$KEY" \
  > "$OUT/moqtest_server.log" 2>&1 &
UP_PID=$!
wait_port "$UP_PORT" || { echo "moqtest_server did not bind :$UP_PORT"; exit 1; }

FAILS=0
for sut in $SUTS; do
  case "$sut" in
    relay)
      python -m aiomoqt.tools.moq_interop_relay --port "$PORT" --dual \
        --draft 16,18 --cert "$CERT" --key "$KEY" \
        --upstream "moqt://127.0.0.1:$UP_PORT/" > "$OUT/$sut.log" 2>&1 &
      ;;
    origin)
      python -m aiomoqt.tools.moqtest_origin --port "$PORT" --dual \
        --draft 16,18 --cert "$CERT" --key "$KEY" > "$OUT/$sut.log" 2>&1 &
      ;;
    *) echo "unknown SUT $sut"; exit 2 ;;
  esac
  SUT_PID=$!
  if ! wait_port "$PORT"; then
    echo "$sut did not bind :$PORT (see $OUT/$sut.log)"
    FAILS=$((FAILS + 1))
    kill "$SUT_PID" 2>/dev/null; wait "$SUT_PID" 2>/dev/null; SUT_PID=""
    continue
  fi
  if [ "$sut" = relay ]; then
    for _ in $(seq 1 50); do
      grep -q "upstream connected" "$OUT/relay.log" && break
      sleep 0.2
    done
  fi
  for d in $DRAFTS; do
    for t in $TRANSPORTS; do
      log="$OUT/$sut-d$d-$t.log"
      if [ "$sut" = origin ]; then
        SKIP_PUBLISH=1 FETCH=1 CLIENT="$CLIENT" DRAFT="$d" TRANSPORT="$t" \
          URL="https://127.0.0.1:$PORT/" bash "$SCRIPT" > "$log" 2>&1
      else
        CLIENT="$CLIENT" DRAFT="$d" TRANSPORT="$t" \
          URL="https://127.0.0.1:$PORT/" bash "$SCRIPT" > "$log" 2>&1
      fi
      rc=$?
      summary=$(grep -E "passed:" "$log" | tail -1)
      mark=""; [ "$rc" -ne 0 ] && { mark="  <-- FAIL"; FAILS=$((FAILS + 1)); }
      printf "%-7s d%s %-5s %s%s\n" "$sut" "$d" "$t" "$summary" "$mark"
    done
  done
  kill "$SUT_PID" 2>/dev/null; wait "$SUT_PID" 2>/dev/null; SUT_PID=""
  sleep 0.5
done
echo "logs: $OUT"
exit "$FAILS"
