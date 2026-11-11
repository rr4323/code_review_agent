"""Domain pricing service module that depends on data_db."""

from sample_target_repo.data_db import execute_raw_query


def calculate_promo_adjustment(coupon_code: str, subtotal: float, db_conn) -> float:
    # Calls data_db.execute_raw_query (inherits downstream SQLi risk from data_db)
    row = execute_raw_query(db_conn, coupon_code)
    if not row:
        return subtotal
    return subtotal * 0.90
