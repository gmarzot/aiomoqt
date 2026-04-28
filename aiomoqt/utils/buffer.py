"""Buffer + BufferReadError re-export from aiopquic.

aiomoqt's serializer/parser uses aiopquic's Cython Buffer for QUIC
varint codecs and outgoing message serialization. This module is a
stable import path; the actual implementation lives in
aiopquic._binding._buffer.
"""

from aiopquic.buffer import Buffer, BufferReadError

__all__ = ["Buffer", "BufferReadError"]
