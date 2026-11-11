"""Static pricing rules."""


def bulk_discount_pct(quantity: int) -> int:
    """Return the bulk discount in PERCENT (0-100) for a quantity."""
    return 10 if quantity >= 10 else 0
