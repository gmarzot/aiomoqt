#!/usr/bin/env bash
# Baseline benchmark sweep: 3 rates x {sub profile, pub profile}
# Outputs to perf/baselines/<label>/ with logs + .prof files.
#
# Usage:
#   perf/baseline_sweep.sh <label>
#
# Example:
#   perf/baseline_sweep.sh released-0.3.1-0.9.3
set -u

LABEL="${1:?label required (e.g. released-0.3.1-0.9.3)}"
PYTHON="${PYTHON:-/tmp/aiomoqt-release-test/bin/python}"
RELAY="${RELAY:-moqt://moqx-local.marzresearch.net:4433}"
DRAFT="${DRAFT:-16}"
NAMESPACE="${NAMESPACE:-aiomoqt}"
PUB_DUR="${PUB_DUR:-30}"
SUB_DUR="${SUB_DUR:-22}"
STREAMS="${STREAMS:-2}"
OBJ_SIZE="${OBJ_SIZE:-1024}"
GROUP_SIZE="${GROUP_SIZE:-120}"
RATES_STR="${RATES:-1000 5000 10000}"
read -r -a RATES <<<"$RATES_STR"

OUT_DIR="$(dirname "$0")/baselines/$LABEL"
mkdir -p "$OUT_DIR"

ver=$($PYTHON -c 'import aiopquic, aiomoqt; print(f"aiopquic={aiopquic.__version__} aiomoqt={aiomoqt.__version__}")')
{
  echo "label:    $LABEL"
  echo "date:     $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host:     $(hostname) $(uname -srm)"
  echo "python:   $PYTHON"
  echo "versions: $ver"
  echo "relay:    $RELAY"
  echo "draft:    $DRAFT"
  echo "config:   -P $STREAMS -s $OBJ_SIZE -g $GROUP_SIZE -t $PUB_DUR --pub-both"
  echo "rates:    ${RATES[*]}"
} | tee "$OUT_DIR/meta.txt"

for r in "${RATES[@]}"; do
  printf '\n========== rate=%s obj/s ==========\n' "$r"

  TRACK_A="bench-r${r}-A-$(uuidgen | head -c 8)"
  echo "[A] sub profiled, pub plain; track=$TRACK_A"
  $PYTHON -m aiomoqt.examples.pub_bench "$RELAY" --draft "$DRAFT" \
    --namespace "$NAMESPACE" --trackname "$TRACK_A" -P "$STREAMS" \
    -s "$OBJ_SIZE" -g "$GROUP_SIZE" -r "$r" -t "$PUB_DUR" --pub-both \
    >"$OUT_DIR/A-r${r}-pub.log" 2>&1 &
  PUB_PID=$!
  sleep 2
  $PYTHON -m cProfile -o "$OUT_DIR/A-r${r}-sub.prof" \
    -m aiomoqt.examples.sub_bench "$RELAY" --draft "$DRAFT" \
    --namespace "$NAMESPACE" --trackname "$TRACK_A" -i 5 -t "$SUB_DUR" \
    >"$OUT_DIR/A-r${r}-sub.log" 2>&1
  wait $PUB_PID 2>/dev/null || true
  echo "  done; sub log tail:"
  tail -n 14 "$OUT_DIR/A-r${r}-sub.log" | sed 's/^/    /'
  sleep 3

  TRACK_B="bench-r${r}-B-$(uuidgen | head -c 8)"
  echo "[B] pub profiled, sub plain; track=$TRACK_B"
  $PYTHON -m cProfile -o "$OUT_DIR/B-r${r}-pub.prof" \
    -m aiomoqt.examples.pub_bench "$RELAY" --draft "$DRAFT" \
    --namespace "$NAMESPACE" --trackname "$TRACK_B" -P "$STREAMS" \
    -s "$OBJ_SIZE" -g "$GROUP_SIZE" -r "$r" -t "$PUB_DUR" --pub-both \
    >"$OUT_DIR/B-r${r}-pub.log" 2>&1 &
  PUB_PID=$!
  sleep 2
  $PYTHON -m aiomoqt.examples.sub_bench "$RELAY" --draft "$DRAFT" \
    --namespace "$NAMESPACE" --trackname "$TRACK_B" -i 5 -t "$SUB_DUR" \
    >"$OUT_DIR/B-r${r}-sub.log" 2>&1
  wait $PUB_PID 2>/dev/null || true
  echo "  done; sub log tail:"
  tail -n 14 "$OUT_DIR/B-r${r}-sub.log" | sed 's/^/    /'
  sleep 3
done

echo
echo "All runs complete. Output: $OUT_DIR"
ls -la "$OUT_DIR" | sed 's/^/  /'
