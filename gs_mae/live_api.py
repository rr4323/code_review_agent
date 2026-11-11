"""Opt-in, local live API exercise and profiling for scoped web modules."""

from __future__ import annotations

import cProfile
import hashlib
import json
import os
import pstats
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from gs_mae.graph_builder import ScopedGraph


@dataclass
class LiveApiResult:
    items: List[Dict] = field(default_factory=list)
    note: str = ""
    details: Dict = field(default_factory=dict)


class LiveApiRunner:
    """Starts a configured local service and drives safe OpenAPI operations.

    The caller converts the returned dictionaries to EvidenceItem instances to
    avoid a dependency cycle with evidence_gathering.
    """

    def __init__(self, cfg: Dict, graph: ScopedGraph, repo_root: Path):
        self.cfg = cfg
        self.graph = graph
        self.repo_root = Path(repo_root).resolve()
        self.options = cfg.get("live_api", {}) or {}

    def run(self) -> LiveApiResult:
        if not self.options.get("enabled", False):
            return LiveApiResult(note="live API profiling disabled",
                                 details={"status": "disabled", "command": []})
        command = self._command()
        if not command:
            return LiveApiResult(note="live API enabled but no start_command configured",
                                 details={"status": "configuration-error", "command": []})

        with tempfile.TemporaryDirectory(prefix="gsmae_api_") as temp:
            work_repo = Path(temp) / "repo"
            shutil.copytree(
                self.repo_root, work_repo,
                ignore=shutil.ignore_patterns("__pycache__", ".git", ".venv", "venv", "node_modules", "*.pyc"),
            )
            profile_path = Path(temp) / "server.prof"
            profiled_command, profiling_enabled = self._profile_command(command, profile_path)
            try:
                proc = self._start(profiled_command, work_repo)
            except OSError as exc:
                return LiveApiResult(
                    items=[self._item(self.graph.root, "API-STARTUP", "high", 1,
                                      f"Could not start service: {exc}", "live-api")],
                    note=f"service start failed: {exc}",
                    details={"status": "startup-failed", "command": profiled_command},
                )
            try:
                base_url = str(self.options.get("base_url", "http://127.0.0.1:8000")).rstrip("/")
                health = self._url(base_url, str(self.options.get("health_path", "/health")))
                ready, readiness_note = self._wait_ready(health, proc)
                details = {
                    "status": "ready" if ready else "startup-failed",
                    "command": profiled_command,
                    "health_url": health,
                    "profiling_enabled": profiling_enabled,
                }
                if not ready:
                    return LiveApiResult(
                        items=[self._item(self.graph.root, "API-STARTUP", "high", 1,
                                          f"Service did not become ready: {readiness_note}", "live-api")],
                        note=readiness_note,
                        details={**details, "server_output": self._output_tail(proc)},
                    )
                openapi_url = self._url(base_url, str(self.options.get("openapi_path", "/openapi.json")))
                try:
                    spec = self._json_get(openapi_url, int(self.options.get("request_timeout_s", 10)))
                except Exception as exc:
                    return LiveApiResult(
                        items=[self._item(self.graph.root, "API-OPENAPI", "high", 1,
                                          f"Could not fetch OpenAPI document: {exc}", "live-api")],
                        note=f"OpenAPI fetch failed: {exc}",
                        details={**details, "openapi_url": openapi_url, "server_output": self._output_tail(proc)},
                    )
                return self._exercise(base_url, spec, proc, profile_path, details)
            finally:
                self._stop(proc)

    def _command(self) -> List[str]:
        value = self.options.get("start_command", [])
        if isinstance(value, str):
            import shlex
            return shlex.split(value)
        return [str(v) for v in value]

    def _profile_command(self, command: List[str], profile_path: Path) -> Tuple[List[str], bool]:
        """cProfile can wrap Python module/script commands, but not arbitrary binaries."""
        executable = Path(command[0]).name.lower() if command else ""
        is_python = command and (command[0] == sys.executable or executable.startswith("python"))
        if is_python:
            return [command[0], "-m", "cProfile", "-o", str(profile_path), *command[1:]], True
        return command, False

    def _start(self, command: Sequence[str], cwd: Path) -> subprocess.Popen:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HOME": str(cwd / ".gsmae-home"),
            "TMPDIR": str(cwd / ".gsmae-tmp"),
        }
        Path(env["HOME"]).mkdir(exist_ok=True)
        Path(env["TMPDIR"]).mkdir(exist_ok=True)
        return subprocess.Popen(
            list(command), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, start_new_session=True,
        )

    def _wait_ready(self, health_url: str, proc: subprocess.Popen) -> Tuple[bool, str]:
        deadline = time.monotonic() + float(self.options.get("startup_timeout_s", 20))
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False, f"server exited with code {proc.returncode}"
            try:
                status, _ = self._request("GET", health_url, int(self.options.get("request_timeout_s", 3)))
                if 200 <= status < 500:
                    return True, ""
            except OSError:
                pass
            time.sleep(0.1)
        return False, f"health endpoint did not respond within {self.options.get('startup_timeout_s', 20)}s"

    def _exercise(self, base_url: str, spec: Dict, proc: subprocess.Popen, profile_path: Path, details: Dict) -> LiveApiResult:
        reps = max(1, int(self.options.get("repetitions", 3)))
        warmups = max(0, int(self.options.get("warmup_runs", 1)))
        timeout = int(self.options.get("request_timeout_s", 10))
        max_operations = max(1, int(self.options.get("max_operations", 20)))
        operations, skipped = self._operations(spec)
        metrics, items = [], []
        request_profiles = {}
        for operation in operations[:max_operations]:
            path, method, op, module_id = operation
            request_path = self._substitute(path, op)
            if request_path is None:
                skipped.append({"operation_id": op.get("operationId", f"{method} {path}"),
                                "reason": "required path parameter has no example or default"})
                continue
            url = self._url(base_url, request_path)
            for _ in range(warmups):
                try:
                    self._request(method, url, timeout)
                except OSError:
                    break
            timings, statuses = [], []
            before = self._process_metrics(proc.pid)
            for _ in range(reps):
                t0 = time.perf_counter()
                profiler = cProfile.Profile()
                profiler.enable()
                try:
                    status, _ = self._request(method, url, timeout)
                except OSError as exc:
                    status = 599
                    items.append(self._item(module_id, "API-REQUEST", "high", self._module_line(module_id),
                                            f"{method} {path} could not be requested: {exc}", "live-api"))
                finally:
                    profiler.disable()
                timings.append((time.perf_counter() - t0) * 1000)
                statuses.append(status)
                request_profiles[f"{method} {path}"] = profiler
            after = self._process_metrics(proc.pid)
            endpoint = f"{method} {path}"
            current = {
                "operation_id": op.get("operationId", endpoint), "endpoint": endpoint, "module_id": module_id,
                "median_ms": round(statistics.median(timings), 3), "p95_ms": round(self._p95(timings), 3),
                "statuses": statuses, **self._metric_delta(before, after),
            }
            metrics.append(current)
            if any(s >= 400 for s in statuses):
                items.append(self._item(module_id, "API-REQUEST", "high", self._module_line(module_id),
                                        f"{endpoint} returned {statuses}", "live-api", current))
        # cProfile writes its output when the server process exits.
        server_output = self._stop(proc)
        profile = self._profile_summary(profile_path)
        threshold = float(self.options.get("slow_endpoint_ms", 500))
        for metric in metrics:
            if metric["median_ms"] >= threshold:
                # A server profile is available for Python start commands that
                # exit cleanly. The request profile is a reliable fallback for
                # externally launched servers that cannot be instrumented.
                endpoint_profile = profile or self._profile_object_summary(request_profiles.get(metric["endpoint"]))
                if endpoint_profile:
                    metric["profile"] = endpoint_profile
                    if not profile:
                        metric["profile_scope"] = "request-client"
        if not profile:
            profile = next((m["profile"] for m in metrics if m.get("profile")), [])
        items.extend(self._baseline_items(metrics))
        details.update({
            "openapi_operations": len(operations) + len(skipped), "executed_operations": metrics,
            "skipped_operations": skipped, "profile": profile, "server_output": server_output,
        })
        return LiveApiResult(items, "", details)

    def _operations(self, spec: Dict) -> Tuple[List[Tuple[str, str, Dict, str]], List[Dict]]:
        mapped, skipped = [], []
        safe_methods = {str(m).upper() for m in self.options.get("methods", ["GET", "HEAD"])}
        explicitly_safe = set(self.options.get("safe_operation_ids", []))
        for path, path_item in (spec.get("paths") or {}).items():
            for method, operation in (path_item or {}).items():
                if method.upper() not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"} or not isinstance(operation, dict):
                    continue
                opid = operation.get("operationId", f"{method.upper()} {path}")
                if method.upper() not in safe_methods:
                    skipped.append({"operation_id": opid, "reason": "method not enabled"})
                    continue
                if method.upper() not in {"GET", "HEAD"} and opid not in explicitly_safe:
                    skipped.append({"operation_id": opid, "reason": "unsafe HTTP method"})
                    continue
                mid = self._module_for_operation(opid)
                if not mid:
                    skipped.append({"operation_id": opid, "reason": "does not map to a scoped module"})
                    continue
                mapped.append((path, method.upper(), operation, mid))
        return mapped, skipped

    def _module_for_operation(self, operation_id: str) -> Optional[str]:
        configured = (self.options.get("operation_modules", {}) or {}).get(operation_id)
        if configured in self.graph.modules:
            return configured
        suffix = operation_id.split(".")[-1]
        for mid, module in self.graph.modules.items():
            if any(c.name.split(".")[-1] == suffix for c in module.contracts):
                return mid
        return None

    def _substitute(self, path: str, operation: Dict) -> Optional[str]:
        params = operation.get("parameters", []) or []
        query = []
        for parameter in params:
            where = parameter.get("in")
            if where not in ("path", "query"):
                continue
            schema = parameter.get("schema", {}) or {}
            values = schema.get("enum") or []
            value = parameter.get("example", schema.get("example", parameter.get("example", values[0] if values else None)))
            if value is None:
                if parameter.get("required", False):
                    return None
                continue
            if where == "path":
                path = path.replace("{" + str(parameter.get("name")) + "}", urllib.parse.quote(str(value), safe=""))
            else:
                query.append((str(parameter.get("name")), str(value)))
        if "{" in path:
            return None
        return path + (("?" + urllib.parse.urlencode(query)) if query else "")

    @staticmethod
    def _url(base: str, path: str) -> str:
        return base + (path if path.startswith("/") else "/" + path)

    @staticmethod
    def _request(method: str, url: str, timeout: int) -> Tuple[int, bytes]:
        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _json_get(self, url: str, timeout: int) -> Dict:
        status, body = self._request("GET", url, timeout)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
            raise ValueError("OpenAPI document has no paths object")
        return data

    @staticmethod
    def _process_metrics(pid: int) -> Dict[str, float]:
        try:
            stat = (Path(f"/proc/{pid}/stat").read_text()).split()
            status = Path(f"/proc/{pid}/status").read_text()
            rss = next((int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:")), 0)
            ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            return {"cpu_s": (int(stat[13]) + int(stat[14])) / ticks, "rss_kb": rss}
        except (FileNotFoundError, IndexError, ValueError):
            return {"cpu_s": 0.0, "rss_kb": 0.0}

    @staticmethod
    def _metric_delta(before: Dict[str, float], after: Dict[str, float]) -> Dict[str, float]:
        return {"cpu_s": round(max(0.0, after["cpu_s"] - before["cpu_s"]), 4), "rss_kb": after["rss_kb"]}

    @staticmethod
    def _p95(values: List[float]) -> float:
        return sorted(values)[min(len(values) - 1, max(0, int(len(values) * .95) - 1))]

    def _baseline_items(self, metrics: List[Dict]) -> List[Dict]:
        base_path = self._baseline_path()
        try:
            baseline = json.loads(base_path.read_text()) if base_path.exists() else {}
        except json.JSONDecodeError:
            baseline = {}
        threshold = float(self.options.get("regression_threshold_percent", 20))
        items = []
        current = {m["operation_id"]: m["median_ms"] for m in metrics}
        for metric in metrics:
            old = baseline.get(metric["operation_id"])
            if isinstance(old, (int, float)) and old > 0:
                delta = 100 * (metric["median_ms"] - old) / old
                if delta > threshold:
                    items.append(self._item(
                        metric["module_id"], "PERF-REGRESSION", "high", self._module_line(metric["module_id"]),
                        f"{metric['endpoint']}: median latency +{delta:.1f}% vs baseline (threshold {threshold}%).",
                        "live-api", {**metric, "latency_delta_pct": round(delta, 2)},
                    ))
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(json.dumps(current, indent=2))
        return items

    def _baseline_path(self) -> Path:
        configured = self.options.get("baseline_path")
        if configured:
            return Path(configured).expanduser().resolve()
        repo_key = hashlib.sha256(str(self.repo_root).encode()).hexdigest()[:12]
        return Path.home() / ".cache" / "gsmae" / "live_api" / f"{repo_key}.json"

    def _module_line(self, module_id: str) -> int:
        mod = self.graph.modules.get(module_id)
        if mod and mod.contracts:
            return mod.contracts[0].lineno
        return 1

    def _item(self, module_id: str, rule_id: str, severity: str, line: int, message: str, tool: str,
              metrics: Optional[Dict] = None) -> Dict:
        return {"module_id": module_id, "rule_id": rule_id, "severity": severity, "line": line,
                "message": message, "tool": tool, "metrics": metrics or {}}

    @staticmethod
    def _profile_summary(path: Path) -> List[Dict]:
        if not path.exists():
            return []
        stats = pstats.Stats(str(path))
        out = []
        for (filename, lineno, name), data in sorted(stats.stats.items(), key=lambda x: x[1][3], reverse=True)[:10]:
            out.append({"function": f"{name} ({Path(filename).name}:{lineno})", "cumtime_s": round(data[3], 4),
                        "calls": data[0]})
        return out

    @staticmethod
    def _profile_object_summary(profiler: Optional[cProfile.Profile]) -> List[Dict]:
        if profiler is None:
            return []
        out = []
        for data in sorted(profiler.getstats(), key=lambda stat: stat.inlinetime, reverse=True)[:10]:
            code = data.code
            if isinstance(code, str):
                label = code
            else:
                label = f"{code.co_name} ({Path(code.co_filename).name}:{code.co_firstlineno})"
            out.append({"function": label, "cumtime_s": round(data.totaltime, 4), "calls": data.callcount})
        return out

    @staticmethod
    def _output_tail(proc: subprocess.Popen) -> str:
        # Reading a live pipe can block; normal completed runs include the tail
        # after shutdown. Startup errors still retain their status/return code.
        return ""

    @staticmethod
    def _stop(proc: subprocess.Popen) -> str:
        if proc.poll() is None:
            try:
                # Python's cProfile writes its stats during normal interpreter
                # shutdown. SIGINT lets typical development servers exit through
                # KeyboardInterrupt rather than losing the profile to SIGTERM.
                os.killpg(proc.pid, signal.SIGINT)
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
        try:
            stdout, _ = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            return ""
        lines = (stdout or "").strip().splitlines()
        return "\n".join(lines[-3:])
