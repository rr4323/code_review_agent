"""Unit tests for GS-MAE CLI authentication and isolated per-agent workspaces."""

import json
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

from gs_mae.__main__ import main
from gs_mae.auth import (
    AuthenticationError,
    RepositoryError,
    authenticate,
    create_user_session,
    is_remote_url,
    resolve_repository,
)
from gs_mae.config import load_config
from gs_mae.graph_builder import ModuleGraphBuilder
from gs_mae.llm import MockClient
from gs_mae.pipeline import review
from gs_mae.workspace import AgentWorkspace, WorkspaceManager


def test_auth_validation_and_configuration():
    # Empty inputs fail
    with pytest.raises(AuthenticationError):
        authenticate("", "secret")
    with pytest.raises(AuthenticationError):
        authenticate("dev", "")
    with pytest.raises(AuthenticationError):
        authenticate("a", "secret")  # too short

    # Default valid credentials pass
    user = authenticate("alice", "supersecret")
    assert user == "alice"

    # Config with explicit user allowlist
    cfg_auth = {"auth": {"users": {"alice": "supersecret", "bob": "hashed_pw"}}}
    assert authenticate("alice", "supersecret", cfg_auth) == "alice"
    with pytest.raises(AuthenticationError):
        authenticate("alice", "wrongpass", cfg_auth)
    with pytest.raises(AuthenticationError):
        authenticate("charlie", "anypass", cfg_auth)


def test_repo_resolution_local_and_remote(tmp_path):
    # Local directory
    local_repo = tmp_path / "my_project"
    local_repo.mkdir()
    (local_repo / "main.py").write_text("print('hello')")

    resolved, is_remote = resolve_repository(str(local_repo), "dev", "pass123")
    assert resolved == local_repo
    assert not is_remote

    # Non-existent local directory raises RepositoryError
    with pytest.raises(RepositoryError):
        resolve_repository(str(tmp_path / "non_existent"), "dev", "pass123")

    # Remote URL detection
    assert is_remote_url("https://github.com/org/repo.git")
    assert is_remote_url("http://gitlab.local/group/project.git")
    assert is_remote_url("git@github.com:org/repo.git")
    assert not is_remote_url(str(local_repo))

    # User session creation
    session = create_user_session("alice", resolved, is_remote=False)
    assert session.username == "alice"
    assert session.session_id.startswith("sess_")
    assert session.repo_name == "my_project"


def test_agent_workspace_traceability(tmp_path):
    ws_mgr = WorkspaceManager(tmp_path / "workspaces", username="alice")
    ws = ws_mgr.get_or_create("subagent_store_correctness", role="category_subagent", module_id="store.controller", category="correctness")

    assert (ws.workspace_dir / "metadata.json").exists()
    assert (ws.workspace_dir / "activity.log").exists()

    # Record prompt
    ws.record_prompt("prompt_correctness", "SYSTEM: You are a reviewer.\nUSER: Check code.")
    assert (ws.workspace_dir / "prompts" / "prompt_correctness.txt").exists()

    # Record LLM response
    ws.record_response("response_correctness", {"findings": [{"line": 15, "explanation": "missing none check"}]})
    assert (ws.workspace_dir / "responses" / "response_correctness.json").exists()

    # Record deliverable artifact
    ws.record_artifact("findings.json", [{"line": 15, "severity": "high", "rule": "CORR-001"}])
    assert (ws.workspace_dir / "artifacts" / "findings.json").exists()

    # Verify written manifest and activity log
    summary = ws.get_summary()
    assert summary["agent_id"] == "subagent_store_correctness"
    assert summary["total_files_written"] >= 3
    assert summary["total_bytes_written"] > 0
    assert (ws.workspace_dir / "written_manifest.json").exists()

    # Verify central workspace manifest
    overall_manifest = ws_mgr.generate_manifest()
    assert overall_manifest["total_agents"] == 1
    assert "subagent_store_correctness" in overall_manifest["agents"]


