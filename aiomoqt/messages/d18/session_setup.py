from dataclasses import dataclass
from typing import Dict, Any, Optional

from .. import MOQTMessageType, MOQTMessage, BUF_SIZE
from ...utils.buffer import Buffer
from ...utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class Setup(MOQTMessage):
    """draft-18 SETUP message (§10.3).

    Symmetric: type 0x2F00 is BOTH the control uni-stream type and the
    SETUP message type, written once at the stream head. There is no
    version array (the version is negotiated out-of-band via ALPN /
    WT-Protocol) and no parameter count — the Setup Options are
    count-less delta-coded Key-Value-Pairs (Figure 2) spanning the
    message Length. Setup Options use a namespace constant across MOQT
    versions, separate from Message Parameters.
    """
    options: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.SETUP

    def serialize(self, *, draft: int) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)
        MOQTMessage._serialize_kvp_to_end(
            payload, self.options or {}, draft=draft)
        # d18 control framing: Type is vi64 (0x2F00 -> AF 00), Length is
        # 16-bit per §10.1.
        buf.push_uint_vi64(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int,
                    buf_end: Optional[int] = None) -> 'Setup':
        """Parse SETUP. The control dispatch loop consumes the Type and
        Length first (as for d14/d16 classes), so buf is positioned at
        the Setup Options and buf_end bounds them."""
        if buf_end is None:
            buf_end = buf.capacity
        options = MOQTMessage._deserialize_kvp_to_end(
            buf, draft=draft, buf_end=buf_end)
        return cls(options=options)
