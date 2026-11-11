"""Identifier parsing."""
from typing import Optional


def parse_id(raw: str) -> Optional[int]:
    """Return the integer id, or None when raw is not a positive integer."""
    if not raw.isdigit() or int(raw) == 0:
        return None
    return int(raw)
