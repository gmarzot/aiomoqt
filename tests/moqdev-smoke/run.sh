#!/usr/bin/env bash
# moq-dev's relay smoke (moq-dev/moq test/smoke) against registry relays, in an ubuntu:24.04 container.
# Usage: run.sh OUT_DIR [relay-keys] [publishers] [subscribers]
# Writes OUT_DIR/{results.json,logs/,relays.out}. The harness and moq-dev binaries are cached in
# $MOQDEV_SMOKE_CACHE (default ~/.cache/aiomoqt-moqdev-smoke).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$1"
OUT="$(cd "$1" && pwd)"
# A stale results.json from a reused OUT_DIR would read as this run's verdict.
rm -f "$OUT/results.json"
KEYS="${2:-aiomoqt-relay}"
PUBS="${3:-rust,js-web}"
SUBS="${4:-rust,js-web,js-bun}"
CACHE="${MOQDEV_SMOKE_CACHE:-$HOME/.cache/aiomoqt-moqdev-smoke}"
IMAGE=moqdev-smoke:latest
mkdir -p "$CACHE/bin"

if [ -d "$CACHE/moq/.git" ]; then
    git -C "$CACHE/moq" pull -q --ff-only
else
    git clone -q --depth 1 --filter=blob:none --sparse https://github.com/moq-dev/moq.git "$CACHE/moq"
fi
# The harness's freshness check reads moq-dev's workflow files.
git -C "$CACHE/moq" sparse-checkout set test/smoke .github/workflows
SMOKE="$CACHE/moq/test/smoke"
echo "harness: moq-dev/moq $(git -C "$CACHE/moq" rev-parse --short HEAD)"

# The browser must match the harness's npm playwright pin.
PW=$(jq -r '[.. | objects | .playwright? // empty] | first // empty' "$SMOKE/clients/js/package.json")
PW="${PW#[~^]}"
[ -n "$PW" ] || { echo "error: no playwright pin in $SMOKE/clients/js/package.json" >&2; exit 1; }
docker build -t "$IMAGE" --build-arg PLAYWRIGHT_VERSION="$PW" "$HERE"

# moq-relay and moq: moq-dev's latest x86_64 Linux release binaries.
releases=$(curl -fsSL "https://api.github.com/repos/moq-dev/moq/releases?per_page=60")
for pkg in moq-relay:moq-relay moq-cli:moq; do
    name="${pkg%%:*}" bin="${pkg##*:}"
    tag=$(jq -r --arg p "$name-v" '[.[] | select(.tag_name | startswith($p))][0].tag_name' <<<"$releases")
    url="https://github.com/moq-dev/moq/releases/download/$tag/$tag-x86_64-unknown-linux-gnu.tar.gz"
    curl -fsSL "$url" | tar -xz -C "$CACHE/bin" --strip-components=2 "$tag-x86_64-unknown-linux-gnu/bin/$bin"
    echo "$bin: $tag"
done

# The in-container timeout bounds the run: killing the docker client would leave the container.
docker run --rm --init --name moqdev-smoke --network host --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -e RELAY_BIN=/work/bin/moq-relay -e MOQ_BIN=/work/bin/moq \
    -v "$CACHE:/work" -v "$OUT:/out" -w /work/moq/test/smoke "$IMAGE" \
    timeout -k 10 900 ./relays.sh --only "$KEYS" --required "$KEYS" \
    --publishers "$PUBS" --subscribers "$SUBS" --timeout 30 \
    --json /out/results.json --logs /out/logs 2>&1 | tee "$OUT/relays.out" || true
