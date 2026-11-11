# Graph-Scoped Multi-Agent Execution (GS-MAE) for Gemma 4

**GS-MAE** is an offline-capable, topology-aware multi-agent code comprehension, review, and repair system built for **Gemma 4** (`gemma4:e2b-it-qat` via Ollama) on consumer hardware.

When a code change occurs (`git diff`), GS-MAE:
1. **Builds an Adapter-Based Call-Graph Snapshot & Downward Cone (`gs_mae/lang_adapters.py`, `gs_mae/graph_builder.py`):** Separates module name-binding `imports` from AST-resolved `calls` across pluggable language adapters (`PythonAdapter`, `NotebookAdapter`, and opt-in `Rust`/`Go`/`C++` adapters), persists a path-indexed call-graph snapshot (`<cache>.graph.json`), re-parses only modified root modules (`ScopedGraph.roots`) on subsequent merge requests to filter their downward call cone (`Parent -> Child -> Grandchild`), excludes both unrelated distractors and uncalled imports, extracts public `FunctionContract` signatures (`may_return_none`, `raises`, `annotated`), and computes bottom-up execution waves (`Wave 1 -> Wave 2 -> ... -> Root`) with **Merkle-Tree Graph Caching**.
2. **Executes Workflow 1 — Evidence Gathering Agent (`gs_mae/evidence_gathering.py`):** Discovers enabled categories from [`gs_mae_config.yaml`](gs_mae_config.yaml) (`performance`, `security`, `code_quality`, `architecture`, `testing`, `correctness`) and runs deterministic tools in parallel (`bandit`, `pytest` + entry-point coverage, AST N+1 SQL-in-loop detector `PERF-NPLUS1`, layer policy engine `ARCH-001` & `FW-001`, and annotation verifier `LANG-001`), indexing every finding by `(module_id, category)` inside `UnifiedEvidenceStore`.
3. **Executes Workflow 2 — Synchronous Bottom-Up Wave Execution with Isolated Workspaces (`gs_mae/workspace.py`, `gs_mae/agents.py`):** Walks the module dependency graph wave by wave (`Wave 1 -> Wave 2 -> ... -> Root`). Each node and category subagent operates within its own dedicated isolated workspace (`workspaces/<agent_id>/`), providing complete auditability of everything the agent wrote (prompts, raw LLM completions, findings, artifacts, activity logs), and recursively merges dependent **Child Module Node Evaluations** (including exported `FunctionContract` invariants and `risks_for_callers`) bottom-up into **Parent Module Nodes** up to the Root Changed Modules (`graph.roots`).
4. **Synthesizes & Verifies Repair Patches (`gs_mae/repair.py`):** Generates a minimal `search`/`replace` patch for the highest-severity root defect, records patch artifacts in the dedicated repair workspace, builds a unified git diff, and executes the repository test suite (plus hidden regression tests) in an isolated copy.

---

## Repository Structure

