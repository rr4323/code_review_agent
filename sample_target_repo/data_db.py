"""Data-layer module with raw SQL query execution."""


def execute_raw_query(db_conn, coupon_code: str):
    # Intentional OWASP-A03-SQLI vulnerability: raw f-string interpolation
    query = f"SELECT id, code, discount_pct FROM coupons WHERE code = '{coupon_code}'"
    return db_conn.execute(query)
