"""Live & post-hoc parameter and telemetry monitor for every section of GS-MAE.

Usage:
  .venv/bin/python -m gs_mae.monitor --section all
  .venv/bin/python -m gs_mae.monitor --section config
  .venv/bin/python -m gs_mae.monitor --section graph   --report outputs/demo_verify/review_report.json
  .venv/bin/python -m gs_mae.monitor --section evidence --report outputs/demo_verify/review_report.json
  .venv/bin/python -m gs_mae.monitor --section llm     --report outputs/demo_verify/review_report.json
  .venv/bin/python -m gs_mae.monitor --section eval    --results benchmark/results/gemma4_e2b-it-qat
  .venv/bin/python -m gs_mae.monitor --section eval    --results benchmark/results/gemma4_e2b-it-qat --watch 10
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from benchmark.metrics import aggregate, score_result
from gs_mae.config import config_fingerprint, enabled_categories, load_config
from gs_mae.graph_grpo_trainer import GraphGRPORewardEngine
from gs_mae.sandbox import SandboxConfig


def sample_runtime_memory() -> Dict[str, Any]:
    """Read live Linux /proc telemetry for ollama + llama-server processes and system RAM."""
    procs: List[Dict[str, Any]] = []
    total_rss_mb = 0.0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if "ollama" not in comm and "llama-server" not in comm:
                continue
            rss_kb = 0
            threads = 0
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                    elif line.startswith("Threads:"):
                        threads = int(line.split()[1])
            rss_mb = round(rss_kb / 1024.0, 1)
            total_rss_mb += rss_mb
            procs.append({"pid": int(pid), "comm": comm, "rss_mb": rss_mb, "threads": threads})
        except OSError:
            continue
    mem_total_mb = mem_avail_mb = 0.0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_mb = round(int(line.split()[1]) / 1024.0, 1)
                elif line.startswith("MemAvailable:"):
                    mem_avail_mb = round(int(line.split()[1]) / 1024.0, 1)
    except OSError:
        pass
    load1, load5, load15 = os.getloadavg()
    return {
        "ollama_total_rss_mb": round(total_rss_mb, 1),
        "processes": procs,
        "system_mem_total_mb": mem_total_mb,
        "system_mem_available_mb": mem_avail_mb,
        "cpu_load_avg_1m_5m_15m": [round(load1, 2), round(load5, 2), round(load15, 2)],
    }


def monitor_config_and_runtime(cfg_path: Optional[Path] = None) -> Dict[str, Any]:
    """Collects Section 1 parameters: declarative config, model runtime, and hardware state."""
    cfg = load_config(cfg_path)
    mr = cfg["model_runtime"]
    ex = cfg["execution"]
    perf = cfg["performance"]
    endpoint = mr.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
    ollama_ps: List[Dict[str, Any]] = []
    reachable = False
    try:
        r = requests.get(f"{endpoint}/api/ps", timeout=3)
        reachable = r.ok
        if r.ok:
            for m in r.json().get("models", []):
                ollama_ps.append({
                    "name": m.get("name"),
                    "size_mb": round(m.get("size", 0) / (1024 * 1024), 1),
                    "context_length": m.get("context_length"),
                    "quantization": m.get("details", {}).get("quantization_level"),
                    "parameter_size": m.get("details", {}).get("parameter_size"),
                })
    except requests.RequestException:
        pass
    return {
        "config_fingerprint": config_fingerprint(cfg),
        "model_runtime_params": mr,
        "spawn_policy": cfg.get("spawn_policy", "evidence_gated"),
        "enabled_categories": enabled_categories(cfg),
        "performance_params": perf,
        "guideline_rules": [
            r["id"] for grp in cfg.get("guidelines", {}).values() for r in grp
        ],
        "execution_params": ex,
        "sandbox_params": SandboxConfig.from_config(cfg).to_dict(),
        "ollama_reachable": reachable,
        "ollama_loaded_models": ollama_ps,
        "hardware_telemetry": sample_runtime_memory(),
    }


def monitor_graph_parameters(report: Dict[str, Any]) -> Dict[str, Any]:
    """Collects Section 2 parameters: dependency graph, contracts, and Merkle cache."""
    g = report["graph"]
    nodes = report.get("nodes", {})
    module_rows = []
    for mid in g["modules"]:
        n = nodes.get(mid, {})
        module_rows.append({
            "module_id": mid,
            "file": n.get("rel_path", ""),
            "layer": n.get("layer", ""),
            "wave": n.get("wave", 0),
            "children_merged": n.get("children_merged", []),
            "merkle_hash": n.get("merkle_hash", ""),
            "num_contracts": len(n.get("contracts", [])),
            "may_return_none_funcs": [c["name"] for c in n.get("contracts", []) if c.get("may_return_none")],
            "raises_funcs": {c["name"]: c["raises"] for c in n.get("contracts", []) if c.get("raises")},
        })
    return {
        "root_module": report["root_module"],
        "changed_files": report["changed_files"],
        "repo_modules_total": g["repo_modules"],
        "scoped_modules_count": len(g["modules"]),
        "pruned_distractor_modules": g["repo_modules"] - len(g["modules"]),
        "edges": g["edges"],
        "waves": g["waves"],
        "cache_hits_cold": report.get("cache_hits", 0),
        "warm_rerun": report.get("warm_rerun"),
        "graph_build_seconds": report["timing_s"]["graph"],
        "modules": module_rows,
    }


def monitor_evidence_parameters(report: Dict[str, Any]) -> Dict[str, Any]:
    """Collects Section 3 parameters: Workflow 1 tool execution and rule counts."""
    by_rule: Dict[str, int] = {}
    for f in report.get("findings", []):
        if f.get("rule_id"):
            by_rule[f["rule_id"]] = by_rule.get(f["rule_id"], 0) + 1
    ev = report.get("evidence", {})
    return {
        "evidence_wall_seconds": report["timing_s"]["evidence"],
        "tool_runs_by_category": ev.get("tool_runs", {}),
        "raw_evidence_items_count": len(ev.get("items", [])),
        "findings_by_rule_id": by_rule,
    }


def monitor_llm_and_node_parameters(report: Dict[str, Any]) -> Dict[str, Any]:
    """Collects Section 4 parameters: per-node spawns/merges and per-call KV-cache prefill metrics."""
    u = report["usage"]
    calls = u.get("calls", [])
    call_rows = []
    first_prefill_by_mod: Dict[str, float] = {}
    for idx, c in enumerate(calls, 1):
        mid = c["module_id"]
        role = c["role"]
        pe_s = c["prompt_eval_s"]
        ev_s = c["eval_s"]
        ptok = c["prompt_tokens"]
        otok = c["output_tokens"]
        speedup = 1.0
        if role.startswith("category:"):
            if mid not in first_prefill_by_mod and pe_s > 0:
                first_prefill_by_mod[mid] = pe_s
            elif mid in first_prefill_by_mod and pe_s > 0:
                speedup = round(first_prefill_by_mod[mid] / pe_s, 2)
        call_rows.append({
            "call_idx": idx,
            "role": role,
            "module_id": mid,
            "prompt_tokens": ptok,
            "output_tokens": otok,
            "prompt_chars": c["prompt_chars"],
            "prompt_eval_s": round(pe_s, 2),
            "eval_s": round(ev_s, 2),
            "prefill_tok_per_s": round(ptok / pe_s, 1) if pe_s > 0 else 0.0,
            "decode_tok_per_s": round(otok / ev_s, 1) if ev_s > 0 else 0.0,
            "kv_prefix_speedup_x": speedup,
            "parse_ok": c["parse_ok"],
        })
    node_rows = []
    for w_idx, wave in enumerate(report["graph"]["waves"], 1):
        for mid in wave:
            n = report["nodes"][mid]
            node_rows.append({
                "wave": w_idx,
                "module_id": mid,
                "layer": n["layer"],
                "spawned_categories": n["spawned"],
                "skipped_categories": n["skipped"],
                "children_merged": n["children_merged"],
                "local_findings": len(n.get("local_findings", [])),
                "inherited_findings": len(n.get("inherited_findings", [])),
                "max_severity": n["max_severity"],
                "summary": n["summary"],
            })
    return {
        "system": report["system"],
        "status": report["status"],
        "llm_calls": u["llm_calls"],
        "prompt_tokens_evaluated": u["prompt_tokens_evaluated"],
        "output_tokens": u["output_tokens"],
        "max_prompt_chars": u["max_prompt_chars"],
        "llm_wall_s": u["llm_wall_s"],
        "json_parse_failures": u["json_parse_failures"],
        "node_execution_trace": node_rows,
        "per_call_telemetry": call_rows,
        "repair_and_sandbox": report.get("repair"),
    }


def monitor_eval_and_grpo(results_dir: Path) -> Dict[str, Any]:
    """Collects Section 5 parameters: benchmark progress, per-system scores, repair, and Graph-GRPO rewards."""
    files = sorted(results_dir.glob("*__*.json")) if results_dir.exists() else []
    rows = [score_result(json.loads(p.read_text())) for p in files]
    agg = aggregate(rows) if rows else {}
    engine = GraphGRPORewardEngine()
    grpo_by_system: Dict[str, List[float]] = {}
    for p in files:
        r = json.loads(p.read_text())
        gt = r["ground_truth"]
        pred_files = {f["file"] for f in r["findings"] if f["bug_class"] in {
            "none_dereference", "unit_mismatch", "exception_mismatch", "argument_order"
        }}
        gt_files = {gt["bug_file"]} if gt.get("bug_type") else set()
        pred_rules = {f["rule_id"] for f in r["findings"] if f.get("rule_id")}
        gt_rules = {tb["rule_id"] for tb in gt.get("tool_bugs", []) if tb.get("rule_id")}
        rep = r.get("repair") or {}
        bd = engine.score_trajectory(
            predicted_modules=pred_files,
            ground_truth_modules=gt_files,
            predicted_rules=pred_rules,
            ground_truth_rules=gt_rules,
            patch_passes_tests=bool(rep.get("tests_passed")) or (gt.get("bug_type") is None and not pred_files),
            perf_regression_resolved=("PERF-NPLUS1" in pred_rules or "PERF-NPLUS1" not in gt_rules),
            inter_node_message_tokens=min(256, max(64, r["usage"]["output_tokens"] // max(1, r["usage"]["llm_calls"]))),
        )
        grpo_by_system.setdefault(r["system"], []).append(bd.r_total)
    grpo_summary = {
        s: round(sum(vs) / len(vs), 4) for s, vs in sorted(grpo_by_system.items()) if vs
    }
    return {
        "results_dir": str(results_dir),
        "completed_run_files": len(files),
        "per_instance_rows": rows,
        "aggregated_metrics": agg,
        "mean_graph_grpo_reward_by_system": grpo_summary,
        "live_hardware": sample_runtime_memory(),
    }


def _print_banner(title: str) -> None:
    print(f"\n{'=' * 88}\n{title}\n{'=' * 88}")


def print_monitor_report(section: str, report_path: Path, results_dir: Path, cfg_path: Optional[Path] = None) -> None:
    if section in ("all", "config"):
        _print_banner("SECTION 1: CONFIGURATION, MODEL RUNTIME & HARDWARE TELEMETRY")
        print(json.dumps(monitor_config_and_runtime(cfg_path), indent=2))
    if report_path.exists():
        rep = json.loads(report_path.read_text())
        if section in ("all", "graph"):
            _print_banner(f"SECTION 2: MODULE DEPENDENCY GRAPH & MERKLE CACHE ({report_path})")
            print(json.dumps(monitor_graph_parameters(rep), indent=2))
        if section in ("all", "evidence"):
            _print_banner(f"SECTION 3: WORKFLOW 1 — EVIDENCE GATHERING AGENT ({report_path})")
            print(json.dumps(monitor_evidence_parameters(rep), indent=2))
        if section in ("all", "llm"):
            _print_banner(f"SECTION 4: WORKFLOW 2 — NODE SPAWN/MERGE & KV-CACHE TELEMETRY ({report_path})")
            print(json.dumps(monitor_llm_and_node_parameters(rep), indent=2))
    elif section in ("graph", "evidence", "llm"):
        print(f"Report file not found at {report_path}. Run `run_gs_mae_demo.py` first.")
    if section in ("all", "eval"):
        _print_banner(f"SECTION 5: BENCHMARK EVALUATION, REPAIR & GRAPH-GRPO ({results_dir})")
        ev = monitor_eval_and_grpo(results_dir)
        print(f"Completed result files: {ev['completed_run_files']}")
        for r in ev["per_instance_rows"]:
            print(
                f"  {r['instance_id']:<38} | {r['system']:<12} | bug={r['bug_type']:<18} "
                f"| hit_strict={str(r['hit_strict']):<5} | fp={r['semantic_fp']} "
                f"| repair_pass={str(r['repair_passed']):<5} | calls={r['llm_calls']:<2} "
                f"| tok={r['prompt_tokens']:<5} | rss={r['peak_rss_mb']} MB"
            )
        print("\nAggregated Summary by System:")
        print(json.dumps(ev["aggregated_metrics"], indent=2))
        print("\nMean Verifiable Graph-GRPO Reward (R_total) by System:")
        print(json.dumps(ev["mean_graph_grpo_reward_by_system"], indent=2))
        print("\nLive Hardware Telemetry:")
        print(json.dumps(ev["live_hardware"], indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Monitor every parameter and metric across GS-MAE sections.")
    ap.add_argument("--section", choices=["all", "config", "graph", "evidence", "llm", "eval"], default="all")
    ap.add_argument("--report", type=Path, default=Path("outputs/demo_verify/review_report.json"))
    ap.add_argument("--results", type=Path, default=Path("benchmark/results/gemma4_e2b-it-qat"))
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--watch", type=int, default=0, help="Refresh interval in seconds (0 = run once)")
    a = ap.parse_args()
    if a.watch <= 0:
        print_monitor_report(a.section, a.report, a.results, a.config)
        return
    try:
        while True:
            os.system("clear" if os.name == "posix" else "cls")
            print(f"[Live Monitor — refreshing every {a.watch}s | Ctrl+C to stop]")
            print_monitor_report(a.section, a.report, a.results, a.config)
            time.sleep(a.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
