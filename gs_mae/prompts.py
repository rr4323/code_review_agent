"""Prompt templates, AST call-to-contract matching, and JSON schemas.

Prompts are ordered [shared module prefix] + [category suffix] so that multiple category
subagents spawned on the same module node reuse the model's KV cache for the prefix.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Dict, List, Optional

from gs_mae.evidence_gathering import EvidenceItem
from gs_mae.graph_builder import ModuleNode

BUG_CLASSES = [
    "none_dereference", "unit_mismatch", "exception_mismatch", "argument_order",
    "sql_injection", "n_plus_one", "layer_violation", "missing_types", "untested", "other",
]
SEVERITIES = ["critical", "high", "medium", "low"]

RULE_TO_CLASS = {
    "BANDIT-B608": "sql_injection", "SQL-FSTRING": "sql_injection", "PERF-NPLUS1": "n_plus_one",
    "PERF-REGRESSION": "other", "ARCH-001": "layer_violation", "FW-001": "layer_violation",
    "LANG-001": "missing_types", "TEST-001": "untested", "TEST-FAIL": "other",
}

SYSTEM = (
    "You are a precise senior code reviewer. You review ONE Python module at a time. "
    "Report only concrete defects you can point to by line number. Never invent code that is not shown. "
    "Answer with JSON only."
)

FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "findings": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "bug_class": {"type": "string", "enum": BUG_CLASSES},
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "related_module": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["line", "bug_class", "severity", "explanation"],
            },
        },
    },
    "required": ["analysis", "findings"],
}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "is_real": {"type": "boolean"},
                    "fix": {"type": "string"},
                },
                "required": ["index", "is_real"],
            },
        }
    },
    "required": ["verdicts"],
}

MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "risks_for_callers": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "compound_risks": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
    },
    "required": ["summary", "risks_for_callers", "compound_risks"],
}

FLAT_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "bug_class": {"type": "string", "enum": BUG_CLASSES},
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "explanation": {"type": "string"},
                },
                "required": ["file", "line", "bug_class", "severity", "explanation"],
            },
        }
    },
    "required": ["findings"],
}

REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "search": {"type": "string"},
        "replace": {"type": "string"},
    },
    "required": ["search", "replace"],
}

TOOL_CATEGORY_TASK = (
    "Task ({cat}): Deterministic tools reported the evidence items below for this module. For each item decide "
    "whether it is a real problem in the code shown (is_real) and give a one-line fix."
)


def child_contract_block(children: List[Dict]) -> str:
    """Renders the merged evaluations & contracts received via message passing from child modules."""
    if not children:
        return "CHILD MODULES: none (leaf module)\n"
    lines = ["CHILD MODULES (contracts and reviewer notes passed up from already-reviewed dependencies):"]
    for ch in children:
        lines.append(f"- module {ch['module_id']} (layer {ch['layer']})")
        for c in ch["contracts"]:
            facts = []
            if c["may_return_none"]:
                facts.append("MAY RETURN None")
            if c["raises"]:
                facts.append("raises " + ", ".join(c["raises"]))
            fact = f" [{'; '.join(facts)}]" if facts else ""
            doc = f" -- {c['doc']}" if c["doc"] else ""
            lines.append(f"    {c['signature']}{fact}{doc}")
        if ch.get("summary"):
            lines.append(f"    reviewer summary: {ch['summary']}")
        for r in ch.get("risks_for_callers", [])[:3]:
            lines.append(f"    risk for callers: {r}")
    return "\n".join(lines) + "\n"


def _param_names_from_sig(signature: str) -> List[str]:
    try:
        tree = ast.parse(signature + ":\n    pass")
        fn = tree.body[0]
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return [a.arg for a in fn.args.args if a.arg not in ("self", "cls")]
    except SyntaxError:
        pass
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return []
    names: List[str] = []
    for raw in m.group(1).split(","):
        p = raw.strip()
        if not p or p in ("self", "&self", "&mut self", "cls"):
            continue
        if ":" in p:
            names.append(p.split(":", 1)[0].strip().lstrip("mut ").strip())
        else:
            toks = re.findall(r"[A-Za-z_]\w*", p)
            if toks:
                names.append(toks[0] if signature.startswith("func ") else toks[-1])
    return names


def _direction_token(name: str) -> Optional[str]:
    lower = name.lower()
    if any(k in lower for k in (
        "from", "src", "source", "sender", "payer", "debit", "lender", "seller",
        "origin", "input", "start", "min", "low", "expected",
    )):
        return "source"
    if any(k in lower for k in (
        "to", "dst", "dest", "target", "recipient", "receiver", "payee", "credit",
        "borrower", "buyer", "output", "end", "max", "high", "actual",
    )):
        return "dest"
    return None


_UNIT_PAIRS = [
    ("cents", "dollar", (100, 100.0, 0.01), "CENTS", "dollars", "100"),
    ("_ms", "second", (1000, 1000.0, 0.001), "MILLISECONDS", "seconds", "1000"),
    ("millis", "second", (1000, 1000.0, 0.001), "MILLISECONDS", "seconds", "1000"),
    ("bytes", "_kb", (1024, 1024.0, 1000, 1000.0), "BYTES", "kilobytes", "1024"),
    ("bytes", "_mb", (1024, 1024.0, 1048576, 1000000), "BYTES", "megabytes", "1024"),
    ("bps", "percent", (100, 100.0, 10000, 10000.0), "BASIS POINTS", "percent", "100"),
]


def extract_contract_anomalies(mod: ModuleNode, children: List[Dict]) -> List[Dict]:
    """
    Cross-checks call sites in `mod` against the `FunctionContract` messages received from
    `children` (available only when child-to-parent message passing is active).
    Returns candidate cross-module contract anomalies to be audited by the `correctness` subagent.
    """
    if not children:
        return []

    contract_by_fn: Dict[str, Dict] = {}
    for ch in children:
        for c in ch.get("contracts", []):
            fn_short = c["name"].split(".")[-1]
            contract_by_fn[fn_short] = {**c, "child_module": ch["module_id"]}

    if not contract_by_fn:
        return []

    src_lines = mod.source.splitlines()
    anomalies: List[Dict] = []

    if mod.language not in ("python", "notebook"):
        for lno, line in enumerate(src_lines, start=1):
            for fn_short, c in contract_by_fn.items():
                m = re.search(rf"\b{re.escape(fn_short)}\s*\(([^)]*)\)", line)
                if not m:
                    continue
                pos_args = [a.strip() for a in m.group(1).split(",") if a.strip()]
                params = _param_names_from_sig(c["signature"])
                lo, hi = max(1, lno - 2), min(len(src_lines), lno + 2)
                snippet = "\n".join(f"  {i}| {src_lines[i - 1]}" for i in range(lo, hi + 1))
                if len(params) == len(pos_args) and len(params) >= 2:
                    for i in range(len(params) - 1):
                        p_i, p_j = _direction_token(params[i]), _direction_token(params[i + 1])
                        a_i, a_j = _direction_token(pos_args[i]), _direction_token(pos_args[i + 1])
                        if p_i and p_j and p_i != p_j and a_i == p_j and a_j == p_i:
                            anomalies.append({
                                "candidate_class": "argument_order",
                                "suspect_line": lno,
                                "call_line": lno,
                                "caller_snippet": snippet,
                                "child_module": c["child_module"],
                                "child_signature": c["signature"],
                                "child_doc": c.get("doc", ""),
                                "hint": (
                                    f"`{fn_short}()` expects parameter order `({params[i]}, {params[i + 1]})`, "
                                    f"but line {lno} passes `({pos_args[i]}, {pos_args[i + 1]})` in reversed order."
                                ),
                            })
        return anomalies

    tree = ast.parse(mod.source)

    for fn_node in ast.walk(tree):
        if not isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start_l = max(1, fn_node.lineno)
        end_l = min(len(src_lines), getattr(fn_node, "end_lineno", fn_node.lineno + 10))
        fn_snippet = "\n".join(f"  {i}| {src_lines[i - 1]}" for i in range(start_l, end_l + 1))
        fn_source = "\n".join(src_lines[start_l - 1: end_l])

        for sub in ast.walk(fn_node):
            if not isinstance(sub, ast.Call):
                continue
            callee = (
                sub.func.id if isinstance(sub.func, ast.Name)
                else sub.func.attr if isinstance(sub.func, ast.Attribute)
                else ""
            )
            if callee not in contract_by_fn:
                continue
            c = contract_by_fn[callee]
            call_line = getattr(sub, "lineno", fn_node.lineno)

            # 1. Check nullable return dereference (may_return_none)
            if c.get("may_return_none"):
                assigned_var = None
                for stmt in ast.walk(fn_node):
                    if isinstance(stmt, ast.Assign) and stmt.value is sub and len(stmt.targets) == 1:
                        if isinstance(stmt.targets[0], ast.Name):
                            assigned_var = stmt.targets[0].id
                if assigned_var:
                    has_none_guard = False
                    deref_line = None
                    for node in ast.walk(fn_node):
                        if isinstance(node, ast.Compare):
                            expr_txt = ast.unparse(node)
                            if assigned_var in expr_txt and "None" in expr_txt:
                                has_none_guard = True
                        if isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
                            if isinstance(node.test.operand, ast.Name) and node.test.operand.id == assigned_var:
                                has_none_guard = True
                        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == assigned_var:
                            has_none_guard = True
                        if isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(node.value, ast.Name):
                            if node.value.id == assigned_var:
                                deref_line = getattr(node, "lineno", call_line)
                        elif isinstance(node, ast.Call) and any(
                            isinstance(a, ast.Name) and a.id == assigned_var for a in node.args
                        ):
                            cname = (
                                node.func.id if isinstance(node.func, ast.Name)
                                else node.func.attr if isinstance(node.func, ast.Attribute)
                                else ""
                            )
                            if cname in (
                                "len", "int", "float", "bytes", "list", "dict", "set", "tuple",
                                "sorted", "iter", "sum", "min", "max", "loads",
                                "extract_zip_bytes", "send_bytes", "sha256_bytes",
                            ):
                                deref_line = deref_line or getattr(node, "lineno", call_line)
                        elif isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.iter, ast.Name) and node.iter.id == assigned_var:
                            deref_line = deref_line or getattr(node, "lineno", call_line)
                    if deref_line and not has_none_guard:
                        anomalies.append({
                            "candidate_class": "none_dereference",
                            "suspect_line": deref_line,
                            "call_line": call_line,
                            "caller_snippet": fn_snippet,
                            "child_module": c["child_module"],
                            "child_signature": c["signature"],
                            "child_doc": c.get("doc", ""),
                            "hint": (
                                f"`{callee}()` in `{c['child_module']}` may return `None`, "
                                f"and `{assigned_var}` is dereferenced/passed at line {deref_line} without a `None` check."
                            ),
                        })

            # 2. Check exception contract mismatch (raises)
            child_raises = set(c.get("raises", []))
            if child_raises:
                for try_node in ast.walk(fn_node):
                    if isinstance(try_node, ast.Try) and any(n is sub for n in ast.walk(try_node)):
                        for handler in try_node.handlers:
                            if handler.type is not None:
                                caught_name = ast.unparse(handler.type).split(".")[-1]
                                if caught_name not in child_raises and caught_name not in ("Exception", "BaseException"):
                                    anomalies.append({
                                        "candidate_class": "exception_mismatch",
                                        "suspect_line": getattr(handler, "lineno", call_line),
                                        "call_line": call_line,
                                        "caller_snippet": fn_snippet,
                                        "child_module": c["child_module"],
                                        "child_signature": c["signature"],
                                        "child_doc": c.get("doc", ""),
                                        "hint": (
                                            f"`{callee}()` raises `{sorted(child_raises)}`, "
                                            f"but line {getattr(handler, 'lineno', call_line)} catches `{caught_name}`."
                                        ),
                                    })

            # 3. Check unit contract mismatch (cents vs dollars, ms vs seconds, bytes vs KB/MB, bps vs percent)
            sig_and_doc = (c["signature"] + " " + c.get("doc", "")).lower()
            fn_lower = fn_source.lower()
            for child_unit, caller_unit, scale_consts, u_from, u_to, scale_str in _UNIT_PAIRS:
                if child_unit in sig_and_doc and caller_unit in fn_lower:
                    has_scale = False
                    for binop in ast.walk(fn_node):
                        if isinstance(binop, ast.BinOp) and isinstance(binop.op, (ast.Div, ast.FloorDiv, ast.Mult)):
                            for side in (binop.right, binop.left):
                                if isinstance(side, ast.Constant) and side.value in scale_consts:
                                    has_scale = True
                    if not has_scale:
                        suspect_l = call_line
                        for stmt in ast.walk(fn_node):
                            if isinstance(stmt, ast.Assign):
                                for t in stmt.targets:
                                    if isinstance(t, ast.Name) and caller_unit in t.id.lower():
                                        suspect_l = getattr(stmt, "lineno", call_line)
                        anomalies.append({
                            "candidate_class": "unit_mismatch",
                            "suspect_line": suspect_l,
                            "call_line": call_line,
                            "caller_snippet": fn_snippet,
                            "child_module": c["child_module"],
                            "child_signature": c["signature"],
                            "child_doc": c.get("doc", ""),
                            "hint": (
                                f"`{callee}()` returns {u_from}, but `{u_to}` at line {suspect_l} "
                                f"is computed without scaling by {scale_str}."
                            ),
                        })
                        break

            # 4. Check positional argument order mismatch (source/dest swap)
            params = _param_names_from_sig(c["signature"])
            pos_args = [a.id if isinstance(a, ast.Name) else "" for a in sub.args]
            if len(params) == len(pos_args) and len(params) >= 2:
                for i in range(len(params) - 1):
                    p_i, p_j = _direction_token(params[i]), _direction_token(params[i + 1])
                    a_i, a_j = _direction_token(pos_args[i]), _direction_token(pos_args[i + 1])
                    if p_i and p_j and p_i != p_j and a_i == p_j and a_j == p_i:
                        anomalies.append({
                            "candidate_class": "argument_order",
                            "suspect_line": call_line,
                            "call_line": call_line,
                            "caller_snippet": fn_snippet,
                            "child_module": c["child_module"],
                            "child_signature": c["signature"],
                            "child_doc": c.get("doc", ""),
                            "hint": (
                                f"`{callee}()` expects parameter order `({params[i]}, {params[i + 1]})`, "
                                f"but line {call_line} passes `({pos_args[i]}, {pos_args[i + 1]})` in reversed order."
                            ),
                        })

    return anomalies


def compact_numbered_source(mod: ModuleNode, focus_lines: Optional[List[int]] = None, max_chars: int = 2200) -> str:
    """Return numbered source preserving exact 1-based line numbers; compact long files (>max_chars)."""
    full = mod.numbered_source()
    if len(mod.source) <= max_chars:
        return full
    raw_lines = mod.source.splitlines()
    keep: set[int] = set()
    radius = 4 if len(focus_lines or []) > 2 else 7
    for lno in (focus_lines or []):
        for k in range(max(1, lno - radius), min(len(raw_lines), lno + radius) + 1):
            keep.add(k)
    in_doc = False
    doc_delim = ""
    kept_rows: List[str] = []
    for idx, line in enumerate(raw_lines, start=1):
        s = line.strip()
        if idx in keep:
            kept_rows.append(f"{idx:>3}| {line}")
            continue
        if in_doc:
            if doc_delim in s:
                in_doc = False
            continue
        if s.startswith(('"""', "'''")):
            delim = s[:3]
            if s.count(delim) == 1:
                in_doc = True
                doc_delim = delim
            continue
        if not s or s.startswith("#"):
            continue
        if focus_lines and not s.startswith(("import ", "from ", "class ", "def ", "async def ", "@")):
            continue
        kept_rows.append(f"{idx:>3}| {line}")
    joined = "\n".join(kept_rows)
    if len(joined) > max_chars * 1.5 and not focus_lines:
        # Keep imports, class/def signatures, return/raise statements, and first 35 code lines
        sig_rows = [
            r for r in kept_rows[:35]
        ] + [
            r for r in kept_rows[35:]
            if any(tok in r for tok in ("def ", "class ", "return ", "raise ", "except "))
        ]
        return "\n".join(sig_rows[:60])
    return joined


