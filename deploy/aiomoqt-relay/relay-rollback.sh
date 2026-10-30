#!/bin/bash
# relay-rollback.sh [--check]: restore the image, relay.toml and compose.yml that the last
# relay-swap.sh recorded, and restart the relay. --check shows what would change and that the
# image starts, and changes nothing.
set -euo pipefail
cd "$(dirname "$0")"
R=rollback/last

probe() {
    local out
    out=$(docker exec relay-rollback-probe python -m aiomoqt.tools.relay_probe -k --timeout 2 \
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

[ -f "$R/image-id" ] || { echo "no rollback recorded in $R"; exit 1; }
ID=$(cat "$R/image-id")
echo "rollback to $(cat "$R/image-tags") (${ID:7:12}), recorded $(readlink "$R")"
v=$(docker run --rm --entrypoint python "$ID" -m aiomoqt.versions) \
    || { echo "the rollback image does not start"; exit 1; }
echo "$v" | head -2
for f in relay.toml compose.yml; do
    cmp -s "$R/$f" "$f" || { echo "$f differs from the recorded copy:"; diff "$R/$f" "$f" || true; }
done
if [ "$(docker inspect --format '{{.Image}}' aiomoqt-relay 2>/dev/null)" = "$ID" ]; then
    echo "the live relay already runs this image"
fi
[ "${1:-}" != --check ] || exit 0

cp "$R/relay.toml" "$R/compose.yml" .
docker run -d --rm --network host --name relay-rollback-probe --entrypoint sleep "$ID" 300 >/dev/null
trap 'docker rm -f relay-rollback-probe >/dev/null 2>&1 || true' EXIT
docker tag "$ID" aiomoqt:dev
t0=$(date +%s%3N)
echo "restart $(date -u +%T.%3N)"
if ! out=$(docker compose up -d relay 2>&1); then
    echo "$out"; echo "docker compose up failed"; exit 1
fi
grep -v DEPRECATED <<< "$out" || true
serving "$t0" || { echo "relay not serving 30 s after the restart"; exit 1; }
tail -n 400 relay-logs/relay.log | grep -E "relay: (refusing|SIGTERM|drain|start)" | tail -4 | cut -c1-160
