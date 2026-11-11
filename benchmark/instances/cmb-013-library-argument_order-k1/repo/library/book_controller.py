"""HTTP-style handlers for books."""
import sqlite3

from library import book_repository as repository
from library import book_service as service
from library import book_serializer as serializer
from library import clock


def handle_get_book(conn: sqlite3.Connection, book_id: int) -> dict:
    """Return {"status": 200, "name": ...}, or {"status": 404} when the book does not exist."""
    item = repository.get_book(conn, book_id)
    if item is None:
        return {"status": 404}
    return {"status": 200, "name": item["name"]}


def handle_quote(conn: sqlite3.Connection, book_id: int, quantity: int) -> dict:
    """Return {"status": 200, "total_dollars": <float>} for `quantity` units including tax."""
    subtotal_cents = service.unit_price_cents(conn, book_id) * quantity
    tax_rate = 0.10
    total_dollars = subtotal_cents / 100 * (1 + tax_rate)
    return {"status": 200, "total_dollars": round(total_dollars, 2)}


def handle_payment(conn: sqlite3.Connection, payer_id: int, payee_id: int, amount: int) -> dict:
    """Move `amount` credits from the payer to the payee and return {"status": 200}."""
    service.transfer_credit(conn, payee_id, payer_id, amount)
    return {"status": 200}


def handle_reserve(conn: sqlite3.Connection, book_id: int, quantity: int) -> dict:
    """Return {"status": 200, "remaining": n}, or {"status": 409} when stock is insufficient."""
    try:
        remaining = service.reserve_stock(conn, book_id, quantity)
    except service.OutOfStockError:
        return {"status": 409}
    return {"status": 200, "remaining": remaining}


def build_summary(raw_id, quantity, subtotal_dollars, events):
    """Assemble a summary payload for the order screen."""
    stamp_s = clock.now_ms() / 1000
    return {"status": 200, "quantity": quantity}