| Path | Description |
| :--- | :--- |
| [`run_gs_mae.py`](run_gs_mae.py) | Top-level CLI application entrypoint supporting repository target, username/password auth, interactive prompts, and review execution |
| [`gs_mae/__main__.py`](gs_mae/__main__.py) | CLI module (`python -m gs_mae`) with repository resolution, interactive credential prompts, and full multi-agent orchestration |
| [`gs_mae/auth.py`](gs_mae/auth.py) | User authentication, credential validation, remote git clone with auth, and `UserSession` tracking |
| [`gs_mae/workspace.py`](gs_mae/workspace.py) | Isolated `AgentWorkspace` and `WorkspaceManager` tracking all files, prompts, responses, artifacts, and activity logs per agent |
| [`gs_mae_config.yaml`](gs_mae_config.yaml) | Declarative YAML configuration (model runtime, categories, performance thresholds, `ARCH-001`/`FW-001`/`LANG-001` rules, quality gates) |
| [`gs_mae/config.py`](gs_mae/config.py) | YAML configuration loader, override handler, and SHA-256 `config_fingerprint` |
| [`gs_mae/lang_adapters.py`](gs_mae/lang_adapters.py) | Pluggable `LanguageAdapter` registry (`PythonAdapter`, `NotebookAdapter`, `RustAdapter`, `GoAdapter`, `CppAdapter`) separating name-binding `imports` from resolved `calls` |
| [`gs_mae/graph_builder.py`](gs_mae/graph_builder.py) | `ModuleGraphBuilder` with path-indexed lazy parsing, `.graph.json` call-graph snapshot, downward call-cone filter, `FunctionContract` extractor, bottom-up wave scheduler, and `MerkleGraphCache` |
| [`gs_mae/evidence_gathering.py`](gs_mae/evidence_gathering.py) | Workflow 1 `EvidenceGatheringAgent` & `UnifiedEvidenceStore` (`bandit`, `pytest`, `PERF-NPLUS1`, `ARCH-001`, `FW-001`, `LANG-001`, `TEST-001`) |
| [`gs_mae/prompts.py`](gs_mae/prompts.py) | Prefix-shared prompt builders (`module_prefix`), AST contract-anomaly pairing (`extract_contract_anomalies`), and JSON schemas |
| [`gs_mae/agents.py`](gs_mae/agents.py) | Workflow 2 `CategorySubagent`, `ModuleGraphNodeAgent`, and `GraphExecutor` integrating topological bottom-up waves and isolated workspaces |
| [`gs_mae/repair.py`](gs_mae/repair.py) | `RepairAgent` with workspace logging, indentation-safe `apply_search_replace`, `unified_diff`, and sandboxed `pytest` verification |
| [`gs_mae/sandbox.py`](gs_mae/sandbox.py) | Defense-in-depth `SandboxConfig`, `validate_patch_ast` static AST gate, and `run_python_sandboxed` (`0o700` workspace, scrubbed env, blocked sockets/outside writes, `RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_FSIZE`) |
| [`gs_mae/llm.py`](gs_mae/llm.py) | `OllamaClient` (records per-call token/prefill/decode telemetry in `CallLedger`) and unit-test `MockClient` |
| [`gs_mae/pipeline.py`](gs_mae/pipeline.py) | End-to-end `review()` (passing companion `.graph.json` beside `cache_path`, recording workspace manifests and user sessions) and `render_markdown()` pipeline |
| [`gs_mae/graph_grpo_trainer.py`](gs_mae/graph_grpo_trainer.py) | `GraphGRPORewardEngine` computing execution-verified composite rewards ($R_{\text{total}}$) and group advantages ($A_i$) |
| [`gs_mae/monitor.py`](gs_mae/monitor.py) | Live & post-hoc parameter and hardware telemetry monitor across all pipeline sections |
| [`run_gs_mae_demo.py`](run_gs_mae_demo.py) | CLI script running live end-to-end Gemma 4 review, repair, and warm Merkle-cache verification |
| [`benchmark/generate.py`](benchmark/generate.py) | Generator and self-verifier for the **CrossMod-Bench** multi-module benchmark (`benchmark/instances/`) |
| [`benchmark/run_eval.py`](benchmark/run_eval.py) | Resumable multi-system evaluation harness (`tools_only`, `flat`, `gsmae_no_msg`, `gsmae`) with `/proc` RSS sampling |
| [`benchmark/metrics.py`](benchmark/metrics.py) | Scorer and aggregator producing `scores_rows.json` and `summary.json` |
| [`tests/test_gs_mae.py`](tests/test_gs_mae.py) | Unit test suite covering call-graph snapshots, cone filtering, multi-language adapters, contracts, Merkle caching, tools, bottom-up merge, ablations, sandbox, repair, and GRPO |
| [`tests/test_cli_and_workspaces.py`](tests/test_cli_and_workspaces.py) | Unit test suite covering CLI authentication, repo resolution, synchronous wave execution, and per-agent workspace traceability |
| [`gemma4_paper_submission.md`](gemma4_paper_submission.md) | Gemma 4 Developer Agent Competition — Paper Track submission |
| [`gs_mae_gemma4_notebook.ipynb`](gs_mae_gemma4_notebook.ipynb) | Reproducible Jupyter Notebook walkthrough and benchmark visualization |

