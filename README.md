# aiomoqt - Media over QUIC Transport (MoQT)

`aiomoqt` is an implementation of the MoQT protocol, based on `asyncio` and `qh3`.

## Overview

This package implements the [MoQT Specification](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html) (**draft-14**). It is designed for general use as an MoQT client and server library, supporting both 'publish' and 'subscribe' roles.

The architecture follows the [asyncio.Protocol](https://docs.python.org/3/library/asyncio-protocol.html) design pattern, and extends the [qh3 QuicConnectionProtocol](https://pypi.org/project/qh3/).

### Features

- Full draft-14 protocol compliance (all message types, enums, wire formats)
- Supports H3/WebTransport and raw QUIC transports
- Async context manager support for session connection management
- Supports asynchronous and awaitable synchronous calls via an optional flag
- High-level API for control messages with default and custom response handlers
- Low-level API for control and data message serialization/deserialization
- SubgroupHeader stream variants (0x10-0x1D) with flag encoding
- ObjectDatagram variants (0x00-0x07, 0x20-0x21) with flag encoding
- Delta-encoded object IDs in subgroup streams
- Interop test client implementing the [moq-interop-runner](https://github.com/englishm/moq-interop-runner) test cases

## Interop Test Results

All 6 standard [moq-interop-runner](https://github.com/englishm/moq-interop-runner) test cases pass against multiple relay implementations:

| Relay | Transport | Tests | Result |
|-------|-----------|-------|--------|
| Cloudflare moq-rs (draft-14) | Raw QUIC | 6/6 | PASS |
| Meta moxygen (draft-14) | Raw QUIC | 6/6 | PASS |
| Meta moxygen (draft-14) | WebTransport | 6/6 | PASS |

Test cases: `setup-only`, `announce-only`, `publish-namespace-done`, `subscribe-error`, `announce-subscribe`, `subscribe-before-announce`

```bash
# Run interop tests
python -m aiomoqt.examples.moq_interop_client -r "moqt://draft-14.cloudflare.mediaoverquic.com:443" --tls-disable-verify
python -m aiomoqt.examples.moq_interop_client -r "moqt://fb.mvfst.net:9448" --tls-disable-verify
```

## Installation

Install using `pip`:

```bash
pip install aiomoqt
```

Or using `uv`:

```bash
uv pip install aiomoqt
```

## Usage

### Basic Client Example

```python
import asyncio
from aiomoqt.client import MOQTClient

async def main():
    client = MOQTClient('localhost', 4433, endpoint='moq')

    async with client.connect() as session:
        try:
            await session.client_session_init()
            response = await session.subscribe(
                'namespace',
                'track_name',
                wait_response=True
            )
            # wait for session close, process data and control messages
            await session.async_closed()
        except Exception as e:
            session.close()

asyncio.run(main())
```

The high-level control message API provides typical default values for most arguments and flexible type handling for input arguments. Messages which expect a response support blocking asyncio `await` via an optional flag (`wait_response=True`), returning the response message object. Asynchronous calls return the request message object immediately.

The message serialization/deserialization classes provide `<msg>.serialize()` which returns a `qh3.Buffer` with the entire message serialized in `buf.data`. The `<MsgClass>.deserialize()` call returns an instance populated from the deserialized data.

### Examples

See `aiomoqt/examples/` for complete working examples:

| Example | Description |
|---------|-------------|
| `pub_example.py` | Publisher client — SubgroupHeader streams or ObjectDatagrams |
| `sub_example.py` | Subscriber client — receives data from a relay |
| `join_example.py` | SUBSCRIBE + FETCH (joining mid-stream) |
| `bench_relay.py` | Combined pub/sub benchmark against a relay |
| `bench_pub.py` | High-performance publisher with configurable parameters |
| `bench_sub.py` | Subscriber with latency/jitter/loss statistics |
| `server_example.py` | WebTransport server (origin) |
| `moq_interop_client.py` | Interop test client (6 standard test cases, TAP14 output) |

## Development

To set up a development environment:

```bash
git clone https://github.com/gmarzot/aiomoqt-python.git
cd aiomoqt-python
./bootstrap_python.sh
source .venv/bin/activate
```

### Local Installation

```bash
uv pip install -e ".[test]"
```

### Running Tests

```bash
pytest aiomoqt/tests/
```

## Known Limitations

* **Raw QUIC data streams:** Data stream creation (SubgroupHeader streams, ObjectDatagrams on streams) currently requires WebTransport (`_h3`). Control plane messages work over raw QUIC, but publishers cannot send data objects in raw QUIC mode. This will be resolved with the aiopquic transport transition.

## TODO

* Raw QUIC data stream support (via aiopquic transport transition)
* Dockerize interop test client for moq-interop-runner registration
* Simple relay implementation
* Support for [MOQT File Format](https://datatracker.ietf.org/doc/html/draft-jennings-moq-file-00)

## Contributing

Contributions are welcome! If you'd like to contribute, please:

* Fork the repository on GitHub.
* Create a new branch for your feature or bug fix.
* Submit a pull request with a clear description of your changes.

For major changes, please open an issue first to discuss your proposal.

## Resources

- [MoQT Specification](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html)
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)
- [MoQ Interop Runner](https://github.com/englishm/moq-interop-runner)
- [`aiomoqt` GitHub Repository](https://github.com/gmarzot/aiomoqt-python)

---

## Acknowledgements

This project takes inspiration from, and has benefited from the great work done by the [OpenMOQ/moxygen](https://github.com/openmoq/moxygen) team, and the continued efforts of the MOQ IETF WG.
