"""End-to-end GS-MAE review pipeline + report rendering."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from gs_mae.agents import SEV_RANK, FlatReviewer, GraphExecutor, _dedupe, tools_only
from gs_mae.config import config_fingerprint, enabled_categories
from gs_mae.evidence_gathering import EvidenceGatheringAgent
from gs_mae.graph_builder import MerkleGraphCache, ModuleGraphBuilder
from gs_mae.llm import make_client
from gs_mae.repair import RepairAgent

SYSTEMS = ("gsmae", "flat", "gsmae_no_msg", "tools_only")


def review(repo: Path, changed_files: List[str], cfg: Dict, client=None, system: str = "gsmae",
           cache_path: Optional[Path] = None, repair: bool = False,
           repair_tests: Optional[List[str]] = None, repair_extra_tests: Optional[Dict[str, str]] = None,
           workspaces_dir: Optional[Path] = None, user_session: Optional[Any] = None) -> Dict:
    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    if client is None and system != "tools_only":
        client = make_client(cfg)
    graph_path = Path(cache_path).with_suffix(".graph.json") if cache_path else None
    graph = ModuleGraphBuilder(repo, config_fingerprint(cfg), graph_path=graph_path).build(changed_files)
    t_graph = time.perf_counter() - t0
    store = EvidenceGatheringAgent(cfg, graph, repo).run()
    t_evidence = time.perf_counter() - t0 - t_graph
    n_calls_before = len(client.ledger.calls) if client else 0

    # Determine default workspaces directory
    if workspaces_dir is None:
        if cache_path:
            ws_root = Path(cache_path).parent / "workspaces"
        else:
            ws_root = Path("outputs/workspaces")
    else:
        ws_root = Path(workspaces_dir)

    username = user_session.username if user_session and hasattr(user_session, "username") else "anonymous"

    out: Dict = {
        "system": system, "root_module": graph.root, "roots": graph.roots, "changed_files": changed_files,
        "graph": {"modules": sorted(graph.modules), "edges": graph.edges(), "waves": graph.waves,
                  "repo_modules": graph.all_module_count},
        "evidence": {"tool_runs": store.tool_runs, "items": [e.to_dict() for e in store.items]},
        "nodes": {}, "compound_risks": [], "repair": None, "cache_hits": 0,
        "user_session": user_session.to_dict() if user_session and hasattr(user_session, "to_dict") else None,
        "workspaces": {},
    }
    if system == "tools_only":
        findings = tools_only(graph, store)
    elif system == "flat":
        findings = FlatReviewer(client, graph, store).run()
    elif system in ("gsmae", "gsmae_no_msg"):
        msg = system == "gsmae"
        ex = GraphExecutor(client, cfg, graph, store, MerkleGraphCache(cache_path) if cache_path else None,
                           use_child_messages=msg, use_llm_merge=msg, workspaces_dir=ws_root, username=username)
        root_eval = ex.run()
        out["cache_hits"] = ex.cache_hits
        out["nodes"] = {k: v.to_dict() for k, v in ex.evals.items()}
        out["workspaces"] = getattr(ex, "workspace_manifest", {})
        # without message passing, the root only knows its own findings: collect every node's local findings
        findings = (
            _dedupe([f for r in graph.roots if r in ex.evals for f in ex.evals[r].all_findings])
            if msg
            else [f for v in ex.evals.values() for f in v.local_findings]
        )
        out["compound_risks"] = sorted({r for v in ex.evals.values() for r in v.compound_risks})
        out["summary"] = root_eval.summary
        if repair:
            llm_bugs = [f for f in findings if f["source"] == "llm"]
            if llm_bugs:
                target = max(llm_bugs, key=lambda f: SEV_RANK[f["severity"]])
                mod = graph.modules[target["module_id"]]
                child_msgs = [ex.evals[c].to_message() for c in sorted(mod.children)]
                ws_repair = ex.ws_mgr.get_or_create("repair_agent", role="repair_agent", module_id=target["module_id"]) if hasattr(ex, "ws_mgr") else None
                out["repair"] = RepairAgent(client, cfg, workspace=ws_repair).repair_and_verify(
                    repo, mod, target, child_msgs, repair_tests, repair_extra_tests
                )
                out["repair"]["target_finding"] = target
                if hasattr(ex, "ws_mgr"):
                    out["workspaces"] = ex.ws_mgr.generate_manifest()
    else:
        raise ValueError(system)

    fail_on = set(cfg.get("execution", {}).get("fail_on", ["critical", "high"]))
    out["findings"] = findings
    out["status"] = "FAILED_QUALITY_GATE" if any(f["severity"] in fail_on for f in findings) else "PASSED"
    calls = client.ledger.calls[n_calls_before:] if client else []
    out["usage"] = {
        "llm_calls": len(calls),
        "prompt_tokens_evaluated": sum(c.prompt_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "prompt_chars_sent": sum(c.prompt_chars for c in calls),
        "max_prompt_chars": max((c.prompt_chars for c in calls), default=0),
        "llm_wall_s": round(sum(c.wall_s for c in calls), 2),
        "json_parse_failures": sum(1 for c in calls if not c.parse_ok),
        "calls": [c.to_dict() for c in calls],
    }
    out["timing_s"] = {"graph": round(t_graph, 3), "evidence": round(t_evidence, 3),
                       "total": round(time.perf_counter() - t0, 2)}
    out["categories"] = enabled_categories(cfg)
    return out


def render_markdown(r: Dict) -> str:
    user_line = ""
    if r.get("user_session"):
        u = r["user_session"]
        user_line = f"**User:** `{u['username']}` | **Session:** `{u['session_id']}` | "
    L = [f"# GS-MAE review: `{r['root_module']}`",
         f"{user_line}**Status:** `{r['status']}` | **System:** `{r['system']}` | **Changed:** {r['changed_files']}", "",
         "## Dependency graph (bottom-up waves)"]
    for i, w in enumerate(r["graph"]["waves"], 1):
        L.append(f"- Wave {i}: {', '.join(f'`{m}`' for m in w)}")
    L += ["", "## Evidence gathering (tools actually executed)", "| Category | Tool | Items | Seconds | Note |",
          "|---|---|---|---|---|"]
    for c, t in r["evidence"]["tool_runs"].items():
        L.append(f"| {c} | {t['tool']} | {t['items']} | {t['seconds']} | {t['note']} |")
        details = t.get("details", {})
        if details.get("status") and c == "performance":
            L.append(f"  - live API: `{details['status']}`; command: `{ ' '.join(details.get('command', [])) or 'not started' }`")
            for metric in details.get("executed_operations", []):
                L.append(
                    f"  - `{metric['endpoint']}`: median {metric['median_ms']} ms, "
                    f"p95 {metric['p95_ms']} ms, CPU {metric['cpu_s']} s, RSS {metric['rss_kb']} KiB"
                )
            for skipped in details.get("skipped_operations", []):
                L.append(f"  - skipped `{skipped['operation_id']}`: {skipped['reason']}")
            profile = details.get("profile", [])
            if profile:
                L.append(f"  - CPU profile: {profile[0]['function']} ({profile[0]['cumtime_s']} s cumulative)")
    if r["nodes"]:
        L += ["", "## Node-by-node spawn and merge trace"]
        for i, w in enumerate(r["graph"]["waves"], 1):
            for m in w:
                n = r["nodes"][m]
                L.append(f"- **`{m}`** (wave {i}, layer {n['layer']}): spawned {n['spawned']}, skipped {n['skipped']}, "
                         f"merged children {n['children_merged'] or '[]'}; local findings {len(n['local_findings'])}, "
                         f"inherited {len(n['inherited_findings'])}, max severity `{n['max_severity']}`")
                if n["summary"]:
                    L.append(f"  - summary: {n['summary']}")
                for rk in n["risks_for_callers"]:
                    L.append(f"  - risk for callers: {rk}")
                for cr in n["compound_risks"]:
                    L.append(f"  - compound risk: {cr}")
    L += ["", f"## Findings ({len(r['findings'])})", "| Severity | Class | File:line | Source | Explanation |",
          "|---|---|---|---|---|"]
    for f in sorted(r["findings"], key=lambda f: -SEV_RANK[f["severity"]]):
        L.append(f"| {f['severity']} | {f['bug_class']} | `{f['file']}:{f['line']}` | {f['source']} | "
                 f"{f['explanation'].replace('|', '/')[:220]} |")
    if r.get("repair"):
        rp = r["repair"]
        L += ["", "## Repair", f"- applied: {rp['applied']}, tests passed: {rp['tests_passed']} ({rp['test_tail']})"]
        if rp["diff"]:
            L.append(f"```diff\n{rp['diff']}```")
    if r.get("workspaces") and r["workspaces"].get("agents"):
        ws = r["workspaces"]
        L += ["", "## Multi-Agent Workspaces & Traceability",
              f"**Workspaces Root:** `{ws.get('workspaces_root')}` | **Total Agents:** {ws.get('total_agents')} | **Files Written:** {ws.get('total_files_written')}",
              "| Agent ID | Role | Module | Files Written | Total Bytes | Workspace Path |",
              "|---|---|---|---|---|---|"]
        for aid, ainfo in sorted(ws["agents"].items()):
            L.append(f"| `{aid}` | {ainfo.get('role', '-')} | `{ainfo.get('module_id', '-')}` | "
                     f"{ainfo.get('total_files_written', 0)} | {ainfo.get('total_bytes_written', 0)} | `{ainfo.get('workspace_path')}` |")
    u = r["usage"]
    L += ["", "## Usage", f"- LLM calls: {u['llm_calls']}, prompt tokens evaluated: {u['prompt_tokens_evaluated']}, "
          f"output tokens: {u['output_tokens']}, LLM wall time: {u['llm_wall_s']} s, total: {r['timing_s']['total']} s"]
    return "\n".join(L) + "\n"


def save_report(r: Dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "review_report.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
    (out_dir / "review_report.md").write_text(render_markdown(r), encoding="utf-8")
