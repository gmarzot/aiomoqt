"""B2 — Round-trip stream latency (transport-only, no MoQT).

Loopback. Server echoes any bytes received on a uni stream. Client
sends one stream of N bytes, awaits the echo, records elapsed.
Sweeps through a target rate to characterize the latency floor +
distribution.

Args:
  --n-bytes N               echo payload size (default 64)
  --rate R                  target echoes/sec (default 1000)
  --duration S              measurement window (default 5)
  --port N                  server port (default 47443)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import ssl
import sys
import time

from aiomoqt.tests.microbench._stats import Stats


AIOPQUIC = os.environ.get(
    'AIOPQUIC_DIR', '/home/gmarzot/Projects/moq/aiopquic')
CERT = os.path.join(AIOPQUIC, 'third_party/picoquic/certs/cert.pem')
KEY = os.path.join(AIOPQUIC, 'third_party/picoquic/certs/key.pem')


async def _run(args):
    from aiopquic.quic.configuration import QuicConfiguration
    from aiopquic.asyncio.server import serve
    from aiopquic.asyncio.client import connect
    from aiopquic.quic.events import StreamDataReceived
    from aiopquic.asyncio.protocol import QuicConnectionProtocol

    class EchoServer(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                self._quic.send_stream_data(
                    event.stream_id,
                    bytes(event.data) if not isinstance(event.data, bytes)
                    else event.data,
                    end_stream=event.end_stream)

    class EchoClient(QuicConnectionProtocol):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.echoes: dict[int, asyncio.Future] = {}

        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                fut = self.echoes.get(event.stream_id)
                if fut and not fut.done():
                    fut.set_result(event.data)

    server_cfg = QuicConfiguration(is_client=False, alpn_protocols=['echo'])
    server_cfg.load_cert_chain(CERT, KEY)
    client_cfg = QuicConfiguration(
        is_client=True, alpn_protocols=['echo'],
        verify_mode=ssl.CERT_NONE,
    )

    server = await serve(
        host='127.0.0.1', port=args.port,
        configuration=server_cfg,
        create_protocol=EchoServer,
    )
    try:
        async with connect('127.0.0.1', args.port,
                           configuration=client_cfg,
                           create_protocol=EchoClient) as client:
            await _drive_rtt(client, args)
    finally:
        if hasattr(server, 'close'):
            server.close()


async def _drive_rtt(client, args):
    # Warmup: a few echoes to amortize first-stream costs
    for _ in range(20):
        sid = client._quic.get_next_available_stream_id()
        fut = asyncio.get_event_loop().create_future()
        client.echoes[sid] = fut
        client._quic.send_stream_data(sid, b'x' * args.n_bytes,
                                       end_stream=True)
        await asyncio.wait_for(fut, timeout=2.0)

    interval = 1.0 / args.rate
    stats = Stats(name='rtt')
    end = time.perf_counter() + args.duration
    sent = 0
    next_send = time.perf_counter()
    payload = b'x' * args.n_bytes
    while time.perf_counter() < end:
        now = time.perf_counter()
        if now < next_send:
            await asyncio.sleep(next_send - now)
        sid = client._quic.get_next_available_stream_id()
        fut = asyncio.get_event_loop().create_future()
        client.echoes[sid] = fut
        t0 = time.perf_counter_ns()
        client._quic.send_stream_data(sid, payload, end_stream=True)
        try:
            await asyncio.wait_for(fut, timeout=2.0)
        except asyncio.TimeoutError:
            print(f"timeout on stream {sid}; aborting")
            break
        elapsed_ms = (time.perf_counter_ns() - t0) / 1e6
        stats.record(elapsed_ms)
        sent += 1
        next_send += interval

    print(f"backend: aiopquic  "
          f"rate={args.rate}/s payload={args.n_bytes}B  "
          f"sent={sent}")
    print(f"  {stats.summary(unit='ms')}")


def main():
    ap = argparse.ArgumentParser(description="Round-trip stream latency")
    ap.add_argument('--n-bytes', type=int, default=64)
    ap.add_argument('--rate', type=int, default=1000,
                    help='target echoes/sec')
    ap.add_argument('--duration', type=float, default=5.0)
    ap.add_argument('--port', type=int, default=47443)
    args = ap.parse_args()

    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        print(f"ERROR: server cert/key not found at {CERT} / {KEY}",
              file=sys.stderr)
        sys.exit(1)

    asyncio.run(_run(args))


if __name__ == '__main__':
    main()
