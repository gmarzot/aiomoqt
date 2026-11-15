#!/bin/bash
# C2 late-join point: 1080p publisher + warm subscriber (t=+2s) + measured late
# subscriber (t=+30s). pub_bench in the 0.7.0 arm returns from the shell as soon
# as PUBLISH is sent when no subscriber is present (verified with -P 1 and -P 4,
# Flow B and Flow A), so a warm subscriber is required to hold the publishing
# state. The measured joiner is still late by the configured delay.
#
# usage: c2_latejoin.sh <run_block_dir> <rep_name> [late_delay_s] [run_s]
set -u
RS=/home/gmarzot/Projects/marzresearch/perf-sweep/aiomoqt/perf/release-sweep
MOQX=/home/gmarzot/Projects/marzresearch/moqx/build/moqx
CFG=$RS/moqx-perf.yaml
PY=/home/gmarzot/Projects/marzresearch/perf-sweep/arms/base-070rc2/venv/bin/python
URL=moqt://10.14.25.173:9768
BLOCK=${1:?run block dir}
REP=${2:?rep name}
LATE=${3:-30}
RUN_S=${4:-140}
D=$BLOCK/C2/$REP
mkdir -p "$D"
cd "$RS" || exit 1

# never pkill broadly: the shared relay on :9668 is not ours. Only our port.
# Wait for :9768 rather than sampling once: a previous rep's relay can still be
# shutting down, and moqx aborts (core dump) on a failed bind.
for _ in $(seq 1 40); do
  ss -ltn 2>/dev/null | grep -q ':9768 ' || break
  sleep 0.5
done
if ss -ltn 2>/dev/null | grep -q ':9768 '; then
  echo "FATAL: :9768 still in use after 20s; refusing to touch it" >&2
  exit 3
fi

"$MOQX" serve --config "$CFG" > "$D/relay.log" 2>&1 &
RP=$!
sleep 2
date +%s.%N > "$D/t0_reference"

"$PY" -P -u -m aiomoqt.tools.pub_bench "$URL" -N aiomoqt -T track \
    -s 4096 -g 200 -P 1 -t $((RUN_S + 20)) --draft 16 -k --video 1080p --pub-both \
    > "$D/pub.log" 2>&1 &
PP=$!

sleep 2
ssh -o BatchMode=yes seawolf \
  "cd /home/gmarzot/perf-sweep && venv/bin/python -P -u scripts/ttff_probe.py $URL \
   -N aiomoqt -T track --draft 16 -k -t $((RUN_S - 10)) -i 5" \
  > "$D/sub-warm.log" 2>&1 &
WP=$!

sleep $((LATE - 2))
date +%s.%N > "$D/late_start_epoch"
ssh -o BatchMode=yes seawolf \
  "cd /home/gmarzot/perf-sweep && venv/bin/python -P -u scripts/ttff_probe.py $URL \
   -N aiomoqt -T track --draft 16 -k -t $((RUN_S - LATE - 10)) -i 5" \
  > "$D/sub1.log" 2>&1 &
LP=$!

wait $LP 2>/dev/null
wait $WP 2>/dev/null
wait $PP 2>/dev/null
kill -9 "$RP" 2>/dev/null
wait "$RP" 2>/dev/null

echo "=== late joiner (measured) ==="
grep -E "TTFF-JSON|No data" "$D/sub1.log" || echo "MISSING TTFF-JSON in sub1.log"
echo "=== warm subscriber ==="
grep -E "TTFF-JSON|No data" "$D/sub-warm.log" || echo "MISSING TTFF-JSON in sub-warm.log"
echo "=== pub ==="
grep -E "Sent:|Done|rror" "$D/pub.log" | head -5
echo "=== ports after ==="
ss -ltn 2>/dev/null | grep -E ':976[89] ' || echo "9768/9769 released"
