from importlib.metadata import version

__version__ = version("aiomoqt")

# Override qh3 default datagram size constant
import qh3.quic.packet_builder
qh3.quic.packet_builder.PACKET_MAX_SIZE = 1200

