#!/bin/sh
# Container entrypoint. MOQT_ROLE selects the program:
#   client (default) — moq-interop-runner client
#   relay            — aiomoqt-relay on UDP/${MOQT_PORT:-4443}
#   origin           — the moqtest origin (conformance SUT, serves
#                      draft-afrind-moq-test tracks incl. FETCH)
# MOQT_QUIC=1 serves raw QUIC instead of WebTransport; MOQT_DUAL=1 both
# on one port. MOQT_BIND limits the listener to one address (default: all
# interfaces, IPv4 and IPv6). Draft confinement comes from the environment: the runner
# injects DRAFT (or older MOQT_DRAFT) and each program pins to it; unset
# = offer/advertise every supported draft.
set -eu

case "${MOQT_ROLE:-client}" in
    relay)
        exec python -m aiomoqt.tools.moq_interop_relay \
            ${MOQT_BIND:+--bind "$MOQT_BIND"} \
            --port "${MOQT_PORT:-4443}" \
            --cert "${MOQT_CERT:-/certs/cert.pem}" \
            --key  "${MOQT_KEY:-/certs/priv.key}" \
            ${MOQT_QUIC:+--quic} \
            ${MOQT_DUAL:+--dual} \
            "$@"
        ;;
    origin)
        exec python -m aiomoqt.tools.moqtest_origin \
            ${MOQT_BIND:+--bind "$MOQT_BIND"} \
            --port "${MOQT_PORT:-4443}" \
            --cert "${MOQT_CERT:-/certs/cert.pem}" \
            --key  "${MOQT_KEY:-/certs/priv.key}" \
            ${MOQT_QUIC:+--quic} \
            ${MOQT_DUAL:+--dual} \
            ${DRAFT:+--draft "$DRAFT"} \
            "$@"
        ;;
    client|*)
        exec python -m aiomoqt.tools.moq_interop_client "$@"
        ;;
esac
