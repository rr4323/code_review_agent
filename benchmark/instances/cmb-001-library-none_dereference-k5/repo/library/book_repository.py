"""Data access for book records (sqlite3)."""
import sqlite3
from typing import Optional


def get_book(conn: sqlite3.Connection, book_id: int) -> Optional[dict]:
    """Return the book as a dict, or None when no book has this id."""
    row = conn.execute(
        "SELECT id, name, price_cents, stock FROM book WHERE id = ?", (book_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "price_cents": row[2], "stock": row[3]}


def set_stock(conn: sqlite3.Connection, book_id: int, stock: int) -> None:
    """Overwrite the stock level of a book."""
    conn.execute("UPDATE book SET stock = ? WHERE id = ?", (stock, book_id))


def get_balance(conn: sqlite3.Connection, member_id: int) -> int:
    """Return the credit balance of a member (0 if unknown)."""
    row = conn.execute("SELECT balance FROM member WHERE id = ?", (member_id,)).fetchone()
    return 0 if row is None else int(row[0])


def set_balance(conn: sqlite3.Connection, member_id: int, balance: int) -> None:
    """Overwrite the credit balance of a member."""
    conn.execute("UPDATE member SET balance = ? WHERE id = ?", (balance, member_id))
def search_books(conn: sqlite3.Connection, name_fragment: str) -> list:
    """Return ids of books whose name contains the fragment."""
    rows = conn.execute("SELECT id FROM book WHERE name LIKE ?", (f"%{name_fragment}%",)).fetchall()
    return [r[0] for r in rows]