def module_prefix(mod: ModuleNode, children: List[Dict], focus_lines: Optional[List[int]] = None) -> str:
    return (
        f"MODULE: {mod.module_id}  (file {mod.rel_path}, layer {mod.layer})\n"
        f"SOURCE (line numbers on the left):\n{compact_numbered_source(mod, focus_lines)}\n\n"
        f"{child_contract_block(children)}\n"
    )


def category_suffix(
    category: str,
    evidence: List[EvidenceItem],
    mod: Optional[ModuleNode] = None,
    children: Optional[List[Dict]] = None,
) -> str:
    if category == "correctness":
        anomalies = extract_contract_anomalies(mod, children or []) if mod else []
        if anomalies:
            blocks = []
            for idx, a in enumerate(anomalies, start=1):
                blocks.append(
                    f"Candidate #{idx} (suspect line {a['suspect_line']}, candidate bug_class `{a['candidate_class']}`):\n"
                    f"  Caller snippet:\n{a['caller_snippet']}\n"
                    f"  Child contract ({a['child_module']}): `{a['child_signature']}` -- {a['child_doc']}\n"
                    f"  Contract mismatch details: {a['hint']}"
                )
            joined = "\n\n".join(blocks)
            return (
                "Task (CORRECTNESS): Verify the cross-module contract mismatch candidate(s) below.\n"
                f"{joined}\n\n"
                "Confirm whether each candidate is a real cross-module bug in the caller snippet above. "
                "Keep `analysis` and `explanation` under 20 words each, and report confirmed bugs in `findings` "
                "using the exact `suspect_line` and `candidate_class`."
            )
        return (
            "Task (CORRECTNESS): Check whether this module misuses any function imported from another module "
            "(none_dereference, unit_mismatch, exception_mismatch, or argument_order). "
            "Keep `analysis` under 20 words. If no concrete cross-module bug is visible, return `\"findings\": []`."
        )
    ev = [{"index": i, "rule": e.rule_id, "line": e.line, "message": e.message} for i, e in enumerate(evidence)]
    return TOOL_CATEGORY_TASK.format(cat=category.upper()) + "\nEVIDENCE: " + json.dumps(ev)


