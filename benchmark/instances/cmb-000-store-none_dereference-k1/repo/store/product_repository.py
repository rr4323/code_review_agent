"""Data access for product records (sqlite3)."""
import sqlite3
from typing import Optional


def get_product(conn: sqlite3.Connection, product_id: int) -> Optional[dict]:
    """Return the product as a dict, or None when no product has this id."""
    row = conn.execute(
        "SELECT id, name, price_cents, stock FROM product WHERE id = ?", (product_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "price_cents": row[2], "stock": row[3]}


def set_stock(conn: sqlite3.Connection, product_id: int, stock: int) -> None:
    """Overwrite the stock level of a product."""
    conn.execute("UPDATE product SET stock = ? WHERE id = ?", (stock, product_id))


def get_balance(conn: sqlite3.Connection, customer_id: int) -> int:
    """Return the credit balance of a customer (0 if unknown)."""
    row = conn.execute("SELECT balance FROM customer WHERE id = ?", (customer_id,)).fetchone()
    return 0 if row is None else int(row[0])


def set_balance(conn: sqlite3.Connection, customer_id: int, balance: int) -> None:
    """Overwrite the credit balance of a customer."""
    conn.execute("UPDATE customer SET balance = ? WHERE id = ?", (balance, customer_id))
def search_products(conn: sqlite3.Connection, name_fragment: str) -> list:
    """Return ids of products whose name contains the fragment."""
    query = f"SELECT id FROM product WHERE name LIKE '%{name_fragment}%'"
    return [r[0] for r in conn.execute(query).fetchall()]
