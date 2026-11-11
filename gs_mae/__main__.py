"""CLI: python -m gs_mae [--repo PATH_OR_URL] [--username USER] [--password PASS] [--changed file.py] [--system gsmae] [--repair] [--out outputs/]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gs_mae.auth import (
    AuthenticationError,
    RepositoryError,
    authenticate,
    create_user_session,
    prompt_credentials,
    prompt_repo,
    resolve_repository,
)
from gs_mae.config import load_config
from gs_mae.graph_builder import ModuleGraphBuilder
from gs_mae.llm import make_client
from gs_mae.pipeline import SYSTEMS, render_markdown, review, save_report


CLI_SYSTEMS = ("gsmae", "flat", "gsmae_no_msg")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gs_mae",
        description="GS-MAE: Autonomous Multi-Agent Code Comprehension, Review & Repair on Gemma 4",
    )
    ap.add_argument("--repo", default=None, help="Local repository path or remote git URL (prompted if omitted)")
    ap.add_argument("-u", "--username", default=None, help="Username for authentication (prompted if omitted)")
    ap.add_argument("-p", "--password", default=None, help="Password for authentication (prompted securely if omitted)")
    ap.add_argument("--changed", nargs="*", default=None, help="Changed files to review; default: git diff HEAD~1 or all sources")
    ap.add_argument("--all", action="store_true", help="Perform a complete codebase scan across all repository modules")
    ap.add_argument("--system", default="gsmae", choices=CLI_SYSTEMS, help="Agent architecture to execute (default: gsmae, always tools + LLM)")
    ap.add_argument("--config", type=Path, default=None, help="Path to custom gs_mae_config.yaml")
    ap.add_argument("--model", default=None, help="Override model tag, e.g. gemma4:e2b-it-qat")
    ap.add_argument("--backend", default=None, choices=("ollama", "mock"), help="Override model runtime backend")
    ap.add_argument("--live-api", action="store_true", default=None, help="Enable live API profiling and OpenAPI exercise")
    ap.add_argument("--no-live-api", action="store_true", help="Disable live API profiling")
    ap.add_argument("--live-api-cmd", nargs="*", default=None, help="Command to start local service for live API profiling")
    ap.add_argument("--live-api-url", default=None, help="Base URL for live API profiling (default: http://127.0.0.1:18000)")
    ap.add_argument("--repair", action="store_true", help="Synthesize, AST-verify and test-verify automated repair patch")
    ap.add_argument("--cache", type=Path, default=None, help="Path to Merkle graph cache file")
    ap.add_argument("--workspaces-dir", type=Path, default=None, help="Base directory for isolated per-agent workspaces")
    ap.add_argument("--out", type=Path, default=Path("outputs"), help="Directory for final reports and artifacts (default: outputs/)")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)

    # 1. Obtain repository and user credentials (prompt interactively if missing)
    repo_target = prompt_repo(a.repo)
    username, password = prompt_credentials(a.username, a.password)

    # 2. Load configuration
    overrides = {}
    if a.model:
        overrides["model_runtime.model"] = a.model
    if a.backend:
        overrides["model_runtime.backend"] = a.backend
    if a.live_api is True:
        overrides["live_api.enabled"] = True
    elif a.no_live_api:
        overrides["live_api.enabled"] = False
    if a.live_api_cmd:
        overrides["live_api.start_command"] = a.live_api_cmd
    if a.live_api_url:
        overrides["live_api.base_url"] = a.live_api_url

    cfg = load_config(a.config, overrides if overrides else None)

    # 3. Authenticate user
    try:
        auth_user = authenticate(username, password, config=cfg)
        print(f"[gs-mae] Authenticated user '{auth_user}'")
    except AuthenticationError as exc:
        print(f"[gs-mae] Authentication failed: {exc}", file=sys.stderr)
        return 3

    # 4. Resolve local path or clone remote repository with authenticated credentials
    try:
        resolved_repo, is_remote = resolve_repository(
            repo_target, auth_user, password, checkout_root=a.out / "checkouts"
        )
    except RepositoryError as exc:
        print(f"[gs-mae] Repository error: {exc}", file=sys.stderr)
        return 4

    user_session = create_user_session(auth_user, resolved_repo, is_remote)
    workspaces_dir = a.workspaces_dir or (a.out / "workspaces" / user_session.session_id)

    # 5. Determine changed files
    builder = ModuleGraphBuilder(resolved_repo)
    if a.all:
        builder._index_paths()
        changed = sorted(rel for _ad, rels in builder._path_index.values() for rel in rels)
    elif a.changed:
        changed = a.changed
    else:
        try:
            changed = builder.changed_files_from_git()
        except Exception:
            changed = []
        if not changed:
            builder._index_paths()
            changed = sorted(rel for _ad, rels in builder._path_index.values() for rel in rels)

    # 6. Initialize LLM client (scans always run tools + LLM)
    client = make_client(cfg)
    if not client.health():
        print(
            f"[gs-mae] Model '{client.model}' is not available at {client.url}. "
            f"Start Ollama and run `ollama pull {client.model}`.",
            file=sys.stderr,
        )
        return 2

    # 7. Execute review with threaded agents, message queue, and per-agent workspaces
    report_cache = a.cache or (a.out / ".merkle_cache.json")
    report = review(
        repo=resolved_repo,
        changed_files=changed,
        cfg=cfg,
        client=client,
        system=a.system,
        cache_path=report_cache,
        repair=a.repair,
        workspaces_dir=workspaces_dir,
        user_session=user_session,
    )

    # 8. Save deliverables and render markdown summary
    save_report(report, a.out)
    print(render_markdown(report))
    print(f"\n[gs-mae] Per-agent workspaces tracked in: {workspaces_dir}")
    print(f"[gs-mae] Full report written to: {a.out / 'review_report.json'}")

    return 1 if report["status"] == "FAILED_QUALITY_GATE" else 0


if __name__ == "__main__":
    sys.exit(main())
