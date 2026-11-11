from store import product_controller as controller
from store.schema import make_db


def test_hidden_missing_product_returns_404():
    assert controller.handle_get_product(make_db(), 999) == {"status": 404}


def test_hidden_quote_is_in_dollars():
    assert controller.handle_quote(make_db(), 1, 2) == {"status": 200, "total_dollars": round(39.98 * 1.10, 2)}


def test_hidden_reserve_too_many_returns_409():
    assert controller.handle_reserve(make_db(), 1, 10) == {"status": 409}


def test_hidden_payment_moves_credit_from_payer_to_payee():
    conn = make_db()
    assert controller.handle_payment(conn, 10, 20, 30) == {"status": 200}
    balances = dict(conn.execute("SELECT id, balance FROM customer").fetchall())
    assert balances == {10: 70, 20: 30}