def test_multi_agent_workspaces_in_graph_executor(tmp_path):
    repo = tmp_path / "garage_repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")

    cfg = load_config()
    def responder(role, mid, user):
        if role == "merge":
            return {"summary": "clean module", "risks_for_callers": [], "compound_risks": []}
        return {"findings": []}

    client = MockClient(responder)
    ws_dir = tmp_path / "executor_workspaces"
    report = review(repo, ["calc.py"], cfg, client, "gsmae", workspaces_dir=ws_dir)

    assert report["workspaces"]["total_agents"] >= 1
    assert (ws_dir / "workspace_manifest.json").exists()
    # Check node workspace exists and has recorded activity
    node_ws = ws_dir / "node_calc"
    assert (node_ws / "metadata.json").exists()
    assert (node_ws / "activity.log").exists()
    assert (node_ws / "artifacts" / "node_evaluation.json").exists()


def test_cli_end_to_end_with_credentials_and_workspaces(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")

    out_dir = tmp_path / "out"
    argv = [
        "--repo", str(repo),
        "-u", "alice",
        "-p", "secure_password",
        "--system", "gsmae",
        "--backend", "mock",
        "--out", str(out_dir),
        "--changed", "calc.py",
    ]
    exit_code = main(argv)
    assert exit_code == 0

    assert (out_dir / "review_report.json").exists()
    assert (out_dir / "review_report.md").exists()
    report = json.loads((out_dir / "review_report.json").read_text())
    assert report["user_session"]["username"] == "alice"
    assert report["system"] == "gsmae"
    assert report["usage"]["llm_calls"] >= 1


def test_cli_interactive_prompts_when_missing(tmp_path):
    repo = tmp_path / "interactive_repo"
    repo.mkdir()
    (repo / "main.py").write_text("def run(): pass\n")
    out_dir = tmp_path / "out_interactive"

    inputs = [str(repo), "bob_dev"]
    passwords = ["secret123"]

    with patch("builtins.input", side_effect=inputs):
        with patch("getpass.getpass", side_effect=passwords):
            argv = ["--system", "gsmae", "--backend", "mock", "--out", str(out_dir), "--changed", "main.py"]
            exit_code = main(argv)
            assert exit_code == 0

    report = json.loads((out_dir / "review_report.json").read_text())
    assert report["user_session"]["username"] == "bob_dev"
    assert report["status"] == "PASSED"
    assert report["system"] == "gsmae"
    assert report["usage"]["llm_calls"] >= 1


def test_cli_complete_codebase_scan_and_live_api(tmp_path):
    repo = tmp_path / "full_repo"
    repo.mkdir()
    (repo / "mod_a.py").write_text("def ping() -> str:\n    return 'pong'\n")
    (repo / "mod_b.py").write_text("import mod_a\ndef run() -> str:\n    return mod_a.ping()\n")
    server_code = (
        "import json, sys\n"
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        if self.path == '/health': body = b'{}'\n"
        "        elif self.path == '/openapi.json': body = json.dumps({'paths': {'/ping': {'get': {'operationId': 'ping'}}}}).encode()\n"
        "        elif self.path == '/ping': body = json.dumps({'ok': True}).encode()\n"
        "        else: self.send_response(404); self.end_headers(); return\n"
        "        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)\n"
        "    def log_message(self, *args): pass\n"
        "HTTPServer(('127.0.0.1', 19876), Handler).serve_forever()\n"
    )
    (repo / "server.py").write_text(server_code)

    out_dir = tmp_path / "out_full"
    argv = [
        "--repo", str(repo),
        "-u", "alice",
        "-p", "secure_password",
        "--system", "gsmae",
        "--backend", "mock",
        "--all",
        "--live-api",
        "--live-api-cmd", sys.executable, "server.py",
        "--live-api-url", "http://127.0.0.1:19876",
        "--out", str(out_dir),
    ]
    exit_code = main(argv)
    assert exit_code == 0
    report = json.loads((out_dir / "review_report.json").read_text())
    assert len(report["roots"]) >= 2
    assert report["system"] == "gsmae"
    assert report["usage"]["llm_calls"] >= 2
    assert report["evidence"]["tool_runs"]["performance"]["details"]["status"] == "ready"
    assert (out_dir / "review_report.md").exists()
    assert "live API: `ready`" in (out_dir / "review_report.md").read_text()

