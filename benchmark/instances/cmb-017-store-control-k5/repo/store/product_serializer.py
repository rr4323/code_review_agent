"""Serialization helpers for product payloads."""
import sqlite3

from store import product_repository as repository


def product_to_dict(item: dict) -> dict:
    """Convert a repository row into a public payload."""
    return {"id": item["id"], "name": item["name"], "in_stock": item["stock"] > 0}
def serialize_products(conn: sqlite3.Connection, ids: list) -> list:
    """Serialize the products with the given ids."""
    return [product_to_dict(repository.get_product(conn, i)) for i in ids]
