"""Serialization helpers for part payloads."""
import sqlite3

from garage import part_repository as repository


def part_to_dict(item: dict) -> dict:
    """Convert a repository row into a public payload."""
    return {"id": item["id"], "name": item["name"], "in_stock": item["stock"] > 0}
def serialize_parts(conn: sqlite3.Connection, ids: list) -> list:
    """Serialize the parts with the given ids."""
    return [part_to_dict(repository.get_part(conn, i)) for i in ids]
