#!/usr/bin/env bash
# Provision / verify the seawolf load host with the pinned measurement environment.
#
# The seawolf venv must match the local arm venv in everything except the two project
# wheels: same CPython, same pinned dependency set. The first version of this script
# installed only the two wheels with --no-deps, which left `sortedcontainers` (an
# aiomoqt dependency) out, so aiomoqt could not even be imported on the load host.
# This version:
#   1. takes the FULL freeze of the local arm venv (minus the editable aiomoqt and the
#      aiopquic wheel entry) and installs it pinned (`--no-deps`: the freeze is closed);
#   2. installs the arm's aiopquic wheel + the aiomoqt wheel (same files, same sha256);
#   3. verifies: freeze identical to local, CPython identical, imports work, wheel
#      sha256 identical. Any drift exits non-zero.
#
# Modes:  (default) ensure   idempotent: create the venv only if missing, then converge
#         --rebuild          recreate the venv from scratch (uv venv --clear)
#         --verify-only      change nothing; exit non-zero on any drift
#
# Usage:
#   scripts/bootstrap_seawolf.sh [--remote HOST] [--dest DIR] [--rebuild|--verify-only] \
#       ARM_DIR AIOMOQT_WHEEL
#
# Example:
#   scripts/bootstrap_seawolf.sh /home/gmarzot/Projects/marzresearch/perf-sweep/arms/base-070rc2 \
#       /tmp/aiomoqt-wheels/aiomoqt-0.13.4.dev3+ged6dbdc3c-py3-none-any.whl
set -euo pipefail

REMOTE=seawolf
DEST=/home/gmarzot/perf-sweep
MODE=ensure
while [[ $# -gt 0 ]]; do
  case $1 in
    --remote)      REMOTE=$2; shift 2 ;;
    --dest)        DEST=$2;   shift 2 ;;
    --rebuild)     MODE=rebuild; shift ;;
    --verify-only) MODE=verify;  shift ;;
    *) break ;;
  esac
done
ARM_DIR=${1:?arm dir (contains venv/ and wheels/)}
AIOMOQT_WHEEL=${2:?aiomoqt wheel path}

ARM_PY="$ARM_DIR/venv/bin/python"
[[ -x $ARM_PY ]] || { echo "arm interpreter missing: $ARM_PY" >&2; exit 1; }
AIOPQUIC_WHEEL=$(ls "$ARM_DIR"/wheels/aiopquic-*.whl | head -1)
[[ -f $AIOPQUIC_WHEEL && -f $AIOMOQT_WHEEL ]] || { echo "wheel(s) not found" >&2; exit 1; }
AQ=$(basename "$AIOPQUIC_WHEEL"); AM=$(basename "$AIOMOQT_WHEEL")

FREEZE=$(mktemp)
trap 'rm -f "$FREEZE"' EXIT
uv pip freeze --python "$ARM_PY" 2>/dev/null \
  | grep -v -E '^(-e |aiopquic[ =@]|aiomoqt[ =@])' | LC_ALL=C sort > "$FREEZE"
grep -q '^sortedcontainers==' "$FREEZE" \
  || { echo "arm freeze lacks sortedcontainers -- arm venv incomplete?" >&2; exit 1; }
echo "==> remote=$REMOTE dest=$DEST mode=$MODE; arm freeze: $(wc -l < "$FREEZE") pinned packages"

SSH=(ssh -o BatchMode=yes "$REMOTE")

if [[ $MODE != verify ]]; then
  "${SSH[@]}" "mkdir -p '$DEST/wheels' '$DEST/bin' '$DEST/runs'"
  scp -q "$AIOPQUIC_WHEEL" "$AIOMOQT_WHEEL" "$REMOTE:$DEST/wheels/"
  scp -q "$FREEZE" "$REMOTE:$DEST/requirements-freeze.txt"
  echo "==> converging venv (uv)"
  "${SSH[@]}" bash -s -- "$DEST" "$MODE" "$AQ" "$AM" <<'EOS'
set -euo pipefail
DEST=$1; MODE=$2; AQ=$3; AM=$4
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
if [[ $MODE == rebuild || ! -x $DEST/venv/bin/python ]]; then
  # Prefer the same patch release as the local arm; fall back to latest 3.14.
  uv python install 3.14.6 2>/dev/null || uv python install 3.14
  uv venv --python "$(uv python find 3.14)" --clear "$DEST/venv"
fi
PYV=$DEST/venv/bin/python
uv pip install --python "$PYV" --no-deps -r "$DEST/requirements-freeze.txt"
uv pip install --python "$PYV" --no-deps "$DEST/wheels/$AQ" "$DEST/wheels/$AM"
EOS
fi

echo "==> verify"
rc=0
RFREEZE=$("${SSH[@]}" "export PATH=\$HOME/.local/bin:\$PATH; uv pip freeze --python '$DEST/venv/bin/python' 2>/dev/null | grep -v -E '^(-e |aiopquic[ =@]|aiomoqt[ =@])' | LC_ALL=C sort")
if ! diff <(printf '%s\n' "$RFREEZE") "$FREEZE"; then
  echo "DRIFT: seawolf venv freeze != local arm freeze" >&2; rc=2
else
  echo "    freeze identical ($(wc -l < "$FREEZE") pins)"
fi
LPY=$("$ARM_PY" -c 'import sys; print(sys.version.split()[0])')
RPY=$("${SSH[@]}" "$DEST/venv/bin/python -c 'import sys; print(sys.version.split()[0])'")
if [[ $LPY != "$RPY" ]]; then echo "DRIFT: CPython local=$LPY remote=$RPY" >&2; rc=3; else echo "    CPython $RPY on both"; fi
for W in "$AIOPQUIC_WHEEL" "$AIOMOQT_WHEEL"; do
  L=$(sha256sum "$W" | cut -d' ' -f1)
  R=$("${SSH[@]}" "sha256sum '$DEST/wheels/$(basename "$W")'" | cut -d' ' -f1)
  if [[ $L != "$R" ]]; then echo "DRIFT: sha256 differs for $(basename "$W")" >&2; rc=4; else echo "    sha256 ok  $(basename "$W")  ${L:0:16}"; fi
done
"${SSH[@]}" "$DEST/venv/bin/python -c 'import aiomoqt, aiopquic, sortedcontainers, certifi, sys; print(\"    imports ok: aiomoqt\", aiomoqt.__version__, \"aiopquic\", aiopquic.__version__, \"sortedcontainers\", sortedcontainers.__version__)'" || rc=5
[[ $rc -eq 0 ]] && echo "==> done: seawolf matches the arm" || echo "==> FAILED rc=$rc" >&2
exit $rc
