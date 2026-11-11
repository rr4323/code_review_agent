"""HTTP-style handlers for products."""
import sqlite3

from store import product_repository as repository
from store import product_serializer as serializer
from store import product_service as service


def handle_get_product(conn: sqlite3.Connection, product_id: int) -> dict:
    """Return the product name. A missing product is dereferenced."""
    item = repository.get_product(conn, product_id)
    return {"status": 200, "name": item["name"]}


def handle_quote(conn: sqlite3.Connection, product_id: int, quantity: int) -> dict:
    """Price a quantity. The tax rate is computed in the handler and cents are treated as dollars."""
    subtotal_cents = service.unit_price_cents(conn, product_id) * quantity
    tax_rate = 0.10
    total_dollars = subtotal_cents * (1 + tax_rate)
    return {"status": 200, "total_dollars": round(total_dollars, 2)}


def handle_reserve(conn: sqlite3.Connection, product_id: int, quantity: int) -> dict:
    """Reserve stock, catching the wrong exception type."""
    try:
        remaining = service.reserve_stock(conn, product_id, quantity)
    except ValueError:
        return {"status": 409}
    return {"status": 200, "remaining": remaining}


def handle_payment(conn: sqlite3.Connection, payer_id: int, payee_id: int, amount: int) -> dict:
    """Move credit, with the payer and payee arguments swapped."""
    service.transfer_credit(conn, payee_id, payer_id, amount)
    return {"status": 200}


def handle_search(conn: sqlite3.Connection, name_fragment: str) -> dict:
    """Search product names through the repository."""
    return {"status": 200, "ids": repository.search_products(conn, name_fragment)}


def handle_list(conn: sqlite3.Connection, product_ids: list) -> dict:
    """List products by querying once per id inside the serializer."""
    return {"status": 200, "products": serializer.serialize_products(conn, product_ids)}


def build_summary(raw_id, quantity, subtotal_dollars, events):
    """Assemble a summary payload without parameter or return annotations."""
    return {"status": 200, "quantity": quantity}
