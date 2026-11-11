"""Data access for part records (sqlite3)."""
import sqlite3
from typing import Optional


def get_part(conn: sqlite3.Connection, part_id: int) -> Optional[dict]:
    """Return the part as a dict, or None when no part has this id."""
    row = conn.execute(
        "SELECT id, name, price_cents, stock FROM part WHERE id = ?", (part_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "price_cents": row[2], "stock": row[3]}


def set_stock(conn: sqlite3.Connection, part_id: int, stock: int) -> None:
    """Overwrite the stock level of a part."""
    conn.execute("UPDATE part SET stock = ? WHERE id = ?", (stock, part_id))


def get_balance(conn: sqlite3.Connection, technician_id: int) -> int:
    """Return the credit balance of a technician (0 if unknown)."""
    row = conn.execute("SELECT balance FROM technician WHERE id = ?", (technician_id,)).fetchone()
    return 0 if row is None else int(row[0])


def set_balance(conn: sqlite3.Connection, technician_id: int, balance: int) -> None:
    """Overwrite the credit balance of a technician."""
    conn.execute("UPDATE technician SET balance = ? WHERE id = ?", (balance, technician_id))
def search_parts(conn: sqlite3.Connection, name_fragment: str) -> list:
    """Return ids of parts whose name contains the fragment."""
    query = f"SELECT id FROM part WHERE name LIKE '%{name_fragment}%'"
    return [r[0] for r in conn.execute(query).fetchall()]
