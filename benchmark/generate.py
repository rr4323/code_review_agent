"""
CrossMod-Bench generator.

Every instance is a small, runnable Python package with a 4-level module graph:

    <entity>_controller (presentation, CHANGED FILE)
        -> <entity>_service (domain) -> <entity>_repository (data, sqlite)
        -> <entity>_serializer (serialization) -> <entity>_repository
        -> k distractor helper modules (validation, formatting, ids, clock, audit, pricing_rules)
    schema.py / reporting.py are NOT reachable from the controller (tests scoping).

The controller has four handlers. In a "bug" instance exactly one handler misuses the contract of a
function defined in a CHILD module (ground truth fixed at generation time):

    none_dereference   : result of repository.get_<entity>() (documented to return None) used unchecked
    unit_mismatch      : service.unit_price_cents() (documented cents) treated as dollars
    exception_mismatch : service.reserve_stock() raises OutOfStockError, handler catches ValueError
    argument_order     : service.transfer_credit(from_account, to_account, ...) called with swapped args

"control" instances contain the same code with every handler correct (false-positive measurement).
Optional tool-detectable defects are injected independently: SQL built with an f-string (repository),
N+1 lookups in the serializer, business tax logic in the controller (ARCH-001), a missing annotation.

Each instance ships hidden tests (outside the repo) that fail on the buggy handler and pass on the gold
fix; the generator verifies this property before writing the instance.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Dict, List, Optional

DOMAINS = [
    {"pkg": "store", "E": "product", "A": "customer"},
    {"pkg": "library", "E": "book", "A": "member"},
    {"pkg": "boxoffice", "E": "ticket", "A": "attendee"},
    {"pkg": "garage", "E": "part", "A": "technician"},
]
BUG_TYPES = ["none_dereference", "unit_mismatch", "exception_mismatch", "argument_order"]
SEMANTIC_CLASSES = set(BUG_TYPES)


def _t(s: str, d: Dict[str, str]) -> str:
    s = textwrap.dedent(s).lstrip("\n")
    for k, v in d.items():
        s = s.replace(f"__{k}__", v)
    return s


REPOSITORY = '''
"""Data access for __E__ records (sqlite3)."""
import sqlite3
from typing import Optional


def get___E__(conn: sqlite3.Connection, __E___id: int) -> Optional[dict]:
    """Return the __E__ as a dict, or None when no __E__ has this id."""
    row = conn.execute(
        "SELECT id, name, price_cents, stock FROM __E__ WHERE id = ?", (__E___id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "price_cents": row[2], "stock": row[3]}


def set_stock(conn: sqlite3.Connection, __E___id: int, stock: int) -> None:
    """Overwrite the stock level of a __E__."""
    conn.execute("UPDATE __E__ SET stock = ? WHERE id = ?", (stock, __E___id))


def get_balance(conn: sqlite3.Connection, __A___id: int) -> int:
    """Return the credit balance of a __A__ (0 if unknown)."""
    row = conn.execute("SELECT balance FROM __A__ WHERE id = ?", (__A___id,)).fetchone()
    return 0 if row is None else int(row[0])


def set_balance(conn: sqlite3.Connection, __A___id: int, balance: int) -> None:
    """Overwrite the credit balance of a __A__."""
    conn.execute("UPDATE __A__ SET balance = ? WHERE id = ?", (balance, __A___id))
'''

REPOSITORY_SQLI = '''

def search___E__s(conn: sqlite3.Connection, name_fragment: str) -> list:
    """Return ids of __E__s whose name contains the fragment."""
    query = f"SELECT id FROM __E__ WHERE name LIKE '%{name_fragment}%'"
    return [r[0] for r in conn.execute(query).fetchall()]
'''

REPOSITORY_SEARCH_OK = '''

def search___E__s(conn: sqlite3.Connection, name_fragment: str) -> list:
    """Return ids of __E__s whose name contains the fragment."""
    rows = conn.execute("SELECT id FROM __E__ WHERE name LIKE ?", (f"%{name_fragment}%",)).fetchall()
    return [r[0] for r in rows]
'''

SERVICE = '''
"""Business rules for __E__s."""
import sqlite3

from __P__ import __E___repository as repository


class OutOfStockError(Exception):
    """Raised when a reservation asks for more units than are in stock."""


def unit_price_cents(conn: sqlite3.Connection, __E___id: int) -> int:
    """Return the unit price of the __E__ in integer CENTS (e.g. 1999 means $19.99)."""
    item = repository.get___E__(conn, __E___id)
    if item is None:
        raise KeyError(__E___id)
    return int(item["price_cents"])


def reserve_stock(conn: sqlite3.Connection, __E___id: int, quantity: int) -> int:
    """Reserve `quantity` units and return the remaining stock.

    Raises OutOfStockError when quantity exceeds the available stock.
    """
    item = repository.get___E__(conn, __E___id)
    if item is None:
        raise KeyError(__E___id)
    if quantity > item["stock"]:
        raise OutOfStockError(f"only {item['stock']} left")
    remaining = item["stock"] - quantity
    repository.set_stock(conn, __E___id, remaining)
    return remaining


def transfer_credit(conn: sqlite3.Connection, from_account: int, to_account: int, amount: int) -> None:
    """Move `amount` credits FROM `from_account` TO `to_account`."""
    repository.set_balance(conn, from_account, repository.get_balance(conn, from_account) - amount)
    repository.set_balance(conn, to_account, repository.get_balance(conn, to_account) + amount)
'''

SERIALIZER = '''
"""Serialization helpers for __E__ payloads."""
import sqlite3

from __P__ import __E___repository as repository


def __E___to_dict(item: dict) -> dict:
    """Convert a repository row into a public payload."""
    return {"id": item["id"], "name": item["name"], "in_stock": item["stock"] > 0}
'''

SERIALIZER_LIST_OK = '''

def serialize___E__s(items: list) -> list:
    """Serialize already-loaded __E__ rows."""
    return [__E___to_dict(i) for i in items]
'''

SERIALIZER_NPLUS1 = '''

def serialize___E__s(conn: sqlite3.Connection, ids: list) -> list:
    """Serialize the __E__s with the given ids."""
    return [__E___to_dict(repository.get___E__(conn, i)) for i in ids]
'''

DISTRACTORS = {
    "validation": '''
        """Input validation helpers."""


        def require_positive(value: int, name: str) -> int:
            """Return value if it is > 0, otherwise raise ValueError."""
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value
        ''',
    "formatting": '''
        """Human-readable formatting."""


        def format_dollars(amount: float) -> str:
            """Format an amount given in DOLLARS, e.g. 12.5 -> '$12.50'."""
            return f"${amount:,.2f}"
        ''',
    "ids": '''
        """Identifier parsing."""
        from typing import Optional


        def parse_id(raw: str) -> Optional[int]:
            """Return the integer id, or None when raw is not a positive integer."""
            if not raw.isdigit() or int(raw) == 0:
                return None
            return int(raw)
        ''',
    "clock": '''
        """Time helpers."""
        import time


        def now_ms() -> int:
            """Current time in MILLISECONDS since the epoch."""
            return int(time.time() * 1000)
        ''',
    "audit": '''
        """In-memory audit log."""


        def record_event(log: list, kind: str, payload: dict) -> None:
            """Append an event to the audit log."""
            log.append({"kind": kind, **payload})
        ''',
    "pricing_rules": '''
        """Static pricing rules."""


        def bulk_discount_pct(quantity: int) -> int:
            """Return the bulk discount in PERCENT (0-100) for a quantity."""
            return 10 if quantity >= 10 else 0
        ''',
}

# Correct usages of each distractor, placed in a controller helper (false-positive traps).
DISTRACTOR_USES = {
    "validation": ("validation", "    validation.require_positive(quantity, \"quantity\")\n"),
    "formatting": ("formatting", "    label = formatting.format_dollars(subtotal_dollars)\n"),
    "ids": ("ids", "    parsed = ids.parse_id(raw_id)\n    if parsed is None:\n        return {\"status\": 400}\n"),
    "clock": ("clock", "    stamp_s = clock.now_ms() / 1000\n"),
    "audit": ("audit", "    audit.record_event(events, \"summary\", {\"qty\": quantity})\n"),
    "pricing_rules": ("pricing_rules", "    discount = subtotal_dollars * pricing_rules.bulk_discount_pct(quantity) / 100\n"),
}

HANDLERS_OK = {
    "none_dereference": '''
def handle_get___E__(conn: sqlite3.Connection, __E___id: int) -> dict:
    """Return {"status": 200, "name": ...}, or {"status": 404} when the __E__ does not exist."""
    item = repository.get___E__(conn, __E___id)
    if item is None:
        return {"status": 404}
    return {"status": 200, "name": item["name"]}
''',
    "unit_mismatch": '''
def handle_quote(conn: sqlite3.Connection, __E___id: int, quantity: int) -> dict:
    """Return {"status": 200, "total_dollars": <float>} for `quantity` units including tax."""
    subtotal_cents = service.unit_price_cents(conn, __E___id) * quantity
__TAX__    total_dollars = subtotal_cents / 100 * __MULT__
    return {"status": 200, "total_dollars": round(total_dollars, 2)}
''',
    "exception_mismatch": '''
def handle_reserve(conn: sqlite3.Connection, __E___id: int, quantity: int) -> dict:
    """Return {"status": 200, "remaining": n}, or {"status": 409} when stock is insufficient."""
    try:
        remaining = service.reserve_stock(conn, __E___id, quantity)
    except service.OutOfStockError:
        return {"status": 409}
    return {"status": 200, "remaining": remaining}
''',
    "argument_order": '''
def handle_payment(conn: sqlite3.Connection, payer_id: int, payee_id: int, amount: int) -> dict:
    """Move `amount` credits from the payer to the payee and return {"status": 200}."""
    service.transfer_credit(conn, payer_id, payee_id, amount)
    return {"status": 200}
''',
}

# (exact line in the OK handler, buggy replacement)
BUG_EDITS = {
    "none_dereference": ("    if item is None:\n        return {\"status\": 404}\n", ""),
    "unit_mismatch": ("    total_dollars = subtotal_cents / 100 * __MULT__\n", "    total_dollars = subtotal_cents * __MULT__\n"),
    "exception_mismatch": ("    except service.OutOfStockError:\n", "    except ValueError:\n"),
    "argument_order": ("    service.transfer_credit(conn, payer_id, payee_id, amount)\n",
                       "    service.transfer_credit(conn, payee_id, payer_id, amount)\n"),
}
# line whose number is the ground-truth bug location (searched in the buggy controller)
BUG_ANCHOR = {
    "none_dereference": "    return {\"status\": 200, \"name\": item[\"name\"]}",
    "unit_mismatch": "    total_dollars = subtotal_cents * ",
    "exception_mismatch": "    except ValueError:",
    "argument_order": "    service.transfer_credit(conn, payee_id, payer_id, amount)",
}
CHILD_OF_BUG = {"none_dereference": "repository", "unit_mismatch": "service",
                "exception_mismatch": "service", "argument_order": "service"}

SCHEMA = '''
"""Test fixtures: schema + seed data (not imported by application code)."""
import sqlite3


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE __E__ (id INTEGER PRIMARY KEY, name TEXT, price_cents INTEGER, stock INTEGER)")
    conn.execute("CREATE TABLE __A__ (id INTEGER PRIMARY KEY, balance INTEGER)")
    conn.execute("INSERT INTO __E__ VALUES (1, 'alpha', 1999, 5)")
    conn.execute("INSERT INTO __E__ VALUES (2, 'beta', 500, 0)")
    conn.execute("INSERT INTO __A__ VALUES (10, 100)")
    conn.execute("INSERT INTO __A__ VALUES (20, 0)")
    return conn
'''

REPORTING = '''
"""Monthly reporting (not used by the controller)."""


def monthly_total(rows: list) -> int:
    """Sum the amount column of report rows."""
    return sum(r["amount"] for r in rows)
'''

VISIBLE_TEST = '''
from __P__ import __E___controller as controller
from __P__.schema import make_db


def test_get_existing___E__():
    assert controller.handle_get___E__(make_db(), 1) == {"status": 200, "name": "alpha"}
'''

HIDDEN_TEST = '''
from __P__ import __E___controller as controller
from __P__.schema import make_db


def test_hidden_missing___E___returns_404():
    assert controller.handle_get___E__(make_db(), 999) == {"status": 404}


def test_hidden_quote_is_in_dollars():
    assert controller.handle_quote(make_db(), 1, 2) == {"status": 200, "total_dollars": round(39.98 * __MULT__, 2)}


def test_hidden_reserve_too_many_returns_409():
    assert controller.handle_reserve(make_db(), 1, 10) == {"status": 409}


def test_hidden_payment_moves_credit_from_payer_to_payee():
    conn = make_db()
    assert controller.handle_payment(conn, 10, 20, 30) == {"status": 200}
    balances = dict(conn.execute("SELECT id, balance FROM __A__").fetchall())
    assert balances == {10: 70, 20: 30}
'''


def build_controller(d: Dict[str, str], order: List[str], distractors: List[str], arch_violation: bool,
                     missing_annotation: bool) -> str:
    imports = ["import sqlite3", "", f"from {d['P']} import {d['E']}_repository as repository",
               f"from {d['P']} import {d['E']}_service as service",
               f"from {d['P']} import {d['E']}_serializer as serializer"]
    for name in distractors:
        imports.append(f"from {d['P']} import {name}")
    head = f'"""HTTP-style handlers for {d["E"]}s."""\n' + "\n".join(imports) + "\n"
    if not arch_violation:
        head += "\nTAX_MULTIPLIER = 1.10\n"
    parts = [head]
    for h in order:
        code = HANDLERS_OK[h]
        if h == "unit_mismatch":
            if arch_violation:
                code = code.replace("__TAX__", "    tax_rate = 0.10\n").replace("__MULT__", "(1 + tax_rate)")
            else:
                code = code.replace("__TAX__", "").replace("__MULT__", "TAX_MULTIPLIER")
        parts.append("\n" + _t(code, {"E": d["E"], "P": d["P"]}))
    if distractors:
        sig = ("def build_summary(raw_id, quantity, subtotal_dollars, events):" if missing_annotation else
               "def build_summary(raw_id: str, quantity: int, subtotal_dollars: float, events: list) -> dict:")
        body = '    """Assemble a summary payload for the order screen."""\n'
        for name in distractors:
            body += DISTRACTOR_USES[name][1]
        body += "    return {\"status\": 200, \"quantity\": quantity}\n"
        parts.append("\n" + sig + "\n" + body)
    return "\n".join(p.rstrip("\n") + "\n" for p in parts)


def make_instance(idx: int, bug: Optional[str], domain: Dict[str, str], n_distractors: int,
                  rng: random.Random) -> Dict:
    d = {"P": domain["pkg"], "E": domain["E"], "A": domain["A"]}
    flags = {
        "sqli": rng.random() < 0.5,
        "nplus1": rng.random() < 0.5,
        "arch": rng.random() < 0.5,
        "missing_annotation": n_distractors > 0 and rng.random() < 0.5,
    }
    order = BUG_TYPES[:]
    rng.shuffle(order)
    distractors = rng.sample(sorted(DISTRACTORS), n_distractors)
    controller_ok = build_controller(d, order, distractors, flags["arch"], flags["missing_annotation"])
    mult = "(1 + tax_rate)" if flags["arch"] else "TAX_MULTIPLIER"
    controller = controller_ok
    gt_line = None
    if bug:
        old, new = BUG_EDITS[bug]
        old, new = old.replace("__MULT__", mult), new.replace("__MULT__", mult)
        assert controller.count(old) == 1, (bug, old)
        controller = controller.replace(old, new)
        anchor = BUG_ANCHOR[bug]
        gt_line = next(i for i, l in enumerate(controller.splitlines(), 1) if l.startswith(anchor))

    files = {
        f"{d['P']}/__init__.py": "",
        f"{d['P']}/{d['E']}_repository.py": _t(REPOSITORY, d) + _t(REPOSITORY_SQLI if flags["sqli"] else REPOSITORY_SEARCH_OK, d),
        f"{d['P']}/{d['E']}_service.py": _t(SERVICE, d),
        f"{d['P']}/{d['E']}_serializer.py": _t(SERIALIZER, d) + _t(SERIALIZER_NPLUS1 if flags["nplus1"] else SERIALIZER_LIST_OK, d),
        f"{d['P']}/{d['E']}_controller.py": controller,
        f"{d['P']}/schema.py": _t(SCHEMA, d),
        f"{d['P']}/reporting.py": _t(REPORTING, d),
        f"tests/test_{d['E']}_visible.py": _t(VISIBLE_TEST, d),
    }
    for name in distractors:
        files[f"{d['P']}/{name}.py"] = textwrap.dedent(DISTRACTORS[name]).lstrip("\n")
    # the controller must import the serializer so it is part of the graph
    files[f"{d['P']}/{d['E']}_controller.py"] = controller.replace(
        f"from {d['P']} import {d['E']}_service as service\n",
        f"from {d['P']} import {d['E']}_service as service\nfrom {d['P']} import {d['E']}_serializer as serializer\n", 1)
    if bug:
        gt_line += 1  # one import line added above the bug
    gold_controller = controller_ok.replace(
        f"from {d['P']} import {d['E']}_service as service\n",
        f"from {d['P']} import {d['E']}_service as service\nfrom {d['P']} import {d['E']}_serializer as serializer\n", 1)

    ctrl_path = f"{d['P']}/{d['E']}_controller.py"
    tool_bugs = []
    if flags["sqli"]:
        tool_bugs.append({"bug_class": "sql_injection", "file": f"{d['P']}/{d['E']}_repository.py"})
    if flags["nplus1"]:
        tool_bugs.append({"bug_class": "n_plus_one", "file": f"{d['P']}/{d['E']}_serializer.py"})
        tool_bugs.append({"bug_class": "layer_violation", "file": f"{d['P']}/{d['E']}_serializer.py"})
    if flags["arch"]:
        tool_bugs.append({"bug_class": "layer_violation", "file": ctrl_path})
    if flags["missing_annotation"]:
        tool_bugs.append({"bug_class": "missing_types", "file": ctrl_path})
    child_file = f"{d['P']}/{d['E']}_{CHILD_OF_BUG[bug]}.py" if bug else None
    return {
        "instance_id": f"cmb-{idx:03d}-{domain['pkg']}-{bug or 'control'}-k{n_distractors}",
        "domain": domain["pkg"], "bug_type": bug, "n_distractors": n_distractors,
        "changed_files": [ctrl_path], "bug_file": ctrl_path if bug else None, "bug_line": gt_line,
        "child_file": child_file, "tool_bugs": tool_bugs, "flags": flags, "handler_order": order,
        "files": files, "gold_controller": gold_controller,
        "hidden_test": _t(HIDDEN_TEST, d).replace("__MULT__", "1.10"),
    }


def _pytest(repo: Path, extra: Optional[str] = None) -> bool:
    if extra:
        (repo / "tests" / "test_hidden.py").write_text(extra)
    proc = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=repo,
                          capture_output=True, text=True,
                          env={"PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
    return proc.returncode == 0


def write_instance(inst: Dict, out_root: Path) -> None:
    root = out_root / inst["instance_id"]
    if root.exists():
        shutil.rmtree(root)
    repo = root / "repo"
    for rel, content in inst["files"].items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (root / "hidden_test.py").write_text(inst["hidden_test"])
    (root / "gold_controller.py").write_text(inst["gold_controller"])
    meta = {k: v for k, v in inst.items() if k not in ("files", "hidden_test", "gold_controller")}
    (root / "ground_truth.json").write_text(json.dumps(meta, indent=1))


def verify_instance(inst: Dict) -> None:
    """Visible tests pass on buggy code; hidden tests fail on buggy code (bug) and pass on gold."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        for rel, content in inst["files"].items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(content)
        assert _pytest(repo), f"{inst['instance_id']}: visible tests fail"
        hidden_ok = _pytest(repo, inst["hidden_test"])
        assert hidden_ok == (inst["bug_type"] is None), f"{inst['instance_id']}: hidden test property violated"
        (repo / inst["changed_files"][0]).write_text(inst["gold_controller"])
        assert _pytest(repo, inst["hidden_test"]), f"{inst['instance_id']}: gold fix fails hidden tests"
        if inst["bug_line"]:
            line = inst["files"][inst["changed_files"][0]].splitlines()[inst["bug_line"] - 1]
            assert line.startswith(BUG_ANCHOR[inst["bug_type"]]), (inst["instance_id"], line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("benchmark/instances"))
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--sizes", type=int, nargs="+", default=[1, 5])
    ap.add_argument("--controls-per-domain", type=int, default=2)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    insts, idx = [], 0
    for bug_i, bug in enumerate(BUG_TYPES):
        for dom_i, dom in enumerate(DOMAINS):
            size = a.sizes[(bug_i + dom_i) % len(a.sizes)]
            insts.append(make_instance(idx, bug, dom, size, rng))
            idx += 1
    for dom in DOMAINS:
        for c in range(a.controls_per_domain):
            insts.append(make_instance(idx, None, dom, a.sizes[c % len(a.sizes)], rng))
            idx += 1
    if a.out.exists():
        shutil.rmtree(a.out)
    a.out.mkdir(parents=True)
    for inst in insts:
        verify_instance(inst)
        write_instance(inst, a.out)
    print(f"wrote {len(insts)} verified instances to {a.out}")
    for i in insts:
        print(f"  {i['instance_id']:<42} bug_line={i['bug_line']} tool_bugs={len(i['tool_bugs'])}")


if __name__ == "__main__":
    main()
