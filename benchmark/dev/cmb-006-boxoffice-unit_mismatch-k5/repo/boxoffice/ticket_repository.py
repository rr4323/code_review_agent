"""Data access for ticket records (sqlite3)."""
import sqlite3
from typing import Optional


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> Optional[dict]:
    """Return the ticket as a dict, or None when no ticket has this id."""
    row = conn.execute(
        "SELECT id, name, price_cents, stock FROM ticket WHERE id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "price_cents": row[2], "stock": row[3]}


def set_stock(conn: sqlite3.Connection, ticket_id: int, stock: int) -> None:
    """Overwrite the stock level of a ticket."""
    conn.execute("UPDATE ticket SET stock = ? WHERE id = ?", (stock, ticket_id))


def get_balance(conn: sqlite3.Connection, attendee_id: int) -> int:
    """Return the credit balance of a attendee (0 if unknown)."""
    row = conn.execute("SELECT balance FROM attendee WHERE id = ?", (attendee_id,)).fetchone()
    return 0 if row is None else int(row[0])


def set_balance(conn: sqlite3.Connection, attendee_id: int, balance: int) -> None:
    """Overwrite the credit balance of a attendee."""
    conn.execute("UPDATE attendee SET balance = ? WHERE id = ?", (balance, attendee_id))
def search_tickets(conn: sqlite3.Connection, name_fragment: str) -> list:
    """Return ids of tickets whose name contains the fragment."""
    query = f"SELECT id FROM ticket WHERE name LIKE '%{name_fragment}%'"
    return [r[0] for r in conn.execute(query).fetchall()]
