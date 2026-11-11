"""Test fixtures: schema + seed data (not imported by application code)."""
import sqlite3


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ticket (id INTEGER PRIMARY KEY, name TEXT, price_cents INTEGER, stock INTEGER)")
    conn.execute("CREATE TABLE attendee (id INTEGER PRIMARY KEY, balance INTEGER)")
    conn.execute("INSERT INTO ticket VALUES (1, 'alpha', 1999, 5)")
    conn.execute("INSERT INTO ticket VALUES (2, 'beta', 500, 0)")
    conn.execute("INSERT INTO attendee VALUES (10, 100)")
    conn.execute("INSERT INTO attendee VALUES (20, 0)")
    return conn
