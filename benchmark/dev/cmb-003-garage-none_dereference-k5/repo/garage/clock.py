"""Time helpers."""
import time


def now_ms() -> int:
    """Current time in MILLISECONDS since the epoch."""
    return int(time.time() * 1000)