---

## Sequence of Steps to Run, Test, Validate, and Evaluate Each Section

Follow the steps below in exact sequential order from the workspace root (`/home/rajeev-ranjan/rajeev/worker_agent`).

### Step 0: Environment & Dependency Verification

Verify the project virtual environment (`.venv`) and installed packages (`bandit`, `pytest`, `PyYAML`, `requests`, `numpy`, `matplotlib`):

```bash
.venv/bin/python --version
.venv/bin/pip list
```

If setting up from scratch:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

### Step 1: Verify Local Ollama Server & `gemma4:e2b-it-qat` Model

Verify that the local Ollama server is running on `127.0.0.1:11434` and that `gemma4:e2b-it-qat` is available:

```bash
# 1. Check if Ollama is responding and gemma4:e2b-it-qat is installed
curl -s http://127.0.0.1:11434/api/tags | .venv/bin/python -m json.tool

# 2. If Ollama is not running, start the daemon in the background:
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_KEEP_ALIVE=60m \
  ~/.local/ollama/bin/ollama serve &
```

---

### Step 2: Run the Automated Unit Test Suite (`pytest`)

Validate all core subsystems (`LanguageAdapter` registry, `ModuleGraphBuilder`, call-graph snapshot & cone filter, `FunctionContract`, `MerkleGraphCache`, `EvidenceGatheringAgent`, `GraphExecutor`, isolated per-agent workspaces, `apply_search_replace`, `sandbox`, and `GraphGRPORewardEngine`) before invoking the live LLM:

```bash
.venv/bin/python -m pytest -v tests/test_cli_and_workspaces.py tests/test_gs_mae.py
```

Expected output: `24 passed` with `0` failures.

---

### Step 2.5: Run the CLI Application with Repository & User Authentication

Run the review directly using the CLI application with repository target, username, and password:

```bash
# 1. Non-interactive CLI with flags:
.venv/bin/python run_gs_mae.py \
  --repo benchmark/instances/cmb-015-garage-argument_order-k1/repo \
  --username alice \
  --password mysecurepassword \
  --system gsmae \
  --repair \
  --out outputs/cli_review

# 2. Interactive CLI (prompts automatically for repo, username, and password if omitted):
.venv/bin/python run_gs_mae.py

# 3. Remote Git repository (clones using authenticated credentials):
.venv/bin/python run_gs_mae.py \
  --repo https://github.com/my-org/my-service.git \
  --username git_user \
  --password git_token \
  --system gsmae
```

Each agent creates its own dedicated workspace under `outputs/cli_review/workspaces/<session_id>/<agent_id>/` with an `activity.log`, `inbox/`, `outbox/`, `prompts/`, `responses/`, and `artifacts/` tracking everything written by each agent with SHA-256 hashes.

---

### Step 3: Validate Each Section Individually

#### Section 3.1 — Validate Configuration & Runtime Parameters (`gs_mae/config.py`)

```bash
.venv/bin/python -m gs_mae.monitor --section config
```

**What to validate:**
- `config_fingerprint` is a deterministic 12-character SHA-256 prefix (`73df130aedee`).
- `enabled_categories` lists all 6 categories (`performance`, `security`, `code_quality`, `architecture`, `testing`, `correctness`).
- `ollama_reachable` is `true` and `gemma4:e2b-it-qat` (`Q4_0`) is loaded.

#### Section 3.2 — Validate Phase 1: Adapter-Based Call-Graph Snapshot, Cone Filter & Contracts (`gs_mae/lang_adapters.py`, `gs_mae/graph_builder.py`)

