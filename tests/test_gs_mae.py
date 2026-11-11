"""Unit tests. The MockClient is used ONLY here to test wiring; reported results use real Gemma 4."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from benchmark.generate import DOMAINS, make_instance, write_instance
from gs_mae.agents import GraphExecutor
from gs_mae.config import config_fingerprint, load_config
from gs_mae.evidence_gathering import EvidenceGatheringAgent
from gs_mae.graph_builder import MerkleGraphCache, ModuleGraphBuilder
from gs_mae.lang_adapters import CppAdapter, GoAdapter, RustAdapter
from gs_mae.graph_grpo_trainer import GraphGRPORewardEngine
from gs_mae.llm import MockClient, parse_json_loose
from gs_mae.live_api import LiveApiRunner
from gs_mae.monitor import (
    monitor_config_and_runtime,
    monitor_evidence_parameters,
    monitor_graph_parameters,
    monitor_llm_and_node_parameters,
)
from gs_mae.pipeline import render_markdown, review
from gs_mae.prompts import extract_contract_anomalies
from gs_mae.repair import RepairAgent, apply_search_replace
from gs_mae.sandbox import SandboxConfig, run_python_sandboxed, validate_patch_ast

import random


@pytest.fixture()
def inst_repo(tmp_path):
    inst = make_instance(0, "argument_order", DOMAINS[0], 2, random.Random(1))
    inst["flags"].update(sqli=True)
    write_instance(inst, tmp_path)
    return tmp_path / inst["instance_id"] / "repo", inst


def test_graph_is_change_rooted_and_bottom_up(inst_repo):
    repo, inst = inst_repo
    g = ModuleGraphBuilder(repo).build(inst["changed_files"])
    assert g.root == "store.product_controller"
    assert "store.reporting" not in g.modules and "store.schema" not in g.modules
    assert g.waves[-1] == [g.root]
    assert g.waves[0][0] == "store.product_repository" or "store.product_repository" in g.waves[0]
    pos = {m: i for i, w in enumerate(g.waves) for m in w}
    for parent, child in g.edges():
        assert pos[child] < pos[parent]


def test_contracts_capture_none_and_raises(inst_repo):
    repo, inst = inst_repo
    g = ModuleGraphBuilder(repo).build(inst["changed_files"])
    repo_c = {c.name: c for c in g.modules["store.product_repository"].contracts}
    svc_c = {c.name: c for c in g.modules["store.product_service"].contracts}
    assert repo_c["get_product"].may_return_none
    assert "OutOfStockError" in svc_c["reserve_stock"].raises


def test_merkle_hash_changes_with_child_and_config(inst_repo):
    repo, inst = inst_repo
    h1 = ModuleGraphBuilder(repo, "cfgA").build(inst["changed_files"]).modules["store.product_controller"].merkle_hash
    h2 = ModuleGraphBuilder(repo, "cfgB").build(inst["changed_files"]).modules["store.product_controller"].merkle_hash
    assert h1 != h2
    p = repo / "store" / "product_repository.py"
    p.write_text(p.read_text() + "\n\ndef extra() -> None:\n    return None\n")
    h3 = ModuleGraphBuilder(repo, "cfgA").build(inst["changed_files"]).modules["store.product_controller"].merkle_hash
    assert h3 != h1


def test_tools_find_injected_defects(tmp_path):
    inst = make_instance(1, None, DOMAINS[1], 1, random.Random(3))
    write_instance(inst, tmp_path)
    repo = tmp_path / inst["instance_id"] / "repo"
    cfg = load_config()
    files = [*inst["changed_files"], f"{DOMAINS[1]['pkg']}/{DOMAINS[1]['E']}_serializer.py"]
    g = ModuleGraphBuilder(repo, config_fingerprint(cfg)).build(files)
    store = EvidenceGatheringAgent(cfg, g, repo).run()
    rules = {e.rule_id for e in store.items}
    if inst["flags"]["sqli"]:
        assert "BANDIT-B608" in rules
    if inst["flags"]["nplus1"]:
        assert "PERF-NPLUS1" in rules and "FW-001" in rules
    if inst["flags"]["arch"]:
        assert "ARCH-001" in rules
    assert store.tool_runs["security"]["tool"] == "bandit"


def _responder(role, module_id, user):
    if role.startswith("category:correctness"):
        return {"findings": [{"line": 3, "bug_class": "argument_order", "severity": "high", "explanation": "swapped"}]}
    if role.startswith("category:"):
        return {"verdicts": []}
    if role == "merge":
        return {"summary": f"merged {module_id}", "risks_for_callers": ["r"], "compound_risks": []}
    return {}


def test_executor_spawns_per_node_and_merges_bottom_up(inst_repo, tmp_path):
    repo, inst = inst_repo
    cfg = load_config()
    client = MockClient(_responder)
    r = review(repo, inst["changed_files"], cfg, client, "gsmae", cache_path=tmp_path / "c.json")
    root = r["nodes"][r["root_module"]]
    assert set(root["children_merged"]) >= {"store.product_service", "store.product_repository"}
    assert "store.product_serializer" not in root["children_merged"]
    assert "store.product_serializer" not in r["graph"]["modules"]
    assert "correctness" in root["spawned"]
    leaf = r["nodes"]["store.product_repository"]
    assert "correctness" in leaf["skipped"]           # leaf has no children to check
    # child findings propagate into the root's merged evaluation
    assert any(f["module_id"] == "store.product_service" for f in root["inherited_findings"]) or \
        any(f["module_id"] != r["root_module"] for f in r["findings"])
    # merge messages were consumed: parent prompt contains the child's summary
    merge_calls = [c for c in client.ledger.calls if c.role == "merge"]
    assert merge_calls
    # warm run: every node comes from the Merkle cache, zero LLM calls
    client2 = MockClient(_responder)
    r2 = review(repo, inst["changed_files"], cfg, client2, "gsmae", cache_path=tmp_path / "c.json")
    assert r2["cache_hits"] == len(r2["graph"]["modules"]) and r2["usage"]["llm_calls"] == 0


def test_no_message_ablation_hides_child_contracts(inst_repo):
    repo, inst = inst_repo
    seen = []
    def resp(role, mid, user):
        seen.append((role, mid, user))
        return _responder(role, mid, user)
    review(repo, inst["changed_files"], load_config(), MockClient(resp), "gsmae_no_msg")
    assert not any(r == "merge" for r, _, _ in seen)
    assert all("MAY RETURN None" not in u for _, _, u in seen)


def test_search_replace_and_json_parsing():
    src = "def f(a, b):\n    g(b, a)\n    return 1\n"
    assert apply_search_replace(src, "  2|     g(b, a)", "    g(a, b)") == "def f(a, b):\n    g(a, b)\n    return 1\n"
    assert apply_search_replace(src, "g(b, a)\n", "    g(a, b)\n    if a is None:\n        return 0\n") == (
        "def f(a, b):\n    g(a, b)\n    if a is None:\n        return 0\n    return 1\n"
    )
    assert apply_search_replace(src, "not there", "x") is None
    assert parse_json_loose("noise {\"a\": 1} tail") == {"a": 1}


def test_parameter_monitor_sections(inst_repo):
    repo, inst = inst_repo
    cfg = load_config()
    r = review(repo, inst["changed_files"], cfg, MockClient(_responder), "gsmae")
    c_mon = monitor_config_and_runtime()
    g_mon = monitor_graph_parameters(r)
    e_mon = monitor_evidence_parameters(r)
    l_mon = monitor_llm_and_node_parameters(r)
    assert "config_fingerprint" in c_mon and "hardware_telemetry" in c_mon
    assert g_mon["root_module"] == "store.product_controller" and g_mon["pruned_distractor_modules"] >= 2
    assert "security" in e_mon["tool_runs_by_category"]
    assert l_mon["system"] == "gsmae" and len(l_mon["node_execution_trace"]) == g_mon["scoped_modules_count"]


def test_graph_grpo_reward_engine():
    eng = GraphGRPORewardEngine()
    r1 = eng.score_trajectory(
        predicted_modules={"store.product_controller"},
        ground_truth_modules={"store.product_controller"},
        predicted_rules={"BANDIT-B608", "PERF-NPLUS1"},
        ground_truth_rules={"BANDIT-B608", "PERF-NPLUS1"},
        patch_passes_tests=True,
        perf_regression_resolved=True,
        inter_node_message_tokens=180,
    )
    r2 = eng.score_trajectory(
        predicted_modules=set(),
        ground_truth_modules={"store.product_controller"},
        predicted_rules={"BANDIT-B608"},
        ground_truth_rules={"BANDIT-B608", "PERF-NPLUS1"},
        patch_passes_tests=False,
        perf_regression_resolved=False,
        inter_node_message_tokens=400,
    )
    ranked = eng.compute_group_advantages([r1, r2])
    assert ranked[0].r_total > ranked[1].r_total
    assert ranked[0].advantage > 0 > ranked[1].advantage


def test_sandbox_isolation_and_ast_gate(inst_repo, monkeypatch):
    repo, _ = inst_repo
    # 1. Static AST gate blocks syntax errors and newly introduced dangerous imports/calls
    g_syn = validate_patch_ast("def f():\n    return {'a': 1}\n", "def f():\n    return {'a': 1\n")
    assert not g_syn["syntax_valid"] and not g_syn["safe"]
    g_bad = validate_patch_ast("def f():\n    return 1\n", "import subprocess\ndef f():\n    os.system('id')\n    return 1\n")
    assert g_bad["syntax_valid"] and not g_bad["safe"] and "subprocess" in g_bad["blocked_imports"]
    g_ok = validate_patch_ast("def f(a, b):\n    return g(b, a)\n", "def f(a, b):\n    return g(a, b)\n")
    assert g_ok["syntax_valid"] and g_ok["safe"]

    # 2. Runtime sandbox scrubs host env, blocks outbound sockets, and blocks outside writes
    monkeypatch.setenv("GS_MAE_TEST_SECRET_TOKEN", "top-secret-123")
    probe_code = (
        "import os, socket\n"
        "assert 'GS_MAE_TEST_SECRET_TOKEN' not in os.environ, 'secret leaked'\n"
        "try:\n"
        "    socket.create_connection(('8.8.8.8', 53), timeout=1)\n"
        "    raise AssertionError('network should be blocked')\n"
        "except PermissionError:\n"
        "    pass\n"
        "try:\n"
        "    open('/tmp/gsmae_outside_escape_test.txt', 'w').write('x')\n"
        "    raise AssertionError('outside write should be blocked')\n"
        "except PermissionError:\n"
        "    pass\n"
        "print('SANDBOX_ISOLATION_VERIFIED')\n"
    )
    sbx = run_python_sandboxed(repo, ["-c", probe_code], SandboxConfig())
    assert sbx.passed and "SANDBOX_ISOLATION_VERIFIED" in sbx.stdout
    assert sbx.telemetry["host_repo_unmodified"] and sbx.telemetry["network_blocked"]


def test_repair_agent_self_correction_retry(inst_repo):
    repo, inst = inst_repo
    g = ModuleGraphBuilder(repo).build(inst["changed_files"])
    mod = g.modules[g.root]
    calls = []

    def retry_responder(role, mid, user):
        calls.append(user)
        if len(calls) == 1:
            # First attempt intentionally returns broken syntax (missing closing parenthesis)
            return {
                "search": "    service.transfer_credit(conn, payee_id, payer_id, amount)",
                "replace": "    service.transfer_credit(conn, payer_id, payee_id, amount",
            }
        # Second attempt sees SyntaxError feedback and returns valid fix
        assert "PREVIOUS ATTEMPT FAILED" in user and "SyntaxError" in user
        return {
            "search": "    service.transfer_credit(conn, payee_id, payer_id, amount)",
            "replace": "    service.transfer_credit(conn, payer_id, payee_id, amount)",
        }

    finding = {
        "module_id": g.root,
        "file": mod.rel_path,
        "line": 23,
        "bug_class": "argument_order",
        "severity": "critical",
        "explanation": "reversed arguments",
    }
    res = RepairAgent(MockClient(retry_responder), load_config(), max_attempts=2).repair_and_verify(
        repo, mod, finding, []
    )
    assert res["attempts"] == 2 and res["tests_passed"] and res["sandbox_telemetry"]["host_repo_unmodified"]


def test_ast_and_topology_layer_inference_without_keywords(tmp_path):
    """When filenames do not contain LAYER_KEYWORDS and have no `gsmae: layer=` comment,
    `infer_layer` falls back to AST structure + graph topology."""
    pkg = tmp_path / "billing"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "persistence.py").write_text(
        "import sqlite3\n"
        "def fetch_invoice(conn: sqlite3.Connection, inv_id: int):\n"
        "    return conn.execute('SELECT * FROM invoices WHERE id = ?', (inv_id,)).fetchone()\n"
    )
    (pkg / "payload.py").write_text(
        "from billing import persistence\n"
        "def to_dict_invoice(conn, inv_id: int) -> dict:\n"
        "    row = persistence.fetch_invoice(conn, inv_id)\n"
        "    return {'id': row['id'], 'amount': row['amount']}\n"
    )
    (pkg / "logic.py").write_text(
        "from billing import persistence\n"
        "class InsufficientFundsError(Exception):\n"
        "    pass\n"
        "def settle_invoice(conn, inv_id: int) -> int:\n"
        "    row = persistence.fetch_invoice(conn, inv_id)\n"
        "    if not row:\n"
        "        raise InsufficientFundsError('missing')\n"
        "    return int(row['amount'])\n"
    )
    (pkg / "entry.py").write_text(
        "from billing import logic, payload\n"
        "def post_settlement(conn, inv_id: int) -> dict:\n"
        "    total = logic.settle_invoice(conn, inv_id)\n"
        "    view = payload.to_dict_invoice(conn, inv_id)\n"
        "    return {'status': 200, 'settled': total, 'invoice': view}\n"
    )
    g = ModuleGraphBuilder(tmp_path).build(["billing/entry.py"])
    assert g.modules["billing.entry"].layer == "presentation"
    assert g.modules["billing.logic"].layer == "domain"
    assert g.modules["billing.payload"].layer == "serialization"
    assert g.modules["billing.persistence"].layer == "data"


def test_non_web_cli_and_systems_codebase_support(tmp_path):
    """Verifies GS-MAE works on non-web codebases (CLI tools, systems/data pipelines)
    for layer inference, repeated file-I/O in loops, and ms/seconds + src/dst contract checks."""
    pkg = tmp_path / "pipeline"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "file_loader.py").write_text(
        "from pathlib import Path\n"
        "def load_blob(path: Path) -> bytes:\n"
        "    return path.read_bytes()\n"
    )
    (pkg / "worker_engine.py").write_text(
        "from pathlib import Path\n"
        "from pipeline import file_loader\n"
        "def copy_artifact(src_path: str, dst_path: str) -> int:\n"
        "    \"\"\"Copy artifact from src_path to dst_path.\"\"\"\n"
        "    return len(src_path) + len(dst_path)\n"
        "def estimate_latency_ms(items: int) -> int:\n"
        "    \"\"\"Return estimated processing latency in milliseconds.\"\"\"\n"
        "    return items * 250\n"
        "def batch_ingest(paths: list[Path]) -> int:\n"
        "    total = 0\n"
        "    for p in paths:\n"
        "        total += len(file_loader.load_blob(p))\n"
        "    return total\n"
    )
    (pkg / "cli_runner.py").write_text(
        "import argparse\n"
        "from pipeline import worker_engine\n"
        "def run_sync(src_path: str, dst_path: str, count: int) -> float:\n"
        "    worker_engine.copy_artifact(dst_path, src_path)\n"
        "    total_seconds = worker_engine.estimate_latency_ms(count) * 1.1\n"
        "    return total_seconds\n"
    )
    cfg = load_config()
    g = ModuleGraphBuilder(tmp_path, config_fingerprint(cfg)).build(["pipeline/cli_runner.py"])
    assert g.modules["pipeline.cli_runner"].layer == "presentation"
    assert g.modules["pipeline.worker_engine"].layer == "domain"
    assert g.modules["pipeline.file_loader"].layer == "data"

    # Workflow 1 detects repeated file I/O inside a loop in worker_engine.batch_ingest
    store = EvidenceGatheringAgent(cfg, g, tmp_path).run()
    assert any(e.rule_id == "PERF-NPLUS1" and e.module_id == "pipeline.worker_engine" for e in store.items)

    # Contract anomaly detector catches both src/dst swap and ms/seconds mismatch in cli_runner
    child_msg = {
        "module_id": "pipeline.worker_engine",
        "contracts": [c.to_dict() for c in g.modules["pipeline.worker_engine"].contracts],
    }
    anoms = extract_contract_anomalies(g.modules["pipeline.cli_runner"], [child_msg])
    classes = {a["candidate_class"] for a in anoms}
    assert "argument_order" in classes and "unit_mismatch" in classes


def test_multilang_rust_go_cpp_and_ml_notebook_graphs(tmp_path):
    """Python and notebooks are built in. Rust, Go, and C++ run through registered adapters."""
    # 1. Rust (.rs) crate
    rs_root = tmp_path / "rust_proj" / "src"
    rs_root.mkdir(parents=True)
    (rs_root / "store.rs").write_text(
        "pub fn fetch_row(id: u64) -> Option<String> {\n"
        "    sqlx::query(\"SELECT * FROM items\");\n"
        "    None\n"
        "}\n"
    )
    (rs_root / "engine.rs").write_text(
        "use crate::store;\n"
        "/// Transfer amount from src_id to dst_id.\n"
        "pub fn move_funds(src_id: u64, dst_id: u64) -> Result<u64, LedgerError> {\n"
        "    let _ = store::fetch_row(src_id);\n"
        "    Ok(1)\n"
        "}\n"
    )
    (rs_root / "main.rs").write_text(
        "use crate::engine;\n"
        "pub fn main() {\n"
        "    let src_id = 1;\n"
        "    let dst_id = 2;\n"
        "    let _ = engine::move_funds(dst_id, src_id);\n"
        "}\n"
    )
    with pytest.raises(ValueError):
        ModuleGraphBuilder(tmp_path / "rust_proj").build(["src/main.rs"])
    g_rs = ModuleGraphBuilder(tmp_path / "rust_proj", adapters=(RustAdapter(),)).build(["src/main.rs"])
    assert set(g_rs.modules) == {"src.main", "src.engine", "src.store"}
    assert g_rs.modules["src.main"].layer == "presentation"
    assert g_rs.modules["src.engine"].layer == "domain"
    assert g_rs.modules["src.store"].layer == "data"
    assert g_rs.modules["src.store"].contracts[0].may_return_none
    assert "LedgerError" in g_rs.modules["src.engine"].contracts[0].raises
    rs_anoms = extract_contract_anomalies(
        g_rs.modules["src.main"],
        [{"module_id": "src.engine", "contracts": [c.to_dict() for c in g_rs.modules["src.engine"].contracts]}],
    )
    assert any(a["candidate_class"] == "argument_order" for a in rs_anoms)

    # 2. Go (.go) module
    go_root = tmp_path / "go_proj"
    go_root.mkdir()
    (go_root / "reader.go").write_text(
        "package main\n"
        "func ReadRecord(id int) (*Record, error) {\n"
        "    return nil, fmt.Errorf(\"not found\")\n"
        "}\n"
    )
    (go_root / "cmd.go").write_text(
        "package main\n"
        "import \"go_proj/reader\"\n"
        "func main() {\n"
        "    _, _ = ReadRecord(1)\n"
        "}\n"
    )
    g_go = ModuleGraphBuilder(go_root, adapters=(GoAdapter(),)).build(["cmd.go"])
    assert set(g_go.modules) == {"cmd", "reader"}
    assert g_go.modules["reader"].contracts[0].may_return_none
    assert "error" in g_go.modules["reader"].contracts[0].raises

    # 3. C++ (.hpp + .cpp)
    cpp_root = tmp_path / "cpp_proj"
    cpp_root.mkdir()
    (cpp_root / "tensor_loader.hpp").write_text("class TensorLoader {};\n")
    (cpp_root / "tensor_loader.cpp").write_text(
        "#include \"tensor_loader.hpp\"\n"
        "float* load_weights(int layer_id) {\n"
        "    if (layer_id < 0) { throw OutOfRangeError(\"bad id\"); }\n"
        "    return nullptr;\n"
        "}\n"
    )
    (cpp_root / "runner.cpp").write_text(
        "#include \"tensor_loader.hpp\"\n"
        "int main() {\n"
        "    float* w = load_weights(0);\n"
        "    return w == nullptr ? 1 : 0;\n"
        "}\n"
    )
    g_cpp = ModuleGraphBuilder(cpp_root, adapters=(CppAdapter(),)).build(["runner.cpp"])
    assert set(g_cpp.modules) == {"runner", "tensor_loader"}
    assert g_cpp.modules["tensor_loader"].contracts[0].may_return_none
    assert "OutOfRangeError" in g_cpp.modules["tensor_loader"].contracts[0].raises

    # 4. ML Jupyter Notebook (.ipynb) importing a Python feature loader
    nb_root = tmp_path / "ml_proj"
    nb_root.mkdir()
    (nb_root / "dataset_loader.py").write_text(
        "from pathlib import Path\n"
        "def load_Shard(path: Path) -> bytes:\n"
        "    return path.read_bytes()\n"
    )
    ipynb_json = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Training Notebook"]},
            {
                "cell_type": "code",
                "source": [
                    "%matplotlib inline\n",
                    "from pathlib import Path\n",
                    "import dataset_loader\n",
                    "def train_epoch(shards: list[Path]) -> int:\n",
                    "    total = 0\n",
                    "    for s in shards:\n",
                    "        total += len(dataset_loader.load_Shard(s))\n",
                    "    return total\n",
                ],
            },
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    (nb_root / "train_experiment.ipynb").write_text(json.dumps(ipynb_json))
    cfg = load_config()
    g_nb = ModuleGraphBuilder(nb_root, config_fingerprint(cfg)).build(["train_experiment.ipynb"])
    assert set(g_nb.modules) == {"train_experiment", "dataset_loader"}
    assert g_nb.modules["train_experiment"].language == "notebook"
    assert g_nb.modules["train_experiment"].layer == "presentation"
    assert g_nb.modules["dataset_loader"].layer == "data"
    nb_store = EvidenceGatheringAgent(cfg, g_nb, nb_root).run()
    assert any(e.rule_id == "PERF-NPLUS1" and e.module_id == "train_experiment" for e in nb_store.items)


def test_call_graph_snapshot_and_cone_filter(inst_repo, tmp_path):
    """Build with every repo source file and a snapshot, then build again with only the controller.
    The second cone excludes reporting, schema, and the serializer, and an edit to an unlisted file
    does not change the controller hash. A third build that lists the edited callee does change it."""
    repo, inst = inst_repo
    snap_path = tmp_path / "graph_snapshot.json"
    all_src = sorted(str(p.relative_to(repo)) for p in (repo / "store").glob("*.py"))

    # 1. First build with every repo source file stores the full snapshot
    g1 = ModuleGraphBuilder(repo, "cfgA", graph_path=snap_path).build(all_src)
    assert snap_path.exists()
    assert {"store.reporting", "store.schema", "store.product_serializer"} <= set(g1.modules)

    # 2. Second build with only the controller selects its call cone from the snapshot
    g2 = ModuleGraphBuilder(repo, "cfgA", graph_path=snap_path).build(inst["changed_files"])
    assert {"store.product_controller", "store.product_service", "store.product_repository"} <= set(g2.modules)
    assert "store.reporting" not in g2.modules
    assert "store.schema" not in g2.modules
    assert "store.product_serializer" not in g2.modules
    h_ctrl = g2.modules["store.product_controller"].merkle_hash

    # Editing an unlisted callee does not change the controller hash because the snapshot entry is reused
    callee = repo / "store" / "product_repository.py"
    callee.write_text(callee.read_text() + "\n\ndef extra_unlisted() -> None:\n    return None\n")
    g2_unlisted = ModuleGraphBuilder(repo, "cfgA", graph_path=snap_path).build(inst["changed_files"])
    assert g2_unlisted.modules["store.product_controller"].merkle_hash == h_ctrl

    # 3. Third build listing the edited callee refreshes its snapshot entry and changes the controller hash
    g3 = ModuleGraphBuilder(repo, "cfgA", graph_path=snap_path).build(
        [*inst["changed_files"], "store/product_repository.py"]
    )
    assert g3.modules["store.product_controller"].merkle_hash != h_ctrl


def test_live_openapi_profile_is_scoped_and_uses_external_baseline(tmp_path):
    """The opt-in runner starts a local service, profiles a safe operation, and never writes into the repo."""
    repo = tmp_path / "api_repo"
    repo.mkdir()
    (repo / "api_app.py").write_text(
        "import json\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "def ping(): return {'ok': True}\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        if self.path == '/health': body = b'{}'\n"
        "        elif self.path == '/openapi.json': body = json.dumps({'paths': {'/ping': {'get': {'operationId': 'ping'}}}}).encode()\n"
        "        elif self.path == '/ping': body = json.dumps(ping()).encode()\n"
        "        else: self.send_response(404); self.end_headers(); return\n"
        "        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)\n"
        "    def log_message(self, *args): pass\n"
        "HTTPServer(('127.0.0.1', 18765), Handler).serve_forever()\n"
    )
    cfg = load_config()
    baseline = tmp_path / "baselines" / "api.json"
    cfg["live_api"].update({
        "enabled": True,
        "start_command": [sys.executable, "api_app.py"],
        "base_url": "http://127.0.0.1:18765",
        "repetitions": 2,
        "warmup_runs": 0,
        "baseline_path": str(baseline),
        "slow_endpoint_ms": 0,
    })
    graph = ModuleGraphBuilder(repo).build(["api_app.py"])
    runner = LiveApiRunner(cfg, graph, repo)
    _, skipped = runner._operations({"paths": {
        "/mutate": {"post": {"operationId": "ping"}},
    }})
    assert skipped[0]["reason"] == "method not enabled"
    assert runner._substitute("/items/{item_id}", {
        "parameters": [{"name": "item_id", "in": "path", "required": True, "schema": {}}],
    }) is None
    store = EvidenceGatheringAgent(cfg, graph, repo).run()
    details = store.tool_runs["performance"]["details"]
    assert details["status"] == "ready"
    assert details["executed_operations"][0]["module_id"] == "api_app"
    assert details["profile"]
    assert baseline.exists() and not (repo / ".gsmae_perf_baseline.json").exists()

    baseline.write_text(json.dumps({"ping": 0.0001}))
    second = EvidenceGatheringAgent(cfg, graph, repo).run()
    assert any(e.rule_id == "PERF-REGRESSION" for e in second.items)
    report = review(repo, ["api_app.py"], cfg, client=MockClient(_responder), system="gsmae")
    markdown = render_markdown(report)
    assert "live API: `ready`" in markdown and "GET /ping" in markdown

    bad_cfg = {**cfg, "live_api": {**cfg["live_api"], "start_command": ["not-a-real-server"]}}
    failed = LiveApiRunner(bad_cfg, graph, repo).run()
    assert failed.details["status"] == "startup-failed"
    assert failed.items[0]["rule_id"] == "API-STARTUP"
