"""Business rules for products."""
import sqlite3

from store import product_repository as repository


class OutOfStockError(Exception):
    """Raised when a reservation asks for more units than are in stock."""


def unit_price_cents(conn: sqlite3.Connection, product_id: int) -> int:
    """Return the unit price of the product in integer CENTS (e.g. 1999 means $19.99)."""
    item = repository.get_product(conn, product_id)
    if item is None:
        raise KeyError(product_id)
    return int(item["price_cents"])


def reserve_stock(conn: sqlite3.Connection, product_id: int, quantity: int) -> int:
    """Reserve `quantity` units and return the remaining stock.

    Raises OutOfStockError when quantity exceeds the available stock.
    """
    item = repository.get_product(conn, product_id)
    if item is None:
        raise KeyError(product_id)
    if quantity > item["stock"]:
        raise OutOfStockError(f"only {item['stock']} left")
    remaining = item["stock"] - quantity
    repository.set_stock(conn, product_id, remaining)
    return remaining


def transfer_credit(conn: sqlite3.Connection, from_account: int, to_account: int, amount: int) -> None:
    """Move `amount` credits FROM `from_account` TO `to_account`."""
    repository.set_balance(conn, from_account, repository.get_balance(conn, from_account) - amount)
    repository.set_balance(conn, to_account, repository.get_balance(conn, to_account) + amount)
