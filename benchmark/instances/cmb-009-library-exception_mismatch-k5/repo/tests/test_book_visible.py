from library import book_controller as controller
from library.schema import make_db


def test_get_existing_book():
    assert controller.handle_get_book(make_db(), 1) == {"status": 200, "name": "alpha"}
