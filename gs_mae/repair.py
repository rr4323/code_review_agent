"""Patch synthesis + sandboxed verification: LLM proposes a search/replace edit; we validate AST
syntax & security, build a real unified diff with difflib, and run the repository's tests inside
an isolated, resource-limited execution sandbox (with automatic self-correction retry on failure)."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from gs_mae import prompts as P
from gs_mae.graph_builder import ModuleNode
from gs_mae.sandbox import SandboxConfig, run_python_sandboxed, validate_patch_ast

_LINENO = re.compile(r"^\s*\d+\|\s?", re.M)


def _strip_numbers(text: str) -> str:
    return _LINENO.sub("", text)


def apply_search_replace(source: str, search: str, replace: str) -> Optional[str]:
    search, replace = _strip_numbers(search), _strip_numbers(replace)
    if not search.strip():
        return None
    # Prefer line-level match with indentation normalization so mixed leading whitespace never corrupts indent
    src_lines = source.splitlines(keepends=True)
    s_lines = [l.strip() for l in search.strip("\n").splitlines() if l.strip() or len(search.strip("\n").splitlines()) == 1]
    n = len(s_lines)
    hits = [i for i in range(len(src_lines) - n + 1) if [l.strip() for l in src_lines[i:i + n]] == s_lines]
    if len(hits) == 1:
        i = hits[0]
        indent = re.match(r"\s*", src_lines[i]).group(0)
        rep_lines = replace.strip("\n").splitlines()
        base_indent = re.match(r"\s*", rep_lines[0]).group(0) if rep_lines else ""
        fixed = [indent + (l[len(base_indent):] if l.startswith(base_indent) else l.lstrip()) + "\n" for l in rep_lines]
        return "".join(src_lines[:i] + fixed + src_lines[i + n:])
    if source.count(search) == 1:
        return source.replace(search, replace, 1)
    return None


def unified_diff(rel_path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))


def run_tests(
    repo: Path,
    test_paths: Optional[List[str]] = None,
    timeout: int = 120,
    sandbox_cfg: Optional[SandboxConfig] = None,
    file_overrides: Optional[Dict[str, str]] = None,
    extra_files: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    py_args = ["-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *(test_paths or [])]
    sbx = run_python_sandboxed(
        repo=repo,
        py_args=py_args,
        sandbox_cfg=sandbox_cfg,
        file_overrides=file_overrides,
        extra_files=extra_files,
        timeout=timeout,
    )
    return {
        "passed": sbx.passed,
        "returncode": sbx.returncode,
        "tail": sbx.tail,
        "sandbox_telemetry": sbx.telemetry,
    }


class RepairAgent:
    def __init__(self, client, cfg: Optional[Dict[str, Any]] = None, max_attempts: int = 2,
                 workspace: Optional[Any] = None):
        self.client = client
        self.sandbox_cfg = SandboxConfig.from_config(cfg)
        self.max_attempts = max_attempts
        self.workspace = workspace
        if self.workspace:
            self.workspace.log("START", f"RepairAgent initialized with max_attempts={max_attempts}")

    def propose(self, mod: ModuleNode, finding: Dict, child_msgs: List[Dict], feedback: str = "") -> Dict:
        user_prompt = P.repair_prompt(mod, finding, child_msgs, feedback=feedback)
        if self.workspace:
            self.workspace.record_prompt("repair_prompt", f"SYSTEM:\n{P.SYSTEM}\n\nUSER:\n{user_prompt}")
        out = self.client.chat_json(
            P.SYSTEM,
            user_prompt,
            P.REPAIR_SCHEMA,
            "repair",
            mod.module_id,
            max_tokens=320,
        )
        if self.workspace:
            self.workspace.record_response("repair_response", out)
        return {"search": str(out.get("search", "")), "replace": str(out.get("replace", ""))}

    def repair_and_verify(
        self,
        repo: Path,
        mod: ModuleNode,
        finding: Dict,
        child_msgs: List[Dict],
        test_paths: Optional[List[str]] = None,
        extra_tests: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Synthesizes a patch, checks AST syntax & safety, and verifies in an isolated sandbox
        (with up to `self.max_attempts` self-correction attempts if syntax or tests fail)."""
        feedback = ""
        last_result: Dict[str, Any] = {
            "module_id": mod.module_id,
            "file": mod.rel_path,
            "finding_line": finding["line"],
            "applied": False,
            "diff": "",
            "tests_passed": False,
            "test_tail": "",
            "attempts": 0,
            "ast_gate": {},
            "sandbox_telemetry": {},
        }
        for attempt in range(1, self.max_attempts + 1):
            if self.workspace:
                self.workspace.log("ATTEMPT", f"Repair attempt {attempt}/{self.max_attempts}")
            edit = self.propose(mod, finding, child_msgs, feedback=feedback)
            after = apply_search_replace(mod.source, edit["search"], edit["replace"])
            if after is None or after == mod.source:
                feedback = "Your `search` string did not match the function lines. Copy the exact defective line into `search`."
                last_result["attempts"] = attempt
                if self.workspace:
                    self.workspace.log("SEARCH_FAIL", "Search string did not match target source lines")
                continue

            ast_gate = validate_patch_ast(mod.source, after)
            diff_text = unified_diff(mod.rel_path, mod.source, after)
            last_result.update({
                "applied": True,
                "diff": diff_text,
                "attempts": attempt,
                "ast_gate": ast_gate,
            })
            if self.workspace:
                self.workspace.record_artifact("patch.diff", diff_text)
                self.workspace.record_artifact("ast_gate.json", ast_gate)

            if not ast_gate["syntax_valid"] or (self.sandbox_cfg.ast_patch_gate and not ast_gate["safe"]):
                feedback = ast_gate["error"]
                last_result["tests_passed"] = False
                last_result["test_tail"] = ast_gate["error"]
                if self.workspace:
                    self.workspace.log("AST_GATE_FAIL", f"AST gate failed: {ast_gate['error']}")
                continue

            t = run_tests(
                repo=repo,
                test_paths=test_paths,
                sandbox_cfg=self.sandbox_cfg,
                file_overrides={mod.rel_path: after},
                extra_files=extra_tests,
            )
            last_result["tests_passed"] = t["passed"]
            last_result["test_tail"] = t["tail"]
            last_result["sandbox_telemetry"] = t["sandbox_telemetry"]
            if self.workspace:
                self.workspace.record_artifact("test_run.json", {
                    "passed": t["passed"],
                    "returncode": t["returncode"],
                    "tail": t["tail"],
                })

            if t["passed"]:
                if self.workspace:
                    self.workspace.record_artifact("repair_result.json", last_result)
                    self.workspace.log("SUCCESS", "Repair patch applied and passed sandboxed verification")
                return last_result
            feedback = f"Tests failed after applying patch: {t['tail']}"
            if self.workspace:
                self.workspace.log("TEST_FAIL", f"Pytest failed: {t['tail']}")

        if self.workspace:
            self.workspace.record_artifact("repair_result.json", last_result)
        return last_result