Run a quick validation on a benchmark repository (`cmb-015-garage-argument_order-k1`) to verify call-graph cone pruning (excluding both unrelated distractors and imported-but-uncalled modules), bottom-up wave scheduling, and AST contract extraction:

```bash
.venv/bin/python -c "
from pathlib import Path
from gs_mae.config import load_config, config_fingerprint
from gs_mae.graph_builder import ModuleGraphBuilder

repo = Path('benchmark/instances/cmb-015-garage-argument_order-k1/repo')
g = ModuleGraphBuilder(repo, config_fingerprint(load_config())).build(['garage/part_controller.py'])
print('Root modules:', g.roots, '| Primary root:', g.root)
print('Total repo modules:', g.all_module_count, '| Scoped call-cone modules:', len(g.modules))
print('Bottom-up execution waves:', g.waves)
for mid, mod in g.modules.items():
    print(f'  {mid:<26} layer={mod.layer:<14} children={mod.children} merkle={mod.merkle_hash}')
    for c in mod.contracts:
        print(f'    contract: {c.signature} | may_return_none={c.may_return_none} | raises={c.raises}')
"
```

**What to validate:**
- `all_module_count` is `8`, while `len(g.modules)` is `4`: distractor modules (`reporting`, `schema`, `distractor_*`) as well as imported-but-uncalled modules (`garage.part_serializer`, which `part_controller.py` imports as `serializer` but never calls) are pruned from the downward call cone.
- `g.waves` orders leaf callees (`garage.part_repository`, `garage.pricing_rules`) in Wave 1, intermediate domain services (`garage.part_service`) in Wave 2, and the changed root (`garage.part_controller`) in Wave 3.
- `get_part` has `may_return_none=True`; `reserve_stock` has `raises=['KeyError', 'OutOfStockError']`.
- Passing `graph_path=Path('/tmp/snap.graph.json')` to `ModuleGraphBuilder` persists the call-graph snapshot so subsequent calls with a merge-request file list refresh only the listed files and filter their downward cone from the stored call graph.

#### Section 3.3 — Validate Workflow 1: Parallel Evidence Gathering Agent (`gs_mae/evidence_gathering.py`)

Verify that real tools (`bandit`, `pytest`, AST `PERF-NPLUS1`, `ARCH-001`, `FW-001`, `LANG-001`, `TEST-001`) execute in parallel and slice findings by `(module_id, category)`:

```bash
.venv/bin/python -c "
import json
from pathlib import Path
from gs_mae.config import load_config, config_fingerprint
from gs_mae.graph_builder import ModuleGraphBuilder
from gs_mae.evidence_gathering import EvidenceGatheringAgent

repo = Path('benchmark/instances/cmb-000-store-none_dereference-k1/repo')
cfg = load_config()
g = ModuleGraphBuilder(repo, config_fingerprint(cfg)).build(['store/product_controller.py'])
store = EvidenceGatheringAgent(cfg, g, repo).run()
print('Tool runs summary:', json.dumps(store.tool_runs, indent=2))
for item in store.items:
    print(f'  [{item.category:<12}] {item.module_id:<26} line={item.line:<3} rule={item.rule_id:<12} {item.message}')
"
```

**What to validate:**
- All 6 category entries appear in `store.tool_runs` with real execution times (`seconds`).
- Injected tool defects (`BANDIT-B608`, `PERF-NPLUS1`, `FW-001`, `ARCH-001`, `LANG-001`, `TEST-001`) are mapped to their exact owning `module_id` and line number.

#### Section 3.4 — Validate Workflow 2 + Live Repair + Merkle Cache (`run_gs_mae_demo.py`)

Run the end-to-end live Gemma 4 execution on an instance and verify both cold execution (spawning category subagents, merging bottom-up, synthesizing & testing the repair patch) and warm Merkle cache execution:

```bash
.venv/bin/python run_gs_mae_demo.py \
  --instance benchmark/instances/cmb-015-garage-argument_order-k1 \
  --out outputs/demo_verify
```

