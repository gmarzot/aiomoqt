#!/bin/bash
# Drive the four remaining Phase 1e reps sequentially on one load host:
#   C2 rep-01, C2 rep-02 (late join, TTFF), C1 rep-02 (churn N=2), C0 rep-01 (control)
# Order is by priority: cut from the tail if the runtime cap approaches.
set -u
RS=/home/gmarzot/Projects/marzresearch/perf-sweep/aiomoqt/perf/release-sweep
MOQX=/home/gmarzot/Projects/marzresearch/moqx/build/moqx
CFG=$RS/moqx-perf.yaml
PY=/home/gmarzot/Projects/marzresearch/perf-sweep/arms/base-070rc2/venv/bin/python
URL=moqt://10.14.25.173:9768
BLOCK=${1:?run block dir}
cd "$RS" || exit 1

portfree() { ! ss -ltn 2>/dev/null | grep -q ':9768 '; }

run_load() {  # <point> <rep> <flow>
  local P=$1 REP=$2 FLOW=$3
  local D=$BLOCK/$P/$REP
  mkdir -p "$D"
  if ! portfree; then echo "SKIP $P/$REP: :9768 busy"; return 3; fi
  "$MOQX" serve --config "$CFG" > "$D/relay.log" 2>&1 &
  local RP=$!
  sleep 2
  date +%s.%N > "$D/t0_reference"
  "$PY" -P -u -m aiomoqt.tools.load_sim "$URL" -f "$RS/scenarios/$FLOW" \
        --draft 16 -k -i 5 --seed 1 > "$D/load.log" 2>&1
  local rc=$?
  kill -9 "$RP" 2>/dev/null; wait "$RP" 2>/dev/null
  echo "== $P/$REP rc=$rc $(grep -cE '^\s*T\+' "$D/load.log") interval rows"
}

echo "### driver start $(date -u +%H:%M:%SZ)"
echo "### C2 rep-01"; bash "$RS/scripts/c2_latejoin.sh" "$BLOCK" rep-01 30 140
echo "### C2 rep-02"; bash "$RS/scripts/c2_latejoin.sh" "$BLOCK" rep-02 30 140
echo "### C1 rep-02"; run_load C1 rep-02 c1-churn-50.json
echo "### C0 rep-01"; run_load C0 rep-01 c0-steady-50.json
echo "### driver done $(date -u +%H:%M:%SZ)"
ss -ltn 2>/dev/null | grep -E ':9768 |:9668 ' || echo "no relay ports listening"
