"""Serialization helpers for product payloads."""


def product_to_dict(item: dict) -> dict:
    """Convert a repository row into a public payload."""
    return {"id": item["id"], "name": item["name"], "in_stock": item["stock"] > 0}


def serialize_products(items: list) -> list:
    """Serialize product rows that the caller already loaded."""
    return [product_to_dict(item) for item in items]
