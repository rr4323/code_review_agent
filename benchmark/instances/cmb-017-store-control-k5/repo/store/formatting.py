"""Human-readable formatting."""


def format_dollars(amount: float) -> str:
    """Format an amount given in DOLLARS, e.g. 12.5 -> '$12.50'."""
    return f"${amount:,.2f}"
