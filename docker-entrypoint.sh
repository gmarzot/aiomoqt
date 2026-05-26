#!/bin/sh
# moq-interop-runner entrypoint. Default role is the client; set
# MOQT_ROLE=relay (matching the runner's docker-compose env) to start
# the minimal interop relay on UDP/${MOQT_PORT:-4443}.
set -eu

case "${MOQT_ROLE:-client}" in
    relay)
        exec python -m aiomoqt.examples.moq_interop_relay \
            --bind "${MOQT_BIND:-0.0.0.0}" \
            --port "${MOQT_PORT:-4443}" \
            --cert "${MOQT_CERT:-/certs/cert.pem}" \
            --key  "${MOQT_KEY:-/certs/priv.key}" \
            ${MOQT_QUIC:+--quic} \
            ${MOQT_DRAFT:+--draft "$MOQT_DRAFT"} \
            "$@"
        ;;
    client|*)
        exec python -m aiomoqt.examples.moq_interop_client "$@"
        ;;
esac
