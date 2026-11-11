"""Input validation helpers."""


def require_positive(value: int, name: str) -> int:
    """Return value if it is > 0, otherwise raise ValueError."""
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
