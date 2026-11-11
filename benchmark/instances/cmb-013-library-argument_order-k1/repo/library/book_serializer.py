"""Serialization helpers for book payloads."""
import sqlite3

from library import book_repository as repository


def book_to_dict(item: dict) -> dict:
    """Convert a repository row into a public payload."""
    return {"id": item["id"], "name": item["name"], "in_stock": item["stock"] > 0}
def serialize_books(conn: sqlite3.Connection, ids: list) -> list:
    """Serialize the books with the given ids."""
    return [book_to_dict(repository.get_book(conn, i)) for i in ids]
