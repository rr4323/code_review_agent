from garage import part_controller as controller
from garage.schema import make_db


def test_get_existing_part():
    assert controller.handle_get_part(make_db(), 1) == {"status": 200, "name": "alpha"}
