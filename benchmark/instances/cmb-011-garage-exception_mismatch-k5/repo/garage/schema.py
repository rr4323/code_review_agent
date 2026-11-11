"""Test fixtures: schema + seed data (not imported by application code)."""
import sqlite3


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE part (id INTEGER PRIMARY KEY, name TEXT, price_cents INTEGER, stock INTEGER)")
    conn.execute("CREATE TABLE technician (id INTEGER PRIMARY KEY, balance INTEGER)")
    conn.execute("INSERT INTO part VALUES (1, 'alpha', 1999, 5)")
    conn.execute("INSERT INTO part VALUES (2, 'beta', 500, 0)")
    conn.execute("INSERT INTO technician VALUES (10, 100)")
    conn.execute("INSERT INTO technician VALUES (20, 0)")
    return conn
