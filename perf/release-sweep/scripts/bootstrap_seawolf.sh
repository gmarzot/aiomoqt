#!/usr/bin/env bash
# Provision the seawolf load host with the pinned measurement environment.
#
# The shenzhen arm venv is CPython 3.14 + the frozen aiopquic cp314 wheel.
# seawolf ships 3.12 only, so we install a matching CPython via uv and a venv
# that consumes the *same wheel files* (same sha256) as the local arm.
#
# Usage:
#   scripts/bootstrap_seawolf.sh [--remote HOST] [--dest DIR] ARM_DIR AIOMOQT_WHEEL
#
# Example:
#   scripts/bootstrap_seawolf.sh ../../arms/base-070rc2 \
#       /tmp/aiomoqt-wheels/aiomoqt-0.13.4.dev3+ged6dbdc3c-py3-none-any.whl
set -euo pipefail

REMOTE=seawolf
DEST=/home/gmarzot/perf-sweep
while [[ $# -gt 0 ]]; do
  case $1 in
    --remote) REMOTE=$2; shift 2 ;;
    --dest)   DEST=$2;   shift 2 ;;
    *) break ;;
  esac
done
ARM_DIR=${1:?arm dir (contains wheels/ and arm.json)}
AIOMOQT_WHEEL=${2:?aiomoqt wheel path}

AIOPQUIC_WHEEL=$(ls "$ARM_DIR"/wheels/aiopquic-*.whl | head -1)
[[ -f "$AIOPQUIC_WHEEL" ]] || { echo "no aiopquic wheel in $ARM_DIR/wheels" >&2; exit 1; }

echo "==> remote=$REMOTE dest=$DEST"
ssh -o BatchMode=yes "$REMOTE" "mkdir -p '$DEST/wheels' '$DEST/bin' '$DEST/runs'"

echo "==> copying wheels"
scp -q "$AIOPQUIC_WHEEL" "$AIOMOQT_WHEEL" "$REMOTE:$DEST/wheels/"

echo "==> installing CPython + venv (uv)"
ssh -o BatchMode=yes "$REMOTE" bash -s -- "$DEST" <<'EOS'
set -euo pipefail
DEST=$1
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "    installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Prefer the same patch release as the local arm; fall back to latest 3.14.
uv python install 3.14.6 2>/dev/null || uv python install 3.14
PY=$(uv python find 3.14)
echo "    python: $PY ($($PY -V))"
uv venv --python "$PY" --clear "$DEST/venv"
uv pip install --python "$DEST/venv/bin/python" --no-deps \
    "$DEST"/wheels/aiopquic-*.whl "$DEST"/wheels/aiomoqt-*.whl
"$DEST/venv/bin/python" - <<'PY'
import aiomoqt, aiopquic, sys, platform
print("    python:", sys.version.split()[0], platform.machine())
print("    aiomoqt:", aiomoqt.__version__)
print("    aiopquic:", aiopquic.__version__)
PY
EOS

echo "==> wheel sha256 (remote)"
ssh -o BatchMode=yes "$REMOTE" "sha256sum $DEST/wheels/*.whl"
echo "==> done"
