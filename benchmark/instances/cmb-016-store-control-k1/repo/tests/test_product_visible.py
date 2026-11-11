from store import product_controller as controller
from store.schema import make_db


def test_get_existing_product():
    assert controller.handle_get_product(make_db(), 1) == {"status": 200, "name": "alpha"}
