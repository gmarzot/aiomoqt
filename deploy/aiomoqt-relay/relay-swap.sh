#!/bin/bash
# relay-swap.sh IMAGE [HHMMSS]: make IMAGE the live relay, at HHMMSS UTC if given. First records the
# live image, relay.toml and compose.yml in rollback/<UTC time>/, which relay-rollback.sh restores.
set -euo pipefail
cd "$(dirname "$0")"
IMG=${1:?usage: relay-swap.sh IMAGE [HHMMSS]}

probe() {
    local out
    out=$(docker exec relay-swap-probe python -m aiomoqt.tools.relay_probe -k --timeout 2 \
          --draft 22 --url "$1" 2>&1) || true
    [[ $out == *✓* ]]
}

# Probe raw QUIC and WebTransport until both connect, up to 30 s after $1 (epoch ms).
serving() {
    local q="" w=""
    while (( $(date +%s%3N) - $1 < 30000 )); do
        [ -n "$q" ] || ! probe moqt://127.0.0.1:4443 || q=$(( $(date +%s%3N) - $1 ))
        [ -n "$w" ] || ! probe https://127.0.0.1:4443/ || w=$(( $(date +%s%3N) - $1 ))
        if [ -n "$q" ] && [ -n "$w" ]; then
            echo "serving: raw QUIC +$q ms, WebTransport +$w ms after the restart began"
            return 0
        fi
        sleep 0.2
    done
    return 1
}

v=$(docker run --rm --entrypoint python "$IMG" -m aiomoqt.versions) \
    || { echo "$IMG does not start; nothing changed"; exit 1; }
echo "$v" | head -2
LIVE=$(docker inspect --format '{{.Image}}' aiomoqt-relay) \
    || { echo "no aiomoqt-relay container to roll back to; nothing changed"; exit 1; }
[ "$LIVE" != "$(docker image inspect --format '{{.Id}}' "$IMG")" ] \
    || { echo "$IMG is already live; nothing changed"; exit 1; }

if [ -n "${2:-}" ]; then
    at=$(date -ud "$(date -u +%F) ${2:0:2}:${2:2:2}:${2:4:2}" +%s)
    (( at > $(date +%s) )) || { echo "$2 UTC has passed; nothing changed"; exit 1; }
    echo "waiting for $2 UTC"
    while (( $(date +%s) < at )); do sleep 0.2; done
fi

R=rollback/$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$R"
echo "$LIVE" > "$R/image-id"
docker image inspect --format '{{join .RepoTags " "}}' "$LIVE" > "$R/image-tags"
cp relay.toml compose.yml "$R/"
docker tag "$LIVE" aiomoqt:rollback
ln -sfn "${R#rollback/}" rollback/last
echo "rollback recorded in $R: $(cat "$R/image-tags")"

docker run -d --rm --network host --name relay-swap-probe --entrypoint sleep "$IMG" 300 >/dev/null
trap 'docker rm -f relay-swap-probe >/dev/null 2>&1 || true' EXIT
docker tag "$IMG" aiomoqt:dev
t0=$(date +%s%3N)
echo "restart $(date -u +%T.%3N)"
if ! out=$(docker compose up -d relay 2>&1); then
    echo "$out"; echo "docker compose up failed: run ./relay-rollback.sh"; exit 1
fi
grep -v DEPRECATED <<< "$out" || true
serving "$t0" || { echo "relay not serving 30 s after the restart: run ./relay-rollback.sh"; exit 1; }
tail -n 400 relay-logs/relay.log | grep -E "relay: (refusing|SIGTERM|drain|start)" | tail -4 | cut -c1-160
