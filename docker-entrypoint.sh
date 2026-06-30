#!/bin/sh
# moq-interop-runner entrypoint. Default role is the client; set
# MOQT_ROLE=relay (matching the runner's docker-compose env) to start
# the minimal interop relay on UDP/${MOQT_PORT:-4443}.
#
# Draft confinement is read from the environment by BOTH roles: the runner
# injects DRAFT (PR #95) — or older MOQT_DRAFT — and the client and relay
# each pin to it. Unset = open-relay context: the client offers its full
# version list and the relay advertises all supported drafts. So no --draft
# is threaded here; the programs read $DRAFT / $MOQT_DRAFT themselves.
set -eu

case "${MOQT_ROLE:-client}" in
    relay)
        exec python -m aiomoqt.examples.moq_interop_relay \
            --bind "${MOQT_BIND:-0.0.0.0}" \
            --port "${MOQT_PORT:-4443}" \
            --cert "${MOQT_CERT:-/certs/cert.pem}" \
            --key  "${MOQT_KEY:-/certs/priv.key}" \
            ${MOQT_QUIC:+--quic} \
            "$@"
        ;;
    client|*)
        exec python -m aiomoqt.examples.moq_interop_client "$@"
        ;;
esac
