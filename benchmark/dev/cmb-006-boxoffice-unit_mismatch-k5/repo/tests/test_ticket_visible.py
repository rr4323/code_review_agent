from boxoffice import ticket_controller as controller
from boxoffice.schema import make_db


def test_get_existing_ticket():
    assert controller.handle_get_ticket(make_db(), 1) == {"status": 200, "name": "alpha"}