Inspect the generated reports:
- Markdown report: [`outputs/demo_verify/review_report.md`](outputs/demo_verify/review_report.md)
- Full telemetry JSON: [`outputs/demo_verify/review_report.json`](outputs/demo_verify/review_report.json)

**What to validate:**
- Wave 3 (`garage.part_controller`) spawns `['correctness', 'code_quality', 'testing']` and merges child node evaluations from `['garage.part_repository', 'garage.part_serializer', 'garage.part_service', 'garage.pricing_rules']`.
- Detects `critical | argument_order | garage/part_controller.py:23`.
- Synthesizes a unified diff swapping `(payee_id, payer_id)` $\to$ `(payer_id, payee_id)` and passes `5/5` tests (`applied: True, tests passed: True`) inside the isolated execution sandbox.
- Warm Merkle Cache Re-Run reports `5/5 modules cached, 0 LLM calls`.

#### Section 3.5 — Validate Execution Sandbox & Static AST Patch Gate (`gs_mae/sandbox.py`)

Verify that the static AST gate blocks syntax errors and unsafe imports/calls, and that `run_python_sandboxed` scrubs host environment secrets, blocks outbound sockets, blocks outside-workspace writes, applies POSIX `RLIMIT_*` limits, and leaves the host repository unmodified:

```bash
.venv/bin/python -c "
import json
from pathlib import Path
from gs_mae.sandbox import SandboxConfig, run_python_sandboxed, validate_patch_ast

print('1. Static AST Gate (unsafe import check):')
print(json.dumps(validate_patch_ast('def f(): return 1\n', 'import subprocess\ndef f(): return 1\n'), indent=2))

print('2. Sandboxed Execution Telemetry:')
repo = Path('benchmark/instances/cmb-015-garage-argument_order-k1/repo')
sbx = run_python_sandboxed(repo, ['-m', 'pytest', '-q', '--no-header', '-p', 'no:cacheprovider'], SandboxConfig())
print(json.dumps({'passed': sbx.passed, 'tail': sbx.tail, 'telemetry': sbx.telemetry}, indent=2))
"
```

---

### Step 4: Generate & Verify the CrossMod-Bench Dataset (`benchmark/generate.py`)

CrossMod-Bench generates 24 test instances across 4 domains (`store`, `library`, `boxoffice`, `garage`), 4 cross-module semantic bug classes (`none_dereference`, `unit_mismatch`, `exception_mismatch`, `argument_order`) plus clean `control` commits, and distractor counts $k \in \{1, 5\}$. Every generated instance is automatically verified so that visible tests pass on the buggy commit, hidden regression tests fail on the buggy commit, and both pass on the gold fix:

```bash
.venv/bin/python -m benchmark.generate --out benchmark/instances --seed 2026
```

---

### Step 5: Run Multi-System Benchmark Evaluation (`benchmark/run_eval.py`)

Run all 4 systems (`tools_only`, `flat`, `gsmae_no_msg`, `gsmae`) on CrossMod-Bench using real `gemma4:e2b-it-qat`. The runner is **resumable** (existing `{instance_id}__{system}.json` files in `benchmark/results/gemma4_e2b-it-qat/` are automatically skipped) and orders instances round-robin across bug classes so partial runs (`--limit N`) remain balanced:

```bash
# Balanced 5-instance evaluation (1 of each semantic bug class + 1 control across all 4 systems = 20 runs)
.venv/bin/python -m benchmark.run_eval \
  --instances benchmark/instances \
  --out benchmark/results \
  --limit 5

# Full 24-instance evaluation (all 24 instances x 4 systems = 96 runs)
.venv/bin/python -m benchmark.run_eval \
  --instances benchmark/instances \
  --out benchmark/results
```

---

### Step 6: Score & Aggregate Benchmark Metrics (`benchmark/metrics.py`)

Compute per-instance scores (`benchmark/results/gemma4_e2b-it-qat/scores_rows.json`) and aggregated system metrics (`benchmark/results/gemma4_e2b-it-qat/summary.json`):

