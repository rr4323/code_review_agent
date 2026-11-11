"""Serialization helpers for ticket payloads."""
import sqlite3

from boxoffice import ticket_repository as repository


def ticket_to_dict(item: dict) -> dict:
    """Convert a repository row into a public payload."""
    return {"id": item["id"], "name": item["name"], "in_stock": item["stock"] > 0}
def serialize_tickets(conn: sqlite3.Connection, ids: list) -> list:
    """Serialize the tickets with the given ids."""
    return [ticket_to_dict(repository.get_ticket(conn, i)) for i in ids]
