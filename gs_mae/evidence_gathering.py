"""
Workflow 1 - Evidence Gathering Agent.

1. Discover & orchestrate : read enabled categories from config.
2. Parallel category runs : one gatherer per category, run concurrently.
3. Tool execution         : every gatherer runs a real, deterministic tool:
     security     -> Bandit (PyCQA SAST); falls back to an AST SQL-format check if Bandit is absent
     performance  -> AST query-in-loop detector (follows calls into child modules that run queries)
                     + optional OpenAPI-driven live service exercise and CPU profiling
     architecture -> config-driven AST rule engine (ARCH-001 / FW-001 from gs_mae_config.yaml)
     code_quality -> AST annotation checker (LANG-001)
     testing      -> runs the repository's visible pytest suite + finds untested entry points
     correctness  -> no tool; LLM-native category (evidence = AST interface contracts)
4. Aggregation            : all items land in a UnifiedEvidenceStore, sliced per (module, category).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from gs_mae.config import enabled_categories
from gs_mae.graph_builder import ScopedGraph
from gs_mae.live_api import LiveApiRunner
from gs_mae.sandbox import SandboxConfig, run_python_sandboxed

QUERY_HINTS = (
    "execute", "query", "fetch_one", "fetch_all", "fetchone", "fetchall", "raw_sql", "cursor",
    "read_text", "read_bytes", "urlopen", "read_to_string", "readfile",
)


@dataclass
class EvidenceItem:
    module_id: str
    category: str
    rule_id: str
    severity: str
    file: str
    line: int
    message: str
    tool: str
    metrics: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


class UnifiedEvidenceStore:
    def __init__(self, categories: List[str]):
        self.categories = categories
        self.items: List[EvidenceItem] = []
        self.tool_runs: Dict[str, Dict] = {}

    def add(self, category: str, tool: str, items: List[EvidenceItem], seconds: float, note: str = "",
            details: Optional[Dict] = None) -> None:
        self.items.extend(items)
        self.tool_runs[category] = {
            "tool": tool, "items": len(items), "seconds": round(seconds, 3), "note": note,
            "details": details or {},
        }

    def slice(self, module_id: str, category: str) -> List[EvidenceItem]:
        return [i for i in self.items if i.module_id == module_id and i.category == category]


def _callee_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_query_call(call: ast.Call, hints) -> bool:
    """Token-prefix match: `conn.execute`, `r.execute_command`, `cur.fetchall` match; `send_execute_task`
    and bare `execute_from_command_line(...)` do not. Bare-name calls must equal the hint exactly."""
    name = _callee_name(call).lower()
    if not name:
        return False
    if isinstance(call.func, ast.Name):
        return name in hints
    return any(name == h or name.startswith(h + "_") for h in hints)


def _per_iteration_nodes(loop: ast.AST) -> List[ast.AST]:
    """Sub-trees evaluated on every iteration. A for-loop's iterable and a comprehension's first
    iterable run once, so calls there are not N+1."""
    if isinstance(loop, (ast.For, ast.AsyncFor)):
        return list(loop.body)
    if isinstance(loop, ast.While):
        return [loop.test, *loop.body]
    if isinstance(loop, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        parts: List[ast.AST] = [loop.key, loop.value] if isinstance(loop, ast.DictComp) else [loop.elt]
        for i, gen in enumerate(loop.generators):
            parts.extend(gen.ifs)
            if i > 0:
                parts.append(gen.iter)
        return parts
    return []


class EvidenceGatheringAgent:
    TOOLS = {
        "security": "bandit",
        "performance": "ast-query-in-loop + live API profiler",
        "architecture": "ast-rule-engine",
        "code_quality": "ast-annotation-check",
        "testing": "pytest + entry-point coverage",
        "correctness": "none (LLM-native)",
    }

    def __init__(self, cfg: Dict, graph: ScopedGraph, repo_root: Path):
        self.cfg = cfg
        self.graph = graph
        self.repo_root = Path(repo_root).resolve()
        self.path_to_module = {m.rel_path: mid for mid, m in graph.modules.items()}
        self._query_funcs = self._find_query_functions()

    # -- helpers -------------------------------------------------------------------------------
    def _find_query_functions(self) -> Dict[str, Set[str]]:
        """module_id -> names of its public functions whose body issues a DB or I/O query."""
        out: Dict[str, Set[str]] = {}
        for mid, m in self.graph.modules.items():
            names: Set[str] = set()
            if m.language in ("python", "notebook"):
                tree = ast.parse(m.source)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for sub in ast.walk(node):
                            if isinstance(sub, ast.Call) and any(h in _callee_name(sub).lower() for h in QUERY_HINTS):
                                names.add(node.name)
                                break
            else:
                lines = m.source.splitlines()
                for c in m.contracts:
                    fn_short = c.name.split(".")[-1]
                    body_slice = "\n".join(lines[max(0, c.lineno - 1): min(len(lines), c.lineno + 25)]).lower()
                    if any(h in body_slice for h in QUERY_HINTS):
                        names.add(fn_short)
            out[mid] = names
        return out

    def _child_query_names(self, mid: str) -> Set[str]:
        names: Set[str] = set()
        for c in self.graph.modules[mid].children:
            names |= self._query_funcs.get(c, set())
        return names

    # -- orchestration -------------------------------------------------------------------------
    def run(self) -> UnifiedEvidenceStore:
        cats = enabled_categories(self.cfg)
        store = UnifiedEvidenceStore(cats)
        runners = {
            "security": self._security, "performance": self._performance,
            "architecture": self._architecture, "code_quality": self._code_quality,
            "testing": self._testing, "correctness": lambda: ([], ""),
        }
        with ThreadPoolExecutor(max_workers=max(1, len(cats))) as pool:
            futs = {}
            for c in cats:
                futs[c] = (time.perf_counter(), pool.submit(runners.get(c, lambda: ([], "")) ))
            for c, (t0, fut) in futs.items():
                result = fut.result()
                items, note, details = (*result, {})[:3]
                store.add(c, self.TOOLS.get(c, "builtin"), items, time.perf_counter() - t0, note, details)
        return store

    # -- security: Bandit ----------------------------------------------------------------------
    def _security(self):
        py_files = [str(self.repo_root / m.rel_path) for m in self.graph.modules.values() if m.language == "python"]
        non_py_mods = {mid: m for mid, m in self.graph.modules.items() if m.language != "python"}
        bandit = shutil.which("bandit") or str(Path(sys.executable).parent / "bandit")
        if py_files and (Path(bandit).exists() or shutil.which("bandit")):
            proc = subprocess.run([bandit, "-q", "-f", "json", *py_files], capture_output=True, text=True)
            try:
                data = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                data = {}
            items = []
            sev_map = {"HIGH": "critical", "MEDIUM": "high", "LOW": "medium"}
            for r in data.get("results", []):
                rel = str(Path(r["filename"]).resolve().relative_to(self.repo_root))
                mid = self.path_to_module.get(rel)
                if not mid:
                    continue
                items.append(EvidenceItem(
                    module_id=mid, category="security", rule_id=f"BANDIT-{r['test_id']}",
                    severity=sev_map.get(r["issue_severity"], "medium"), file=rel, line=r["line_number"],
                    message=f"{r['test_name']}: {r['issue_text']}", tool="bandit",
                ))
            if non_py_mods:
                items.extend(self._security_fallback(non_py_mods))
            return items, ""
        return self._security_fallback(), "bandit not installed or non-Python modules; used SQL-format check"

    def _security_fallback(self, modules_subset=None) -> List[EvidenceItem]:
        items = []
        target_mods = modules_subset if modules_subset is not None else self.graph.modules
        for mid, m in target_mods.items():
            if m.language in ("python", "notebook"):
                for node in ast.walk(ast.parse(m.source)):
                    if isinstance(node, ast.JoinedStr):
                        text = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
                        if any(k in text.upper() for k in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")):
                            items.append(EvidenceItem(mid, "security", "SQL-FSTRING", "critical", m.rel_path,
                                                      node.lineno, "SQL built with an f-string", "ast-sql-format-check"))
            else:
                for lno, line in enumerate(m.source.splitlines(), start=1):
                    if re.search(r"(format!\s*\(|fmt\.Sprintf\s*\(|sprintf\s*\().*\b(SELECT|INSERT|UPDATE|DELETE)\b", line, re.I):
                        items.append(EvidenceItem(mid, "security", "SQL-FSTRING", "critical", m.rel_path,
                                                  lno, "SQL built with formatted string interpolation", "ast-sql-format-check"))
        return items

    # -- performance -----------------------------------------------------------------------------
    def _performance(self):
        items: List[EvidenceItem] = []
        for mid, m in self.graph.modules.items():
            child_q = self._child_query_names(mid)
            if m.language in ("python", "notebook"):
                tree = ast.parse(m.source)
                seen: Set[int] = set()
                for loop in ast.walk(tree):
                    if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
                        continue
                    for sub in (s for part in _per_iteration_nodes(loop) for s in ast.walk(part)):
                        if isinstance(sub, ast.Call) and sub.lineno not in seen:
                            name = _callee_name(sub)
                            direct = _is_query_call(sub, QUERY_HINTS)
                            via_child = name in child_q
                            if direct or via_child:
                                seen.add(sub.lineno)
                                how = "issues a query" if direct else "calls a child-module function that issues a query"
                                items.append(EvidenceItem(
                                    mid, "performance", "PERF-NPLUS1", "high", m.rel_path, sub.lineno,
                                    f"'{name}()' inside a loop {how} on every iteration (N+1 query pattern).",
                                    "ast-query-in-loop"))
            else:
                lines = m.source.splitlines()
                in_loop = 0
                for lno, line in enumerate(lines, start=1):
                    if re.search(r"\b(for|while)\b.*\{", line):
                        in_loop = max(in_loop, 6)
                    if in_loop > 0:
                        for qname in [*QUERY_HINTS, *child_q]:
                            if re.search(rf"\b{re.escape(qname)}\s*\(", line):
                                items.append(EvidenceItem(
                                    mid, "performance", "PERF-NPLUS1", "high", m.rel_path, lno,
                                    f"'{qname}()' inside a loop issues a query or I/O call on every iteration.",
                                    "ast-query-in-loop"))
                                break
                        in_loop -= 1
        live = LiveApiRunner(self.cfg, self.graph, self.repo_root).run()
        live_items = []
        for row in live.items:
            mod = self.graph.modules.get(row["module_id"]) or self.graph.modules.get(self.graph.root)
            file_path = mod.rel_path if mod else ""
            live_items.append(
                EvidenceItem(
                    module_id=row["module_id"], category="performance", rule_id=row["rule_id"],
                    severity=row["severity"], file=file_path,
                    line=row["line"], message=row["message"], tool=row["tool"], metrics=row["metrics"],
                )
            )
        return items + live_items, live.note, live.details

    # -- architecture ----------------------------------------------------------------------------
    def _architecture(self):
        items: List[EvidenceItem] = []
        g = self.cfg.get("guidelines", {})
        for mid, m in self.graph.modules.items():
            child_q = self._child_query_names(mid)
            if m.language in ("python", "notebook"):
                tree = ast.parse(m.source)
                for rule in g.get("architecture", []):
                    if m.layer not in rule.get("applies_to_layers", []):
                        continue
                    bad = set(rule.get("forbidden_identifiers", []))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in bad:
                            items.append(EvidenceItem(mid, "architecture", rule["id"], rule.get("severity", "high"),
                                                      m.rel_path, node.lineno,
                                                      f"{rule['rule']} (business variable '{node.id}' computed in {m.layer} layer)",
                                                      "ast-rule-engine"))
                            break
                for rule in g.get("framework", []):
                    if m.layer not in rule.get("applies_to_layers", []):
                        continue
                    subs = tuple(rule.get("forbidden_call_substrings", []))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call):
                            name = _callee_name(node)
                            direct_sql = _is_query_call(node, subs)
                            serializer_db = (m.layer == "serialization") and (name in child_q)
                            if direct_sql or serializer_db:
                                items.append(EvidenceItem(mid, "architecture", rule["id"], rule.get("severity", "high"),
                                                          m.rel_path, node.lineno,
                                                          f"{rule['rule']} ('{name}()' reaches the database from the {m.layer} layer)",
                                                          "ast-rule-engine"))
            else:
                lines = m.source.splitlines()
                for rule in g.get("architecture", []):
                    if m.layer not in rule.get("applies_to_layers", []):
                        continue
                    for ident in rule.get("forbidden_identifiers", []):
                        for lno, line in enumerate(lines, start=1):
                            if re.search(rf"\b{re.escape(ident)}\b\s*:?=", line):
                                items.append(EvidenceItem(mid, "architecture", rule["id"], rule.get("severity", "high"),
                                                          m.rel_path, lno,
                                                          f"{rule['rule']} (business variable '{ident}' computed in {m.layer} layer)",
                                                          "ast-rule-engine"))
                                break
        return items, ""

    # -- code quality ----------------------------------------------------------------------------
    def _code_quality(self):
        items: List[EvidenceItem] = []
        for rule in self.cfg.get("guidelines", {}).get("language", []):
            if rule["id"] != "LANG-001":
                continue
            for mid, m in self.graph.modules.items():
                for c in m.contracts:
                    if not c.annotated:
                        items.append(EvidenceItem(mid, "code_quality", "LANG-001", rule.get("severity", "medium"),
                                                  m.rel_path, c.lineno,
                                                  f"Public function '{c.name}' lacks full type annotations.",
                                                  "ast-annotation-check"))
        return items, ""

    # -- testing ---------------------------------------------------------------------------------
    def _testing(self):
        items: List[EvidenceItem] = []
        tests_text = "\n".join(self.graph.test_files.values())
        for mid, m in self.graph.modules.items():
            if m.layer != "presentation" and mid != self.graph.root:
                continue
            for c in m.contracts:
                if c.name.split(".")[-1] not in tests_text:
                    items.append(EvidenceItem(mid, "testing", "TEST-001", "low", m.rel_path, c.lineno,
                                              f"Entry point '{c.name}' is not referenced by any test.",
                                              "entry-point-coverage"))
        note = ""
        if self.graph.test_files:
            sbx = run_python_sandboxed(
                self.repo_root,
                ["-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                sandbox_cfg=SandboxConfig.from_config(self.cfg),
                timeout=120,
            )
            note = (sbx.stdout.strip().splitlines() or [""])[-1]
            out_txt = (sbx.stdout or "") + "\n" + (sbx.stderr or "")
            m_missing = re.search(r"ModuleNotFoundError: No module named '([^'.]+)", out_txt)
            repo_top_names = {p.stem for p in self.repo_root.iterdir()}
            if sbx.returncode == 2 and m_missing and m_missing.group(1) not in repo_top_names:
                note = f"pytest skipped (external package '{m_missing.group(1)}' not installed in scanner env)"
            elif sbx.returncode not in (0, 5):
                items.append(EvidenceItem(self.graph.root, "testing", "TEST-FAIL", "high",
                                          self.graph.modules[self.graph.root].rel_path, 1,
                                          f"Visible test suite fails: {note}", "pytest"))
        return items, note
