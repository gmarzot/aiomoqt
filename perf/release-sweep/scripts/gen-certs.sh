#!/bin/bash
# gen-certs.sh — throwaway self-signed TLS material for the sweep harness.
#
# The bench tools only need *a* cert: subscribers connect with -k (insecure),
# so identity does not matter; the SAN list matters only when something
# verifies instead (moqx with insecure: false, relay_probe without -k).
# Output is certs/ which is gitignored — never commit these files.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p certs
CN="${CN:-aiomoqt-bench}"
DAYS="${DAYS:-365}"

if [[ -s certs/cert.pem && -s certs/key.pem ]]; then
    echo "certs/ already present (cert.pem, key.pem); delete them to regenerate"
    exit 0
fi

openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
    -keyout certs/key.pem -out certs/cert.pem \
    -subj "/CN=$CN" \
    -addext "subjectAltName=DNS:localhost,DNS:shenzhen,DNS:seawolf,IP:127.0.0.1,IP:10.14.25.173" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
    2>/dev/null
chmod 600 certs/key.pem
echo "wrote certs/cert.pem certs/key.pem (CN=$CN, ${DAYS}d)"
