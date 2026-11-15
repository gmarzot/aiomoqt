#!/usr/bin/env bash
# Phase 2a A/B driver — alternating 0.6.1 vs 0.7.0 on three transport points.
#
# One sweep.py invocation per rep so the two arms interleave in the PROGRAM §6
# order A,B,B,A,A,B,B,A,A,B instead of running in per-arm blocks (drift between
# arms would otherwise be indistinguishable from a release delta).
#
# Layout of the block:
#   D3 x5   (early control, block start)   A,B,B,A,A
#   R2 x10                                 A,B,B,A,A,B,B,A,A,B
#   R3 x10                                 A,B,B,A,A,B,B,A,A,B
#   D3 x5   (late control, block end)      B,B,A,A,B
#
# D3 is therefore the control at both ends of the block and carries N=5 per arm
# overall (A: 3 early + 2 late, B: 2 early + 3 late); the early-vs-late group
# means are arm-balanced, so their difference is a clean drift test.
#
# Each rep is its own run block (tag p2a-<slot>), so meta.arm in result.json is
# the arm identity of that rep and no rep can be relabelled by block position.
set -u
cd "$(dirname "$0")/.." || exit 1

LOG=runs/p2a-driver.log
: > "$LOG"
: > runs/p2a-runs.txt
A=cmp-061
B=base-070

run() { # point arm slot
  echo "=== $(date -Is) start $1 $2 $3" >> "$LOG"
  python3 scripts/sweep.py --manifest manifests/phase2a.yaml --only "$1" --reps 1 \
    --arm "$2" --tag "p2a-$3" --out runs --gap 0 \
    --waive clock_offset_jump,clock_unavailable,short_run >> "$LOG" 2>&1
  rc=$?
  echo "=== $(date -Is) done $1 $2 $3 rc=$rc" >> "$LOG"
  echo "$(date -Is) $1 $2 $3 rc=$rc" >> runs/p2a-runs.txt
}

# per-point arm order: A,B,B,A,A,B,B,A,A,B
order5="A B B A A B B A A"

seq_point() { # point prefix order-arms...
  local pt=$1 pre=$2; shift 2
  local i=0 a arm
  for a in "$@"; do
    i=$((i+1))
    if [ "$a" = A ]; then arm=$A; else arm=$B; fi
    run "$pt" "$arm" "$pre-$(printf '%s' "$a" | tr 'AB' 'ab')$i"
  done
}

seq_point D3 d3e A B B A A
seq_point R2 r2  A B B A A B B A A B
seq_point R3 r3  A B B A A B B A A B
seq_point D3 d3l B B A A B

echo "=== $(date -Is) DRIVER COMPLETE" >> "$LOG"
grep -c ' rc=0' runs/p2a-runs.txt >> "$LOG"
