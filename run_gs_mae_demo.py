#!/usr/bin/env python3
"""
End-to-End CLI Demo for GS-MAE (Gemma 4 Developer Agent).

Runs GS-MAE on a representative CrossMod-Bench multi-module repository (or `--repo`),
demonstrating:
1. Change Detection -> Scoped Module Dependency Graph (Bottom-Up Waves & Merkle Hashes)
2. Workflow 1 -> Parallel Evidence Gathering Agent (Bandit SAST, AST N+1 & Layer Rules, PyTest)
3. Workflow 2 -> Per-Node Category Subagent Spawning & Recursive Child-to-Parent Merging
4. Automated Patch Synthesis & Hidden Test Verification
5. Warm Merkle Graph Cache Re-Run (0 LLM calls on unchanged subtrees)
"""

from __future__ import annotations

import argparse
from pathlib import Path

from gs_mae.config import load_config
from gs_mae.llm import make_client
from gs_mae.pipeline import render_markdown, review, save_report


def main() -> int:
    ap = argparse.ArgumentParser(description="Run GS-MAE end-to-end demo.")
    ap.add_argument(
        "--instance",
        type=Path,
        default=Path("benchmark/instances/cmb-015-garage-argument_order-k1"),
        help="Path to a CrossMod-Bench instance directory",
    )
    ap.add_argument("--out", type=Path, default=Path("outputs"))
    ap.add_argument("--no-repair", action="store_true", help="Skip patch synthesis & verification")
    args = ap.parse_args()

    cfg = load_config()
    client = make_client(cfg)
    if not client.health():
        print(f"ERROR: Ollama model '{client.model}' is not reachable at {client.url}.")
        return 2

    repo = args.instance / "repo"
    gt_file = args.instance / "ground_truth.json"
    hidden_test_file = args.instance / "hidden_test.py"

    import json
    gt = json.loads(gt_file.read_text(encoding="utf-8"))
    changed_files = gt["changed_files"]
    extra_tests = (
        {"tests/test_hidden.py": hidden_test_file.read_text(encoding="utf-8")}
        if hidden_test_file.exists()
        else None
    )

    cache_path = args.out / ".demo_merkle_cache.json"
    if cache_path.exists():
        cache_path.unlink()

    print("=" * 84)
    print(f"GS-MAE Live Gemma 4 Execution ({client.model}) on {gt['instance_id']}")
    print("=" * 84)

    report = review(
        repo=repo,
        changed_files=changed_files,
        cfg=cfg,
        client=client,
        system="gsmae",
        cache_path=cache_path,
        repair=(not args.no_repair),
        repair_extra_tests=extra_tests,
    )
    save_report(report, args.out)
    print(render_markdown(report))

    # Warm Merkle Cache Re-Run
    calls_before = len(client.ledger.calls)
    warm = review(
        repo=repo,
        changed_files=changed_files,
        cfg=cfg,
        client=client,
        system="gsmae",
        cache_path=cache_path,
        repair=False,
    )
    warm_calls = len(client.ledger.calls) - calls_before
    print(
        f"Warm Merkle Cache Re-Run: {warm['cache_hits']}/{len(warm['graph']['modules'])} modules cached, "
        f"{warm_calls} LLM calls, {warm['timing_s']['total']} s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
