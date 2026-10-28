#!/usr/bin/env bash
# build_arm.sh — build one frozen benchmark arm.
#
#   build_arm.sh NAME REF [PATCHFILE]
#
#   NAME       arm name (e.g. base-070rc2). Output goes to <perf-sweep>/arms/NAME/
#   REF        aiopquic git ref (tag or sha) to freeze
#   PATCHFILE  optional unified diff applied to the aiopquic tree after checkout
#
# Produces, under <perf-sweep>/arms/NAME/:
#   src/          detached git worktree of aiopquic at REF (+ submodules)
#   wheels/*.whl  host-tuned wheel built from that tree
#   venv/         venv containing aiomoqt (editable, perf-sweep checkout) + that wheel
#   arm.json      REF, submodule SHAs, wheel sha256, interpreter, build flavour
#   versions.txt  the version print (a label that disagrees with it is a bug)
#   build.log     full transcript of the build
#
# Build flavour is fixed by this program (PROGRAM.md §5.4): AIOPQUIC_PERF=1
# host-tuned (-O3 -march=native -flto, DISABLE_DEBUG_PRINTF), system OpenSSL.
# Do not change these without bumping every arm.
#
# An arm with arm.json is refused: a recorded arm is never silently rebuilt
# underneath its results. An arm directory holding a wheel but no arm.json
# (a verify step died) is *resumed* — wheel kept, install/verify/manifest
# re-run — so a failed verify does not orphan a good build.
#
# Python is invoked with CWD=${ARM} and -P: a directory named `aiomoqt` in an
# ancestor CWD shadows the installed package as a namespace package, which
# makes `python -m aiomoqt.versions` die with "unknown location".
set -euo pipefail

NAME=${1:?usage: build_arm.sh NAME REF [PATCHFILE]}
REF=${2:?usage: build_arm.sh NAME REF [PATCHFILE]}
PATCHFILE=${3:-}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PERF_ROOT=$(cd "${SCRIPT_DIR}/../../../.." && pwd)
AIOPQUIC_SRC="${PERF_ROOT}/aiopquic"
AIOMOQT_SRC="${PERF_ROOT}/aiomoqt"
ARM="${PERF_ROOT}/arms/${NAME}"
SRC="${ARM}/src"
VENV="${ARM}/venv"

RESUME=0
if [[ -e "${ARM}" ]]; then
    if [[ -f "${ARM}/arm.json" ]]; then
        echo "ERROR: ${ARM} is a recorded arm; refusing to rebuild it" >&2
        exit 1
    fi
    if [[ -d "${SRC}" ]] && compgen -G "${ARM}/wheels/*.whl" > /dev/null; then
        RESUME=1
    else
        echo "ERROR: ${ARM} exists but is incomplete (no src/ or no wheel); rm -rf it to start over" >&2
        exit 1
    fi
fi

mkdir -p "${ARM}/wheels"
exec > >(tee -a "${ARM}/build.log") 2>&1

echo "=== build_arm $(date -Is) ==="
echo "NAME=${NAME} REF=${REF} PATCHFILE=${PATCHFILE:-none} RESUME=${RESUME}"
echo "PERF_ROOT=${PERF_ROOT}"

unset PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX || true
export AIOPQUIC_PERF=1
export AIOPQUIC_WHEEL_BUILD=0
export AIOPQUIC_IO_URING="${AIOPQUIC_IO_URING:-0}"
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-$(nproc)}"

# --- interpreter -------------------------------------------------------
# Pin an explicitly-installed CPython so the arms do not depend on whatever
# interpreter happens to be on PATH.
ARM_PY_SPEC=${ARM_PY_SPEC:-3.14}
PYBIN=$(uv python find "${ARM_PY_SPEC}")
echo "interpreter: ${PYBIN} ($("${PYBIN}" -V 2>&1))"

if [[ ${RESUME} -eq 0 ]]; then
    # --- source tree: detached worktree at REF -------------------------
    echo "--- worktree add ${REF} ---"
    git -C "${AIOPQUIC_SRC}" worktree add --detach "${SRC}" "${REF}"
    git -C "${SRC}" submodule update --init --recursive
    if [[ -n "${PATCHFILE}" ]]; then
        echo "--- applying patch ${PATCHFILE} ---"
        git -C "${SRC}" apply --verbose "$(cd "$(dirname "${PATCHFILE}")" && pwd)/$(basename "${PATCHFILE}")"
    fi
