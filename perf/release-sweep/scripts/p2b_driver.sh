#!/usr/bin/env bash
# Phase 2b A/B driver — alternating 0.6.1 vs 0.7.0 on the media (M1) and
# late-join (C2) points, with the D3 control at both ends of the block.
#
# Same discipline as Phase 2a (scripts/p2a_driver.sh): one measurement
# invocation per rep so the arms interleave in the PROGRAM §6.2 order
# A,B,B,A,A,B,B,A,A,B instead of running in per-arm blocks. Per-arm blocks
# would make block drift indistinguishable from a release delta.
#
# Layout of the block:
#   D3 x5   (early control, block start)   A,B,B,A,A
#   M1 x10                                 A,B,B,A,A,B,B,A,A,B
#   C2 x10                                 A,B,B,A,A,B,B,A,A,B
#   D3 x5   (late control, block end)      B,B,A,A,B
#
# D3 carries N=5 per arm overall (A: 3 early + 2 late, B: 2 early + 3 late); the
# early-vs-late group means are arm-balanced, so their difference is a clean
# drift test. Early and late groups are separated by ~45 min, which is what makes
# the drift test meaningful for the media/churn window.
#
# M1 runs through sweep.py (topology M). C2 cannot: the topology-C2 path in
# sweep.py cannot hold publishing state, so C2 runs through c2_latejoin.sh with
# the arm in $ARM (see manifests/phase2b.yaml header).
#
# Every rep is its own run block (tag p2b-<slot>), so meta.arm in result.json is
# the arm identity of that rep and no rep can be relabelled by block position.
set -u
cd "$(dirname "$0")/.." || exit 1

LOG=runs/p2b-driver.log
: > "$LOG"
: > runs/p2b-runs.txt
A=cmp-061
B=base-070
C2_RUN_S=60
C2_LATE=30

sw() { # point arm slot
  echo "=== $(date -Is) start $1 $2 $3" >> "$LOG"
  python3 scripts/sweep.py --manifest manifests/phase2b.yaml --only "$1" --reps 1 \
    --arm "$2" --tag "p2b-$3" --out runs --gap 0 \
    --waive clock_offset_jump,clock_unavailable,short_run >> "$LOG" 2>&1
  rc=$?
  echo "=== $(date -Is) done $1 $2 $3 rc=$rc" >> "$LOG"
  echo "$(date -Is) $1 $2 $3 rc=$rc" >> runs/p2b-runs.txt
}

c2() { # arm slot
  echo "=== $(date -Is) start C2 $1 $2" >> "$LOG"
  ARM="$1" scripts/c2_latejoin.sh runs/p2b-c2 "c2-$2" "$C2_LATE" "$C2_RUN_S" >> "$LOG" 2>&1
  rc=$?
  echo "=== $(date -Is) done C2 $1 $2 rc=$rc" >> "$LOG"
  echo "$(date -Is) C2 $1 $2 rc=$rc" >> runs/p2b-runs.txt
}

seq_point() { # point-prefix slot-prefix arms...
  local pt=$1 pre=$2; shift 2
  local i=0 a arm
  for a in "$@"; do
    i=$((i+1))
    if [ "$a" = A ]; then arm=$A; else arm=$B; fi
    sw "$pt" "$arm" "$pre-$(printf '%s' "$a" | tr 'AB' 'ab')$i"
  done
}

seq_c2() { # slot-prefix arms...
  local pre=$1; shift
  local i=0 a arm
  for a in "$@"; do
    i=$((i+1))
    if [ "$a" = A ]; then arm=$A; else arm=$B; fi
    c2 "$arm" "$pre-$(printf '%s' "$a" | tr 'AB' 'ab')$i"
  done
}

seq_point D3 d3e A B B A A
seq_point M1 m1  A B B A A B B A A B
seq_c2     c2    A B B A A B B A A B
seq_point D3 d3l B B A A B

echo "=== $(date -Is) DRIVER COMPLETE" >> "$LOG"
grep -c ' rc=0' runs/p2b-runs.txt >> "$LOG"
