"""Monthly reporting (not used by the controller)."""


def monthly_total(rows: list) -> int:
    """Sum the amount column of report rows."""
    return sum(r["amount"] for r in rows)
