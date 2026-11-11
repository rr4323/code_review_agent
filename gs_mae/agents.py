"""
Workflow 2 - Evidence Summarization: multi-agent module-graph execution.

For every module node (bottom-up, wave by wave):
  1. The node agent spawns one category subagent per enabled category
     (spawn_policy=evidence_gated skips tool categories with an empty evidence slice and skips
      `correctness` on leaf modules, since it reviews calls into child modules).
  2. Each subagent sees [module source + merged child evaluations] (shared prefix) + its own
     (module, category) evidence slice, and returns JSON findings / verdicts.
  3. Intra-node merge   : category outputs are combined into the node's local findings.
  4. Inter-node merge   : an LLM merge call fuses local findings with the children's merged
     evaluations and emits a compact message (summary, risks_for_callers, compound_risks, AST
     contracts) that is passed to every parent node.
  5. The root (changed) module's merged evaluation is the final review.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from gs_mae import prompts as P
from gs_mae.config import config_fingerprint, enabled_categories
from gs_mae.evidence_gathering import EvidenceItem, UnifiedEvidenceStore
from gs_mae.graph_builder import MerkleGraphCache, ModuleNode, ScopedGraph
from gs_mae.workspace import AgentWorkspace, WorkspaceManager

SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "none": 0}
TOOL_CATEGORIES = {"performance", "security", "code_quality", "architecture", "testing"}


def _safe_id(module_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", module_id)


def _finding(module: ModuleNode, line: int, bug_class: str, severity: str, explanation: str,
             source: str, category: str, rule_id: str = "", fix: str = "", related: str = "") -> Dict:
    return {
        "module_id": module.module_id, "file": module.rel_path, "line": int(line or 0),
        "bug_class": bug_class if bug_class in P.BUG_CLASSES else "other",
        "severity": severity if severity in SEV_RANK else "medium",
        "category": category, "rule_id": rule_id, "explanation": (explanation or "")[:400],
        "fix": (fix or "")[:300], "related_module": related, "source": source,
    }


@dataclass
class NodeEvaluation:
    module_id: str
    layer: str
    merkle_hash: str
    spawned: List[str]
    skipped: List[str]
    children_merged: List[str]
    local_findings: List[Dict]
    inherited_findings: List[Dict]
    contracts: List[Dict]
    summary: str = ""
    risks_for_callers: List[str] = field(default_factory=list)
    compound_risks: List[str] = field(default_factory=list)
    max_severity: str = "none"

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_message(self) -> Dict:
        """Compact message sent to parent nodes (no raw source code)."""
        return {"module_id": self.module_id, "layer": self.layer, "contracts": self.contracts,
                "summary": self.summary, "risks_for_callers": self.risks_for_callers}

    @property
    def all_findings(self) -> List[Dict]:
        return self.local_findings + self.inherited_findings


class CategorySubagent:
    def __init__(self, client, category: str, workspace: Optional[AgentWorkspace] = None):
        self.client, self.category = client, category
        self.workspace = workspace
        if self.workspace:
            self.workspace.log("START", f"CategorySubagent ready for '{category}'")

    def run(self, mod: ModuleNode, prefix: str, evidence: List[EvidenceItem],
            child_msgs: Optional[List[Dict]] = None) -> List[Dict]:
        if self.workspace:
            self.workspace.log("PROCESS", f"Evaluating category '{self.category}' on module {mod.module_id}")

        if self.category == "testing":
            # Static test-coverage / pytest items do not require LLM re-triage against non-test source files
            findings = [
                _finding(mod, ev.line, P.RULE_TO_CLASS.get(ev.rule_id, "other"), ev.severity,
                         ev.message, "tool", self.category, ev.rule_id)
                for ev in evidence
            ]
            if self.workspace:
                self.workspace.record_artifact("findings.json", findings)
                self.workspace.log("COMPLETE", f"Produced {len(findings)} deterministic testing findings")
            return findings

        user = prefix + P.category_suffix(self.category, evidence, mod=mod, children=child_msgs)
        role = f"category:{self.category}"
        if self.workspace:
            self.workspace.record_prompt(f"prompt_{self.category}", f"SYSTEM:\n{P.SYSTEM}\n\nUSER:\n{user}")

        if self.category in TOOL_CATEGORIES:
            out = self.client.chat_json(P.SYSTEM, user, P.TRIAGE_SCHEMA, role, mod.module_id, max_tokens=256)
            if self.workspace:
                self.workspace.record_response(f"response_{self.category}", out)
            verdicts = {v.get("index"): v for v in out.get("verdicts", []) if isinstance(v, dict)}
            findings = []
            for i, ev in enumerate(evidence):
                v = verdicts.get(i, {})
                if v.get("is_real", True) is False:      # unparsed/missing verdict keeps the tool finding
                    continue
                findings.append(_finding(mod, ev.line, P.RULE_TO_CLASS.get(ev.rule_id, "other"), ev.severity,
                                         ev.message, "tool+llm", self.category, ev.rule_id, v.get("fix", "")))
            if self.workspace:
                self.workspace.record_artifact("findings.json", findings)
                self.workspace.log("COMPLETE", f"Produced {len(findings)} tool-triaged findings")
            return findings

        out = self.client.chat_json(P.SYSTEM, user, P.FINDING_SCHEMA, role, mod.module_id, max_tokens=448)
        if self.workspace:
            self.workspace.record_response(f"response_{self.category}", out)
        res = []
        for f in out.get("findings", [])[:3]:
            if not isinstance(f, dict):
                continue
            res.append(_finding(mod, f.get("line", 0), f.get("bug_class", "other"), f.get("severity", "high"),
                                f.get("explanation", ""), "llm", self.category, related=f.get("related_module", "")))
        if self.workspace:
            self.workspace.record_artifact("findings.json", res)
            self.workspace.log("COMPLETE", f"Produced {len(res)} LLM findings")
        return res


class ModuleGraphNodeAgent:
    def __init__(self, client, cfg: Dict, store: UnifiedEvidenceStore, use_child_messages: bool = True,
                 use_llm_merge: bool = True, ws_mgr: Optional[WorkspaceManager] = None):
        self.client, self.cfg, self.store = client, cfg, store
        self.use_child_messages, self.use_llm_merge = use_child_messages, use_llm_merge
        self.policy = cfg.get("spawn_policy", "evidence_gated")
        self.ws_mgr = ws_mgr

    def evaluate(self, mod: ModuleNode, children: List[NodeEvaluation]) -> NodeEvaluation:
        safe_mid = _safe_id(mod.module_id)
        ws_node = self.ws_mgr.get_or_create(f"node_{safe_mid}", role="node_agent", module_id=mod.module_id) if self.ws_mgr else None
        if ws_node:
            ws_node.log("EVALUATE", f"Evaluating module {mod.module_id} (layer={mod.layer}) with {len(children)} children")

        child_msgs = [c.to_message() for c in children] if self.use_child_messages else []
        focus_lines = sorted({
            e.line for c in enabled_categories(self.cfg) if c != "testing"
            for e in self.store.slice(mod.module_id, c) if e.line
        })
        prefix = P.module_prefix(mod, child_msgs, focus_lines=focus_lines or None)
        if ws_node:
            ws_node.record_prompt("module_prefix", prefix)

        spawned, skipped, local = [], [], []
        # correctness first is deliberate: it has no evidence suffix, so the shared prefix is evaluated once
        cats = sorted(enabled_categories(self.cfg), key=lambda c: (c != "correctness", c))
        has_llm_tool_ev = any(
            bool(self.store.slice(mod.module_id, c))
            for c in cats if c in TOOL_CATEGORIES and c != "testing"
        )
        has_code = bool(mod.contracts or mod.children or any(
            line.strip() and not line.strip().startswith("#") for line in mod.source.splitlines()
        ))
        for cat in cats:
            ev = self.store.slice(mod.module_id, cat)
            if self.policy == "evidence_gated":
                if cat in TOOL_CATEGORIES and not ev:
                    skipped.append(cat)
                    continue
                if cat == "correctness":
                    if not mod.children and (has_llm_tool_ev or not has_code):
                        skipped.append(cat)
                        continue
                    if (
                        mod.children
                        and self.use_child_messages
                        and not P.extract_contract_anomalies(mod, child_msgs)
                        and has_llm_tool_ev
                    ):
                        skipped.append(cat)
                        continue
            spawned.append(cat)
            ws_sub = self.ws_mgr.get_or_create(f"subagent_{safe_mid}_{cat}", role="category_subagent",
                                               module_id=mod.module_id, category=cat) if self.ws_mgr else None
            local.extend(CategorySubagent(self.client, cat, workspace=ws_sub).run(mod, prefix, ev, child_msgs=child_msgs))

        inherited: List[Dict] = []
        seen = set()
        for c in children:
            for f in c.all_findings:
                key = (f["file"], f["line"], f["bug_class"])
                if key not in seen:
                    seen.add(key)
                    inherited.append(f)

        node = NodeEvaluation(
            module_id=mod.module_id, layer=mod.layer, merkle_hash=mod.merkle_hash, spawned=spawned,
            skipped=skipped, children_merged=[c.module_id for c in children], local_findings=_dedupe(local),
            inherited_findings=inherited, contracts=[c.to_dict() for c in mod.contracts],
        )
        non_trivial_findings = [f for f in (node.local_findings + node.inherited_findings) if f["bug_class"] != "untested"]
        needs_llm_merge = any(
            f["severity"] in ("critical", "high") or f["source"] == "llm"
            for f in non_trivial_findings
        )
        if self.use_llm_merge and needs_llm_merge:
            merge_user = P.merge_prompt(mod, node.local_findings, child_msgs)
            if ws_node:
                ws_node.record_prompt("merge_prompt", f"SYSTEM:\n{P.SYSTEM}\n\nUSER:\n{merge_user}")
            out = self.client.chat_json(P.SYSTEM, merge_user, P.MERGE_SCHEMA, "merge", mod.module_id, max_tokens=256)
            if ws_node:
                ws_node.record_response("merge_response", out)
            node.summary = str(out.get("summary", ""))[:400]
            node.risks_for_callers = [str(r)[:200] for r in out.get("risks_for_callers", [])][:3]
            node.compound_risks = [str(r)[:300] for r in out.get("compound_risks", [])][:2]
        elif non_trivial_findings:
            classes = ", ".join(sorted({f["bug_class"] for f in non_trivial_findings}))
            node.summary = f"{mod.module_id} ({mod.layer}): {len(non_trivial_findings)} finding(s) ({classes})."
            node.risks_for_callers = [f["explanation"][:200] for f in node.local_findings if f["bug_class"] != "untested"][:3]
        else:
            node.summary = f"{mod.module_id} ({mod.layer}): clean (0 blocking/semantic findings)."
        node.max_severity = max((f["severity"] for f in node.all_findings), key=lambda s: SEV_RANK[s], default="none")

        if ws_node:
            ws_node.record_artifact("node_evaluation.json", node.to_dict())
            ws_node.log("COMPLETE", f"Node evaluation complete for {mod.module_id} (max_severity={node.max_severity})")

        return node


def _dedupe(findings: List[Dict]) -> List[Dict]:
    out, seen = [], set()
    for f in findings:
        key = (f["file"], f["line"], f["bug_class"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


class GraphExecutor:
    """GS-MAE multi-agent graph executor in topological wave order with isolated per-agent workspaces."""

    def __init__(self, client, cfg: Dict, graph: ScopedGraph, store: UnifiedEvidenceStore,
                 cache: Optional[MerkleGraphCache] = None, use_child_messages: bool = True,
                 use_llm_merge: bool = True, workspaces_dir: Optional[Path] = None,
                 username: str = "anonymous"):
        self.client, self.cfg, self.graph, self.store = client, cfg, graph, store
        self.cache = cache
        self.use_child_messages = use_child_messages
        self.use_llm_merge = use_llm_merge
        self.workspaces_dir = Path(workspaces_dir or Path("outputs/workspaces")).resolve()
        self.username = username
        self.ws_mgr = WorkspaceManager(self.workspaces_dir, username=self.username)
        self.agent = ModuleGraphNodeAgent(client, cfg, store, use_child_messages, use_llm_merge, ws_mgr=self.ws_mgr)
        self.evals: Dict[str, NodeEvaluation] = {}
        self.cache_hits = 0
        self.workspace_manifest: Dict = {}

    def run(self) -> NodeEvaluation:
        for wave in self.graph.waves:
            for mid in wave:
                mod = self.graph.modules[mid]
                cached = self.cache.get(mid, mod.merkle_hash) if self.cache else None
                if cached:
                    self.evals[mid] = NodeEvaluation(**cached)
                    self.cache_hits += 1
                    continue
                children = [self.evals[c] for c in sorted(mod.children) if c in self.evals]
                ev = self.agent.evaluate(mod, children)
                self.evals[mid] = ev
                if self.cache:
                    self.cache.put(mid, mod.merkle_hash, ev.to_dict())
        self.workspace_manifest = self.ws_mgr.generate_manifest()
        return self.evals[self.graph.root]


class FlatReviewer:
    """Baseline: one prompt with every scoped file + all tool evidence (same model, same scope)."""

    def __init__(self, client, graph: ScopedGraph, store: UnifiedEvidenceStore):
        self.client, self.graph, self.store = client, graph, store

    def run(self) -> List[Dict]:
        mods = [self.graph.modules[m] for w in reversed(self.graph.waves) for m in w]
        out = self.client.chat_json(P.SYSTEM.replace("ONE Python module", "a multi-file change"),
                                    P.flat_prompt(mods, self.store.items), P.FLAT_SCHEMA, "flat",
                                    self.graph.root, max_tokens=640)
        by_path = {m.rel_path: m for m in mods}
        by_name = {m.rel_path.split("/")[-1]: m for m in mods}
        res = []
        for f in out.get("findings", []):
            if not isinstance(f, dict):
                continue
            path = str(f.get("file", ""))
            mod = by_path.get(path) or by_name.get(path.split("/")[-1])
            if not mod:
                continue
            res.append(_finding(mod, f.get("line", 0), f.get("bug_class", "other"), f.get("severity", "medium"),
                                f.get("explanation", ""), "llm-flat", "flat"))
        return _dedupe(res)


def tools_only(graph: ScopedGraph, store: UnifiedEvidenceStore) -> List[Dict]:
    res = []
    for e in store.items:
        mod = graph.modules.get(e.module_id)
        if mod is None:
            continue
        res.append(_finding(mod, e.line, P.RULE_TO_CLASS.get(e.rule_id, "other"), e.severity, e.message,
                            "tool", e.category, e.rule_id))
    return _dedupe(res)