else
    echo "--- resume: keeping existing ${SRC} and wheel ---"
fi

AIOPQUIC_SHA=$(git -C "${SRC}" rev-parse HEAD)
AIOPQUIC_DESC=$(git -C "${SRC}" describe --tags --always --dirty)
PICOQUIC_SHA=$(git -C "${SRC}/third_party/picoquic" rev-parse HEAD)
PICOTLS_SHA=$(git -C "${SRC}/third_party/picotls" rev-parse HEAD)
LIBURING_SHA=$(git -C "${SRC}/third_party/liburing" rev-parse HEAD)
echo "aiopquic: ${AIOPQUIC_DESC} ${AIOPQUIC_SHA}"
echo "picoquic: ${PICOQUIC_SHA}"
echo "picotls:  ${PICOTLS_SHA}"
echo "liburing: ${LIBURING_SHA}"

# --- venv --------------------------------------------------------------
echo "--- venv ---"
if [[ ! -x "${VENV}/bin/python" ]]; then
    uv venv --python "${PYBIN}" --seed "${VENV}"
    uv pip install --python "${VENV}/bin/python" "setuptools>=75" "setuptools_scm[toml]>=9" "cython>=3.2.0" wheel
fi
VPY="${VENV}/bin/python"

if [[ ${RESUME} -eq 0 ]]; then
    # --- native libs (host-tuned) --------------------------------------
    echo "--- build.sh --native ---"
    ( cd "${SRC}" && PYTHON="${VPY}" ./build.sh --native )

    # --- wheel ---------------------------------------------------------
    echo "--- wheel ---"
    ( cd "${SRC}" && "${VPY}" -m pip wheel . --no-deps --no-build-isolation -w "${ARM}/wheels" )
fi
WHEEL=$(ls -1 "${ARM}/wheels"/*.whl 2>/dev/null | head -1 || true)
[[ -n "${WHEEL}" ]] || { echo "ERROR: no wheel produced" >&2; exit 1; }
WHEEL_SHA=$(sha256sum "${WHEEL}" | cut -d' ' -f1)
echo "wheel: $(basename "${WHEEL}") sha256=${WHEEL_SHA}"

# --- install arm -------------------------------------------------------
echo "--- install ---"
uv pip install --python "${VPY}" --reinstall "${WHEEL}"
uv pip install --python "${VPY}" --no-deps -e "${AIOMOQT_SRC}"
uv pip install --python "${VPY}" certifi "sortedcontainers>=2.4.0"

# --- verify ------------------------------------------------------------
echo "--- versions ---"
cd "${ARM}"
"${VPY}" -P -m aiomoqt.versions | tee versions.txt
"${VPY}" -P -c "import aiopquic, sys; print('aiopquic.__file__ =', aiopquic.__file__); print('python =', sys.version.split()[0])" >> versions.txt
cat versions.txt

# --- manifest ----------------------------------------------------------
cat > "${ARM}/arm.json" <<EOF
{
  "name": "${NAME}",
  "ref": "${REF}",
  "patch": "${PATCHFILE:-}",
  "aiopquic_sha": "${AIOPQUIC_SHA}",
  "aiopquic_describe": "${AIOPQUIC_DESC}",
  "picoquic_sha": "${PICOQUIC_SHA}",
  "picotls_sha": "${PICOTLS_SHA}",
  "liburing_sha": "${LIBURING_SHA}",
  "wheel": "$(basename "${WHEEL}")",
  "wheel_sha256": "${WHEEL_SHA}",
  "python": "$("${VPY}" -P -V 2>&1)",
  "python_bin": "${PYBIN}",
  "aiopquic_perf": "${AIOPQUIC_PERF}",
  "aiopquic_io_uring": "${AIOPQUIC_IO_URING}",
  "build_cmd": "AIOPQUIC_PERF=1 AIOPQUIC_IO_URING=${AIOPQUIC_IO_URING} ./build.sh --native && python -m pip wheel . --no-deps --no-build-isolation",
  "built_at": "$(date -Is)"
}
EOF
echo "=== build_arm OK $(date -Is) ==="
