"""Wait for the in-process relay to serve a PUBLISHed track."""
import asyncio

from aiomoqt.tools import moq_interop_relay as relay


async def until_published(namespace, names, timeout=5.0):
    """Wait until a SUBSCRIBE for each of `names` would find its PUBLISH.

    PublishedTrack.publish() returns before PUBLISH_OK, so a SUBSCRIBE
    from another session can reach the relay ahead of the PUBLISH."""
    ns = relay._ns_tuple(namespace)
    keys = [(ns, n.encode() if isinstance(n, str) else n) for n in names]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not all(relay._live_published(k) for k in keys):
        assert loop.time() < deadline, (
            f"relay never registered PUBLISH for {keys}")
        await asyncio.sleep(0.02)
