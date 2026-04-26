"""B3 — Stream open/close churn (transport-only, no MoQT).

One persistent connection, then open N uni streams in sequence,
each carries a small payload + FIN. Server discards. Measures
streams/sec sustained and time-to-completion distribution per
stream batch.

This isolates per-stream overhead: stream-id allocation, control-
frame issuance (MAX_STREAMS, etc.), callback dispatch on receive
side.

Args:
  --backend qh3|aiopquic   transport (default aiopquic)
  --n-streams N            total streams to open (default 10000)
  --payload-size N         bytes per stream (default 100)
  --port N                 server port (default 47444)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import ssl
import sys
import time

AIOPQUIC = os.environ.get(
    'AIOPQUIC_DIR', '/home/gmarzot/Projects/moq/aiopquic')
CERT = os.path.join(AIOPQUIC, 'third_party/picoquic/certs/cert.pem')
KEY = os.path.join(AIOPQUIC, 'third_party/picoquic/certs/key.pem')


async def _qh3_run(args):
    from qh3.quic.configuration import QuicConfiguration
    from qh3.asyncio.server import serve
    from qh3.asyncio.client import connect
    from qh3.quic.events import StreamDataReceived
    from qh3.asyncio.protocol import QuicConnectionProtocol

    received = 0

    class SinkServer(QuicConnectionProtocol):
        def quic_event_received(self, event):
            nonlocal received
            if isinstance(event, StreamDataReceived) and event.end_stream:
                received += 1

    server_cfg = QuicConfiguration(is_client=False, alpn_protocols=['churn'])
    server_cfg.load_cert_chain(CERT, KEY)
    client_cfg = QuicConfiguration(
        is_client=True, alpn_protocols=['churn'],
        verify_mode=ssl.CERT_NONE,
    )

    s = await serve(host='127.0.0.1', port=args.port,
                    configuration=server_cfg,
                    create_protocol=SinkServer)
    try:
        async with connect('127.0.0.1', args.port,
                           configuration=client_cfg) as client:
            return await _drive_churn(client, args, qh3=True,
                                        get_received=lambda: received)
    finally:
        s.close()


async def _aiopquic_run(args):
    from aiopquic.quic.configuration import QuicConfiguration
    from aiopquic.asyncio.server import serve
    from aiopquic.asyncio.client import connect
    from aiopquic.quic.events import StreamDataReceived
    from aiopquic.asyncio.protocol import QuicConnectionProtocol

    received = 0

    class SinkServer(QuicConnectionProtocol):
        def quic_event_received(self, event):
            nonlocal received
            if isinstance(event, StreamDataReceived) and event.end_stream:
                received += 1

    server_cfg = QuicConfiguration(is_client=False, alpn_protocols=['churn'])
    server_cfg.load_cert_chain(CERT, KEY)
    client_cfg = QuicConfiguration(
        is_client=True, alpn_protocols=['churn'],
        verify_mode=ssl.CERT_NONE,
    )

    server = await serve(host='127.0.0.1', port=args.port,
                         configuration=server_cfg,
                         create_protocol=SinkServer)
    try:
        async with connect('127.0.0.1', args.port,
                           configuration=client_cfg) as client:
            return await _drive_churn(client, args, qh3=False,
                                        get_received=lambda: received)
    finally:
        if hasattr(server, 'close'):
            server.close()


async def _drive_churn(client, args, *, qh3: bool, get_received):
    payload = b'x' * args.payload_size
    t0 = time.perf_counter()
    for _ in range(args.n_streams):
        sid = client._quic.get_next_available_stream_id(is_unidirectional=True)
        client._quic.send_stream_data(sid, payload, end_stream=True)
        if qh3:
            client.transmit()
    # Wait for server to drain
    deadline = time.perf_counter() + 30
    while get_received() < args.n_streams and time.perf_counter() < deadline:
        await asyncio.sleep(0.01)
    elapsed = time.perf_counter() - t0
    rate = args.n_streams / elapsed
    print(f"backend: {'qh3' if qh3 else 'aiopquic'}  "
          f"streams={args.n_streams:,} payload={args.payload_size}B  "
          f"received={get_received()}/{args.n_streams}  "
          f"elapsed={elapsed:.2f}s  rate={rate:,.0f} streams/s")


def main():
    ap = argparse.ArgumentParser(description="Stream open/close churn")
    ap.add_argument('--backend', choices=['qh3', 'aiopquic'],
                    default='aiopquic')
    ap.add_argument('--n-streams', type=int, default=10000)
    ap.add_argument('--payload-size', type=int, default=100)
    ap.add_argument('--port', type=int, default=47444)
    args = ap.parse_args()

    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        print(f"ERROR: cert/key not found at {CERT} / {KEY}", file=sys.stderr)
        sys.exit(1)

    if args.backend == 'qh3':
        asyncio.run(_qh3_run(args))
    else:
        asyncio.run(_aiopquic_run(args))


if __name__ == '__main__':
    main()
