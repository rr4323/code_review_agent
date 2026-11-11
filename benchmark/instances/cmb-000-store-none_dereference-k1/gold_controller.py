"""HTTP-style handlers for products."""
import sqlite3

from store import product_repository as repository
from store import product_service as service
from store import product_serializer as serializer
from store import pricing_rules

TAX_MULTIPLIER = 1.10


def handle_reserve(conn: sqlite3.Connection, product_id: int, quantity: int) -> dict:
    """Return {"status": 200, "remaining": n}, or {"status": 409} when stock is insufficient."""
    try:
        remaining = service.reserve_stock(conn, product_id, quantity)
    except service.OutOfStockError:
        return {"status": 409}
    return {"status": 200, "remaining": remaining}


def handle_quote(conn: sqlite3.Connection, product_id: int, quantity: int) -> dict:
    """Return {"status": 200, "total_dollars": <float>} for `quantity` units including tax."""
    subtotal_cents = service.unit_price_cents(conn, product_id) * quantity
    total_dollars = subtotal_cents / 100 * TAX_MULTIPLIER
    return {"status": 200, "total_dollars": round(total_dollars, 2)}


def handle_payment(conn: sqlite3.Connection, payer_id: int, payee_id: int, amount: int) -> dict:
    """Move `amount` credits from the payer to the payee and return {"status": 200}."""
    service.transfer_credit(conn, payer_id, payee_id, amount)
    return {"status": 200}


def handle_get_product(conn: sqlite3.Connection, product_id: int) -> dict:
    """Return {"status": 200, "name": ...}, or {"status": 404} when the product does not exist."""
    item = repository.get_product(conn, product_id)
    if item is None:
        return {"status": 404}
    return {"status": 200, "name": item["name"]}


def build_summary(raw_id: str, quantity: int, subtotal_dollars: float, events: list) -> dict:
    """Assemble a summary payload for the order screen."""
    discount = subtotal_dollars * pricing_rules.bulk_discount_pct(quantity) / 100
    return {"status": 200, "quantity": quantity}
