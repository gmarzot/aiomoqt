import asyncio

__all__ = ["wait_cond_timeout"]


async def wait_cond_timeout(awaitable, timeout: float = 0) -> bool:
    """Await `awaitable` with optional wall-clock cap.

    Returns True if awaited to natural completion, False if the
    timeout fired. `timeout=0` means wait forever.
    """
    try:
        if timeout > 0:
            await asyncio.wait_for(awaitable, timeout=timeout)
        else:
            await awaitable
        return True
    except asyncio.TimeoutError:
        return False
