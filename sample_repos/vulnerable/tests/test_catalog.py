from store import product_controller as controller
from store.seed import make_db


def test_missing_product_returns_404():
    assert controller.handle_get_product(make_db(), 999) == {"status": 404}


def test_quote_converts_cents_to_dollars():
    assert controller.handle_quote(make_db(), 1, 2) == {"status": 200, "total_dollars": 43.98}


def test_reserve_too_many_returns_409():
    assert controller.handle_reserve(make_db(), 1, 10) == {"status": 409}


def test_payment_moves_credit_from_payer_to_payee():
    conn = make_db()
    assert controller.handle_payment(conn, 10, 20, 30) == {"status": 200}
    balances = dict(conn.execute("SELECT id, balance FROM customer").fetchall())
    assert balances == {10: 70, 20: 30}


def test_search_treats_fragment_as_data():
    conn = make_db()
    assert controller.handle_search(conn, "alpha") == {"status": 200, "ids": [1]}
    assert controller.handle_search(conn, "' OR 1=1 --") == {"status": 200, "ids": []}


def test_build_summary_keeps_quantity():
    assert controller.build_summary("1", 2, 3.0, [])["quantity"] == 2


def test_list_returns_requested_products():
    listed = controller.handle_list(make_db(), [1])
    assert listed["status"] == 200
    assert listed["products"] == [{"id": 1, "name": "alpha", "in_stock": True}]