```bash
.venv/bin/python -m benchmark.metrics --results benchmark/results/gemma4_e2b-it-qat
```

---

## Monitoring Every Parameter Across All Sections (`gs_mae/monitor.py`)

We provide a dedicated parameter and telemetry monitor (`python -m gs_mae.monitor`) to inspect and track every configuration, graph, tool, LLM KV-cache, memory, repair, and evaluation parameter.

### Quick Monitoring Commands

```bash
# Monitor all 5 sections at once
.venv/bin/python -m gs_mae.monitor --section all

# Monitor a single section
.venv/bin/python -m gs_mae.monitor --section config
.venv/bin/python -m gs_mae.monitor --section graph    --report outputs/demo_verify/review_report.json
.venv/bin/python -m gs_mae.monitor --section evidence --report outputs/demo_verify/review_report.json
.venv/bin/python -m gs_mae.monitor --section llm      --report outputs/demo_verify/review_report.json
.venv/bin/python -m gs_mae.monitor --section eval     --results benchmark/results/gemma4_e2b-it-qat

# Live continuous terminal dashboard (refreshes every 10 seconds during benchmark runs)
.venv/bin/python -m gs_mae.monitor --section eval --results benchmark/results/gemma4_e2b-it-qat --watch 10
```

---

### Complete Parameter Reference by Section

#### 1. Configuration, Model Runtime & Hardware Parameters (`--section config`)

| Parameter | Source | Value / Meaning |
| :--- | :--- | :--- |
| `config_fingerprint` | `gs_mae/config.py` | 12-char SHA-256 digest of rules, categories, spawn policy, and model (`73df130aedee`); folded into Merkle node hashes |
| `model_runtime.model` | `gs_mae_config.yaml` | Ollama model tag (`gemma4:e2b-it-qat`, 4-bit QAT edge model) |
| `model_runtime.num_ctx` | `gs_mae_config.yaml` | Context window size (`8192` tokens) |
| `model_runtime.temperature` | `gs_mae_config.yaml` | Sampling temperature (`0.0` for deterministic decoding) |
| `model_runtime.seed` | `gs_mae_config.yaml` | Random seed (`7`) |
| `model_runtime.max_output_tokens` | `gs_mae_config.yaml` | Max completion budget per subagent call (`384` tokens) |
| `spawn_policy` | `gs_mae_config.yaml` | `evidence_gated` (skips LLM calls on clean tool categories/nodes) or `all` |
| `ollama_total_rss_mb` | `/proc/<pid>/status` | Combined resident memory (`VmRSS`) of `ollama` server + `llama-server` runner (~`4,607–4,685 MB`) |
| `cpu_load_avg_1m_5m_15m` | `os.getloadavg()` | Live 1m / 5m / 15m Linux CPU load averages |

#### 2. Module Dependency Graph & Merkle Cache Parameters (`--section graph`)

| Parameter | Source | Value / Meaning |
| :--- | :--- | :--- |
| `root_module` | `ModuleGraphBuilder` | Target module owning the modified file from `git diff` |
| `repo_modules_total` | `ModuleGraphBuilder` | Total Python modules discovered in the repository |
| `scoped_modules_count` | `ModuleGraphBuilder` | Modules reachable in the downward dependency cone of `root_module` |
| `pruned_distractor_modules` | `ModuleGraphBuilder` | Unrelated repository modules excluded from agent execution (`repo_modules_total - scoped_modules_count`) |
| `waves` | `ModuleGraphBuilder` | Topological bottom-up execution schedule (`Wave 1` leaves $\to$ `Root`) |
| `merkle_hash` | `ModuleNode` | 16-char SHA-256 over module source + sorted child Merkle hashes + `config_fingerprint` |
| `may_return_none_funcs` | `FunctionContract` | Functions in the module whose AST can return `None` |
| `raises_funcs` | `FunctionContract` | Mapping of function names to explicitly raised exception classes |
| `cache_hits_cold` / `warm_rerun` | `MerkleGraphCache` | Number of module nodes served directly from the Merkle cache (`5/5` on unchanged re-runs with `0` LLM calls) |

