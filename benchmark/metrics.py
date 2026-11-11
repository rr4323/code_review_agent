"""Score CrossMod-Bench results (only real model outputs are read from benchmark/results)."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

SEMANTIC = {"none_dereference", "unit_mismatch", "exception_mismatch", "argument_order"}
LINE_TOL = 3


def score_result(r: Dict) -> Dict:
    gt = r["ground_truth"]
    sem = [f for f in r["findings"] if f["bug_class"] in SEMANTIC]
    hit_strict = hit_file = False
    matched_ids = set()
    if gt["bug_type"]:
        for k, f in enumerate(sem):
            if f["bug_class"] == gt["bug_type"] and f["file"] == gt["bug_file"]:
                hit_file = True
                if abs(f["line"] - gt["bug_line"]) <= LINE_TOL:
                    hit_strict = True
                    matched_ids.add(k)
    fps = len([1 for k, f in enumerate(sem) if k not in matched_ids
               and not (gt["bug_type"] and f["bug_class"] == gt["bug_type"] and f["file"] == gt["bug_file"])])
    tool_hits = 0
    for tb in gt["tool_bugs"]:
        if any(f["bug_class"] == tb["bug_class"] and f["file"] == tb["file"] for f in r["findings"]):
            tool_hits += 1
    u = r["usage"]
    rep = r.get("repair") or {}
    return {
        "instance_id": gt["instance_id"], "system": r["system"], "bug_type": gt["bug_type"] or "control",
        "k": gt["n_distractors"], "hit_strict": hit_strict, "hit_file": hit_file, "semantic_fp": fps,
        "tool_bugs": len(gt["tool_bugs"]), "tool_hits": tool_hits,
        "llm_calls": u["llm_calls"], "prompt_tokens": u["prompt_tokens_evaluated"], "output_tokens": u["output_tokens"],
        "max_prompt_chars": u["max_prompt_chars"], "llm_wall_s": u["llm_wall_s"], "total_s": r["timing_s"]["total"],
        "parse_fail": u["json_parse_failures"], "peak_rss_mb": r.get("peak_ollama_rss_mb", 0.0),
        "repair_attempted": bool(rep), "repair_applied": bool(rep.get("applied")),
        "repair_passed": bool(rep.get("tests_passed")),
        "repair_on_true_bug": bool(rep) and gt["bug_type"] is not None
        and rep.get("target_finding", {}).get("bug_class") == gt["bug_type"],
        "warm_cache_hits": (r.get("warm_rerun") or {}).get("cache_hits"),
        "warm_modules": (r.get("warm_rerun") or {}).get("modules"),
        "warm_llm_calls": (r.get("warm_rerun") or {}).get("llm_calls"),
        "graph_modules": len(r["graph"]["modules"]), "repo_modules": r["graph"]["repo_modules"],
    }


def _mean(xs: List[float]) -> float:
    return round(sum(xs) / len(xs), 3) if xs else 0.0


def aggregate(rows: List[Dict]) -> Dict:
    by_sys: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        by_sys[r["system"]].append(r)
    out = {}
    for s, rs in by_sys.items():
        bug = [r for r in rs if r["bug_type"] != "control"]
        ctl = [r for r in rs if r["bug_type"] == "control"]
        tp = sum(r["hit_strict"] for r in bug)
        fp = sum(r["semantic_fp"] for r in rs)
        out[s] = {
            "n_bug": len(bug), "n_control": len(ctl),
            "recall_strict": _mean([r["hit_strict"] for r in bug]),
            "recall_file": _mean([r["hit_file"] for r in bug]),
            "recall_by_type": {t: _mean([r["hit_strict"] for r in bug if r["bug_type"] == t])
                               for t in sorted({r["bug_type"] for r in bug})},
            "recall_by_k": {str(k): _mean([r["hit_strict"] for r in bug if r["k"] == k]) for k in sorted({r["k"] for r in bug})},
            "semantic_fp_per_instance": _mean([r["semantic_fp"] for r in rs]),
            "control_fp_per_instance": _mean([r["semantic_fp"] for r in ctl]),
            "control_clean_rate": _mean([r["semantic_fp"] == 0 for r in ctl]),
            "precision": round(tp / (tp + fp), 3) if (tp + fp) else 0.0,
            "tool_bug_recall": round(sum(r["tool_hits"] for r in rs) / max(1, sum(r["tool_bugs"] for r in rs)), 3),
            "llm_calls": _mean([r["llm_calls"] for r in rs]),
            "prompt_tokens": _mean([r["prompt_tokens"] for r in rs]),
            "output_tokens": _mean([r["output_tokens"] for r in rs]),
            "max_prompt_chars": max((r["max_prompt_chars"] for r in rs), default=0),
            "mean_max_prompt_chars": _mean([r["max_prompt_chars"] for r in rs]),
            "llm_wall_s": _mean([r["llm_wall_s"] for r in rs]),
            "total_s": _mean([r["total_s"] for r in rs]),
            "parse_fail_total": sum(r["parse_fail"] for r in rs),
            "peak_rss_mb": max((r["peak_rss_mb"] for r in rs), default=0.0),
        }
        if s == "gsmae":
            att = [r for r in rs if r["repair_attempted"]]
            out[s]["repair"] = {
                "bug_instances": len(bug),
                "attempted": len(att),
                "attempted_on_true_bug": sum(r["repair_on_true_bug"] for r in att),
                "applied": sum(r["repair_applied"] for r in att),
                "hidden_tests_pass_bug_instances": sum(r["repair_passed"] for r in att if r["bug_type"] != "control"),
                "resolved_rate_over_bug_instances": round(
                    sum(r["repair_passed"] for r in att if r["bug_type"] != "control") / max(1, len(bug)), 3),
            }
            out[s]["warm_rerun"] = {
                "all_nodes_cached": all(r["warm_cache_hits"] == r["warm_modules"] for r in rs),
                "llm_calls_total": sum(r["warm_llm_calls"] or 0 for r in rs),
            }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    a = ap.parse_args()
    rows = [score_result(json.loads(p.read_text())) for p in sorted(a.results.glob("*__*.json"))]
    agg = aggregate(rows)
    (a.results / "scores_rows.json").write_text(json.dumps(rows, indent=1))
    (a.results / "summary.json").write_text(json.dumps(agg, indent=1))
    print(json.dumps(agg, indent=1))


if __name__ == "__main__":
    main()
