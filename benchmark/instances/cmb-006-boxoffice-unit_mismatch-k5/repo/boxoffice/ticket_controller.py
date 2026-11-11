"""HTTP-style handlers for tickets."""
import sqlite3

from boxoffice import ticket_repository as repository
from boxoffice import ticket_service as service
from boxoffice import ticket_serializer as serializer
from boxoffice import validation
from boxoffice import pricing_rules
from boxoffice import audit
from boxoffice import formatting
from boxoffice import clock

TAX_MULTIPLIER = 1.10


def handle_payment(conn: sqlite3.Connection, payer_id: int, payee_id: int, amount: int) -> dict:
    """Move `amount` credits from the payer to the payee and return {"status": 200}."""
    service.transfer_credit(conn, payer_id, payee_id, amount)
    return {"status": 200}


def handle_reserve(conn: sqlite3.Connection, ticket_id: int, quantity: int) -> dict:
    """Return {"status": 200, "remaining": n}, or {"status": 409} when stock is insufficient."""
    try:
        remaining = service.reserve_stock(conn, ticket_id, quantity)
    except service.OutOfStockError:
        return {"status": 409}
    return {"status": 200, "remaining": remaining}


def handle_get_ticket(conn: sqlite3.Connection, ticket_id: int) -> dict:
    """Return {"status": 200, "name": ...}, or {"status": 404} when the ticket does not exist."""
    item = repository.get_ticket(conn, ticket_id)
    if item is None:
        return {"status": 404}
    return {"status": 200, "name": item["name"]}


def handle_quote(conn: sqlite3.Connection, ticket_id: int, quantity: int) -> dict:
    """Return {"status": 200, "total_dollars": <float>} for `quantity` units including tax."""
    subtotal_cents = service.unit_price_cents(conn, ticket_id) * quantity
    total_dollars = subtotal_cents * TAX_MULTIPLIER
    return {"status": 200, "total_dollars": round(total_dollars, 2)}


def build_summary(raw_id: str, quantity: int, subtotal_dollars: float, events: list) -> dict:
    """Assemble a summary payload for the order screen."""
    validation.require_positive(quantity, "quantity")
    discount = subtotal_dollars * pricing_rules.bulk_discount_pct(quantity) / 100
    audit.record_event(events, "summary", {"qty": quantity})
    label = formatting.format_dollars(subtotal_dollars)
    stamp_s = clock.now_ms() / 1000
    return {"status": 200, "quantity": quantity}
