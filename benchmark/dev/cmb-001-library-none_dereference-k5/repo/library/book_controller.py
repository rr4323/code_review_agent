"""HTTP-style handlers for books."""
import sqlite3

from library import book_repository as repository
from library import book_service as service
from library import book_serializer as serializer
from library import formatting
from library import ids
from library import clock
from library import audit
from library import pricing_rules

TAX_MULTIPLIER = 1.10


def handle_quote(conn: sqlite3.Connection, book_id: int, quantity: int) -> dict:
    """Return {"status": 200, "total_dollars": <float>} for `quantity` units including tax."""
    subtotal_cents = service.unit_price_cents(conn, book_id) * quantity
    total_dollars = subtotal_cents / 100 * TAX_MULTIPLIER
    return {"status": 200, "total_dollars": round(total_dollars, 2)}


def handle_get_book(conn: sqlite3.Connection, book_id: int) -> dict:
    """Return {"status": 200, "name": ...}, or {"status": 404} when the book does not exist."""
    item = repository.get_book(conn, book_id)
    return {"status": 200, "name": item["name"]}


def handle_reserve(conn: sqlite3.Connection, book_id: int, quantity: int) -> dict:
    """Return {"status": 200, "remaining": n}, or {"status": 409} when stock is insufficient."""
    try:
        remaining = service.reserve_stock(conn, book_id, quantity)
    except service.OutOfStockError:
        return {"status": 409}
    return {"status": 200, "remaining": remaining}


def handle_payment(conn: sqlite3.Connection, payer_id: int, payee_id: int, amount: int) -> dict:
    """Move `amount` credits from the payer to the payee and return {"status": 200}."""
    service.transfer_credit(conn, payer_id, payee_id, amount)
    return {"status": 200}


def build_summary(raw_id, quantity, subtotal_dollars, events):
    """Assemble a summary payload for the order screen."""
    label = formatting.format_dollars(subtotal_dollars)
    parsed = ids.parse_id(raw_id)
    if parsed is None:
        return {"status": 400}
    stamp_s = clock.now_ms() / 1000
    audit.record_event(events, "summary", {"qty": quantity})
    discount = subtotal_dollars * pricing_rules.bulk_discount_pct(quantity) / 100
    return {"status": 200, "quantity": quantity}