def merge_prompt(mod: ModuleNode, local_findings: List[Dict], children: List[Dict]) -> str:
    lf = [{"line": f["line"], "class": f["bug_class"], "severity": f["severity"], "why": f["explanation"][:160]}
          for f in local_findings]
    ch = [{"module": c["module_id"], "summary": c.get("summary", ""), "risks": c.get("risks_for_callers", [])}
          for c in children]
    return (
        f"MODULE: {mod.module_id} (layer {mod.layer})\n"
        f"FINDINGS FROM THIS MODULE'S CATEGORY REVIEWERS: {json.dumps(lf)}\n"
        f"MERGED EVALUATIONS OF CHILD MODULES: {json.dumps(ch)}\n\n"
        "Merge these into one evaluation of this module. summary: at most 40 words. risks_for_callers: facts a "
        "module that calls this one must know. compound_risks: problems that only appear when a finding here "
        "combines with a child-module risk. Use empty lists when there is nothing to report."
    )


def flat_prompt(modules: List[ModuleNode], evidence: List[EvidenceItem]) -> str:
    parts = []
    for m in modules:
        parts.append(f"=== FILE {m.rel_path} (module {m.module_id}, layer {m.layer}) ===\n{m.numbered_source()}\n")
    ev = [{"file": e.file, "line": e.line, "rule": e.rule_id, "message": e.message} for e in evidence]
    return (
        "\n".join(parts)
        + "\nTOOL EVIDENCE: " + json.dumps(ev) + "\n\n"
        "Review the whole change above. Report real defects: tool evidence you confirm, and cross-module "
        "correctness bugs (none_dereference, unit_mismatch, exception_mismatch, argument_order) where one file "
        "misuses a function defined in another file. Give the file and line where the defect is."
    )


