"""Serialization helpers for ticket payloads."""
import sqlite3

from boxoffice import ticket_repository as repository


def ticket_to_dict(item: dict) -> dict:
    """Convert a repository row into a public payload."""
    return {"id": item["id"], "name": item["name"], "in_stock": item["stock"] > 0}
def serialize_tickets(items: list) -> list:
    """Serialize already-loaded ticket rows."""
    return [ticket_to_dict(i) for i in items]
