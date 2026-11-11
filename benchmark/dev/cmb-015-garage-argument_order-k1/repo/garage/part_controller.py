"""HTTP-style handlers for parts."""
import sqlite3

from garage import part_repository as repository
from garage import part_service as service
from garage import part_serializer as serializer
from garage import validation


def handle_get_part(conn: sqlite3.Connection, part_id: int) -> dict:
    """Return {"status": 200, "name": ...}, or {"status": 404} when the part does not exist."""
    item = repository.get_part(conn, part_id)
    if item is None:
        return {"status": 404}
    return {"status": 200, "name": item["name"]}


def handle_payment(conn: sqlite3.Connection, payer_id: int, payee_id: int, amount: int) -> dict:
    """Move `amount` credits from the payer to the payee and return {"status": 200}."""
    service.transfer_credit(conn, payee_id, payer_id, amount)
    return {"status": 200}


def handle_reserve(conn: sqlite3.Connection, part_id: int, quantity: int) -> dict:
    """Return {"status": 200, "remaining": n}, or {"status": 409} when stock is insufficient."""
    try:
        remaining = service.reserve_stock(conn, part_id, quantity)
    except service.OutOfStockError:
        return {"status": 409}
    return {"status": 200, "remaining": remaining}


def handle_quote(conn: sqlite3.Connection, part_id: int, quantity: int) -> dict:
    """Return {"status": 200, "total_dollars": <float>} for `quantity` units including tax."""
    subtotal_cents = service.unit_price_cents(conn, part_id) * quantity
    tax_rate = 0.10
    total_dollars = subtotal_cents / 100 * (1 + tax_rate)
    return {"status": 200, "total_dollars": round(total_dollars, 2)}


def build_summary(raw_id: str, quantity: int, subtotal_dollars: float, events: list) -> dict:
    """Assemble a summary payload for the order screen."""
    validation.require_positive(quantity, "quantity")
    return {"status": 200, "quantity": quantity}