#### 3. Workflow 1: Evidence Gathering Parameters (`--section evidence`)

| Parameter | Source | Value / Meaning |
| :--- | :--- | :--- |
| `evidence_wall_seconds` | `EvidenceGatheringAgent` | Wall-clock time for parallel execution of all enabled category tools (~`0.29s`) |
| `tool_runs_by_category[c].tool` | `UnifiedEvidenceStore` | Underlying tool executed (`bandit`, `pytest + entry-point coverage`, `ast-query-in-loop`, `ast-rule-engine`, `ast-annotation-check`) |
| `tool_runs_by_category[c].items` | `UnifiedEvidenceStore` | Number of raw evidence items captured for category `c` |
| `findings_by_rule_id` | `UnifiedEvidenceStore` | Counts per rule (`BANDIT-B608`, `PERF-NPLUS1`, `ARCH-001`, `FW-001`, `LANG-001`, `TEST-001`) |

#### 4. Workflow 2: Node Spawn/Merge & KV-Cache LLM Parameters (`--section llm`)

| Parameter | Source | Value / Meaning |
| :--- | :--- | :--- |
| `spawned_categories` | `ModuleGraphNodeAgent` | Categories that spawned an LLM subagent on this module node |
| `skipped_categories` | `ModuleGraphNodeAgent` | Categories gated off due to zero tool/contract anomalies |
| `children_merged` | `ModuleGraphNodeAgent` | Immediate child module nodes whose `MergedNodeEvaluation` messages were merged into this node |
| `prompt_tokens` / `output_tokens` | `CallLedger` | Exact prompt and completion tokens reported by Ollama per LLM call |
| `prompt_eval_s` / `eval_s` | `CallLedger` | Prompt prefill time (s) and token generation decode time (s) per LLM call |
| `kv_prefix_speedup_x` | `monitor.py` | Prefill speedup ratio on warm category subagent calls sharing `module_prefix` (`10.5x–14.3x` faster prefill vs. Call 1 on the same module) |
| `max_prompt_chars` | `CallLedger` | Largest prompt sent in any single LLM call (bounded in `gsmae` vs. growing with $k$ in `flat`) |
| `json_parse_failures` | `CallLedger` | Count of malformed JSON responses (`0` with schema-constrained decoding) |

#### 5. Benchmark Evaluation, Repair & Graph-GRPO Parameters (`--section eval`)

| Parameter | Source | Value / Meaning |
| :--- | :--- | :--- |
| `recall_strict` | `benchmark/metrics.py` | Fraction of bug instances where the exact `bug_class`, `bug_file`, and `bug_line` ($\pm 3$ lines) were identified |
| `recall_file` | `benchmark/metrics.py` | Fraction of bug instances where the exact `bug_class` and `bug_file` were identified |
| `semantic_fp_per_instance` | `benchmark/metrics.py` | Mean false-positive semantic bug reports per instance |
| `precision` | `benchmark/metrics.py` | $\text{TP} / (\text{TP} + \text{FP})$ across semantic cross-module findings |
| `tool_bug_recall` | `benchmark/metrics.py` | Recall on injected static/dynamic tool defects (`BANDIT-B608`, `PERF-NPLUS1`, `ARCH-001`, `FW-001`) |
| `repair.resolved_rate_over_bug_instances` | `benchmark/metrics.py` | Fraction of bug instances where `RepairAgent`'s patch passed both visible and hidden regression tests |
| `mean_graph_grpo_reward_by_system` | `GraphGRPORewardEngine` | Mean composite verifiable reward $R_{\text{total}} = 0.3 R_{\text{loc}} + 0.4 R_{\text{test}} + 0.3 R_{\text{rule}} - 0.15 R_{\text{len}}$ |
