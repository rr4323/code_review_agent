"""Serialization component that violates FW-001, LANG-001, and PERF-NPLUS1."""

from sample_target_repo.data_db import execute_raw_query


def serialize_order_items(items, db_conn):
    # Violates LANG-001 (missing type annotations on public function)
    # Violates FW-001 (direct database query inside serialization component)
    # Violates PERF-NPLUS1 (executes DB query inside a loop)
    serialized = []
    for item in items:
        discount = execute_raw_query(db_conn, item["coupon_code"])
        serialized.append({"sku": item["sku"], "discount": discount})
    return serialized
