# aiomoqt - Media over QUIC Transport (MoQT)

`aiomoqt` is an implementaion of the MoQT protocol, based on `asyncio` and `qh3`.

## Overview

This package implements the [MoQT Specification](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html) (currently **draft-14**). It is desinged for general use as an MoQT client and server library, supporting both 'publish' and 'subscribe' roles.

The architecture follows the [asyncio.Protocol](https://pypi.org/project/asyncio/) design pattern, and extends the [qh3 QuicConnectionProtocol](https://pypi.org/project/qh3/) protocol.

### Featurtes

- Provides 'Async Context Manager' support for session connection management.
- Supports asynchronous and awaitable synchronous calls via an optional flag.
- High-level API for control messages with default and custom response handlers.
- Low-level API for control and data message serialization and deserialization.
- Supports H3/WT and raw QUIC connection

🚀 **Status:** Alpha

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
from aiomoqt.client import MOQTClientSession

    client = MOQTClientSession(host=127.0.0.1, port=443, endpoint='wt-endpoint')

    async with client.connect() as session:
        try:
            await session.client_session_init():
            response = await session.subscribe(
                'namespace', 
                'track_name',
                wait_response=True
            )
            # wait for session close, process data and control messages
            await session.async_closed()
        except MOQTException as e:
            session.close(e.error_code, e.reason_phrase)

asyncio.run(main())
```

The high-level control message API is used for sending MoQT control messages to a server, providing typical default values for most arguments, and flexible type handling for input arguments. Those messages which expect a response, support the blocking asyncio ```await``` call construct via an optional flag (```wait_response=True```). The synchronous call will return a response message object. Asynchronous calls will return the request message object, and will return immediately. Some response handling is provided by the default handler.

The message serialization/deserialization classes provide ```<moqt-msg-obj>.serialize()``` which returns an 'qh3' Buffer with the entire message serialized in buf.data and buf.tell() at the end of the buffer. The buffer data may be passed directly to ```session.send_control_message()```. The ```<moqt-msg-class>.deserialize()``` call returns an instance of the given class populated from the deserialized data. MoQT messages that start with a type and length, will already have had the type and length parsed/pulled provided 'qh3' buffer.

#### see aiomoqt-python/aiomoqt/examples for additional examples

## Development

To set up a development environment:

```bash
git clone https://github.com/gmarzot/aiomoqt-python.git
cd aiomoqt-python
./bootstrap_python.sh
source .venv/bin/activate
```
## Local Installation

```bash
uv pip install -e .
```

## TODO

* Flesh out more message sending and handling API's
* Support for completion call back to replace or augment default handling
* Move track data read/write API to aiomoqt.messages.track
* Support file I/O [MOQT File Format](https://datatracker.ietf.org/doc/html/draft-jennings-moq-file-00)
* Simple relay (?)
* More tests

## Contributing

Contributions are welcome! If you'd like to contribute, please:

* Fork the repository on GitHub.
* Create a new branch for your feature or bug fix.
* Submit a pull request with a clear description of your changes.

For major changes, please open an issue first to discuss your proposal.

## Resources

- [MoQT Specification](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html)
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)
- [`aiomoqt` GitHub Repository](https://github.com/gmarzot/aiomoqt-python)

---

## Acknowledgements

This project takes inspiration from, and has benefited from the great work done by the [Meta/moxygen](https://github.com/facebookexperimental/moxygen) team, and the continued efforts of the MOQ IETF WG.

