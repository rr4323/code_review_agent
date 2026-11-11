"""HTTP-style handlers for products."""
import sqlite3

from store import product_repository as repository
from store import product_serializer as serializer
from store import product_service as service

TAX_MULTIPLIER = 1.10


def handle_get_product(conn: sqlite3.Connection, product_id: int) -> dict:
    """Return the product name, or 404 when it does not exist."""
    item = repository.get_product(conn, product_id)
    if item is None:
        return {"status": 404}
    return {"status": 200, "name": item["name"]}


def handle_quote(conn: sqlite3.Connection, product_id: int, quantity: int) -> dict:
    """Price a quantity, converting integer cents to dollars before tax."""
    subtotal_cents = service.unit_price_cents(conn, product_id) * quantity
    total_dollars = subtotal_cents / 100 * TAX_MULTIPLIER
    return {"status": 200, "total_dollars": round(total_dollars, 2)}


def handle_reserve(conn: sqlite3.Connection, product_id: int, quantity: int) -> dict:
    """Reserve stock, or return 409 when there is not enough."""
    try:
        remaining = service.reserve_stock(conn, product_id, quantity)
    except service.OutOfStockError:
        return {"status": 409}
    return {"status": 200, "remaining": remaining}


def handle_payment(conn: sqlite3.Connection, payer_id: int, payee_id: int, amount: int) -> dict:
    """Move credit from the payer to the payee."""
    service.transfer_credit(conn, payer_id, payee_id, amount)
    return {"status": 200}


def handle_search(conn: sqlite3.Connection, name_fragment: str) -> dict:
    """Search product names through a parameterized repository query."""
    return {"status": 200, "ids": repository.search_products(conn, name_fragment)}


def handle_list(conn: sqlite3.Connection, product_ids: list) -> dict:
    """List products from one repository read."""
    wanted = set(product_ids)
    rows = repository.list_products(conn)
    items = []
    for item in rows:
        if item["id"] in wanted:
            items.append(item)
    return {"status": 200, "products": serializer.serialize_products(items)}


def build_summary(raw_id: str, quantity: int, subtotal_dollars: float, events: list) -> dict:
    """Assemble a summary payload."""
    return {"status": 200, "quantity": quantity}