_REPAIR_HINTS = {
    "none_dereference": (
        "Add `if item is None:\\n        return {\"status\": 404}` immediately after fetching `item` "
        "before subscripting `item[...]`."
    ),
    "unit_mismatch": (
        "Divide `subtotal_cents` by `100` when computing `total_dollars` "
        "(e.g., `total_dollars = (subtotal_cents / 100) * TAX_MULTIPLIER`)."
    ),
    "exception_mismatch": (
        "Replace `except ValueError:` with `except service.OutOfStockError:` so the raised exception is caught."
    ),
    "argument_order": (
        "Swap the reversed positional arguments in the call on the defective line so `payer_id` comes before `payee_id` "
        "(or `borrower_id` before `lender_id`, `buyer_id` before `seller_id`, `sender_id` before `receiver_id`)."
    ),
}


def repair_prompt(mod: ModuleNode, finding: Dict, children: List[Dict], feedback: str = "") -> str:
    src_lines = mod.source.splitlines()
    line_no = max(1, min(len(src_lines), int(finding.get("line", 1))))
    lo, hi = max(1, line_no - 4), min(len(src_lines), line_no + 3)
    try:
        tree = ast.parse(mod.source)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end_l = getattr(node, "end_lineno", node.lineno + 8)
                if node.lineno <= line_no <= end_l:
                    lo, hi = node.lineno, min(len(src_lines), end_l)
                    break
    except SyntaxError:
        pass
    window = "\n".join(src_lines[lo - 1: hi])
    target_line_str = src_lines[line_no - 1]
    bug_cls = finding.get("bug_class", "")
    hint = _REPAIR_HINTS.get(bug_cls, "Fix the defective line while preserving valid Python syntax and indentation.")
    fb_block = f"\nPREVIOUS ATTEMPT FAILED: {feedback}\nFix the issue and ensure valid Python syntax.\n" if feedback else ""
    return (
        f"FILE: {mod.rel_path}\n"
        f"ENCLOSING FUNCTION (lines {lo}-{hi}, no line numbers):\n"
        f"```python\n{window}\n```\n"
        f"EXACT DEFECTIVE LINE ({line_no}):\n"
        f"```python\n{target_line_str}\n```\n\n"
        f"DEFECT ({bug_cls} at line {line_no}): {finding['explanation']}\n"
        f"REPAIR GUIDANCE: {hint}\n"
        f"{fb_block}\n"
        "Return JSON `{\"search\": ..., \"replace\": ...}` where `search` is the exact line(s) from "
        "ENCLOSING FUNCTION to replace, and `replace` is the corrected Python line(s)."
    )
