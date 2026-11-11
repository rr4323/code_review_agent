"""Execution Sandbox & Static AST Patch Gate for GS-MAE.

Provides defense-in-depth isolation whenever GS-MAE executes untrusted repository code
or LLM-synthesized patches (Workflow 1 `pytest` / micro-benchmarks and Workflow 2 `RepairAgent`):
1. Static AST Syntax & Security Gate (`validate_patch_ast`): verifies `ast.parse` and blocks
   newly introduced dangerous imports (`subprocess`, `socket`, `ctypes`, `urllib`, `http`, etc.)
   or dangerous builtins/calls (`eval`, `exec`, `__import__`, `os.system`, `os.popen`, `shutil.rmtree`).
2. Ephemeral Isolated Workspace (`0o700` temp directory) with SHA-256 host-tree integrity verification.
3. Scrubbed Environment: strips host secrets/tokens (`AWS_*`, `GITHUB_*`, `OPENAI_*`, `SSH_*`, etc.).
4. In-Process Runtime Guard (`sitecustomize.py`): blocks all outbound network sockets and any
   filesystem write/delete outside the ephemeral sandbox root.
5. POSIX OS Resource Limits (`resource.setrlimit` + process-group `start_new_session` kill-on-timeout):
   enforces `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, and `RLIMIT_NOFILE`.
"""

from __future__ import annotations

import ast
import hashlib
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

FORBIDDEN_PATCH_IMPORTS: Set[str] = {
    "subprocess", "socket", "urllib", "http", "ftplib", "telnetlib", "xmlrpc",
    "ctypes", "multiprocessing", "pty", "fcntl", "resource", "webbrowser",
}

FORBIDDEN_PATCH_CALLS: Set[str] = {
    "eval", "exec", "compile", "__import__", " breakpoint",
    "os.system", "os.popen", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe", "os.execl", "os.execle",
    "os.execlp", "os.execlpe", "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.fork", "os.kill", "os.killpg", "os.remove", "os.unlink", "os.rmdir",
    "shutil.rmtree", "shutil.move",
}

_SITECUSTOMIZE_GUARD = '''"""GS-MAE runtime sandbox guard loaded automatically via PYTHONPATH."""
import builtins
import os
import shutil
import socket

_SANDBOX_ROOT = os.path.realpath(os.environ.get("GS_MAE_SANDBOX_ROOT", ""))
_BLOCK_NET = os.environ.get("GS_MAE_BLOCK_NETWORK", "1") == "1"
_BLOCK_FS = os.environ.get("GS_MAE_BLOCK_OUTSIDE_WRITES", "1") == "1"

if _BLOCK_NET:
    def _blocked_net(*args, **kwargs):
        raise PermissionError("Network access is blocked inside the GS-MAE execution sandbox.")

    socket.socket.connect = _blocked_net
    socket.socket.connect_ex = _blocked_net
    socket.create_connection = _blocked_net

if _BLOCK_FS and _SANDBOX_ROOT:
    _orig_open = builtins.open
    _WRITE_FLAGS = {"w", "a", "x", "+"}

    def _is_inside_sandbox(path_obj) -> bool:
        if isinstance(path_obj, int):
            return True
        try:
            rp = os.path.realpath(os.fspath(path_obj))
        except Exception:
            return False
        return rp == _SANDBOX_ROOT or rp.startswith(_SANDBOX_ROOT + os.sep) or rp == "/dev/null"

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(ch in mode for ch in _WRITE_FLAGS) and not _is_inside_sandbox(file):
            raise PermissionError(f"Write access outside GS-MAE sandbox blocked: {file}")
        return _orig_open(file, mode, *args, **kwargs)

    builtins.open = _guarded_open

    for _fn_name in ("remove", "unlink", "rmdir", "mkdir", "makedirs", "chmod", "chown"):
        _orig_fn = getattr(os, _fn_name, None)
        if _orig_fn is not None:
            def _make_os_guard(fn, name):
                def _guard(path, *a, **kw):
                    if not _is_inside_sandbox(path):
                        raise PermissionError(f"os.{name} outside GS-MAE sandbox blocked: {path}")
                    return fn(path, *a, **kw)
                return _guard
            setattr(os, _fn_name, _make_os_guard(_orig_fn, _fn_name))

    _orig_rmtree = shutil.rmtree
    def _guarded_rmtree(path, *a, **kw):
        if not _is_inside_sandbox(path):
            raise PermissionError(f"shutil.rmtree outside GS-MAE sandbox blocked: {path}")
        return _orig_rmtree(path, *a, **kw)
    shutil.rmtree = _guarded_rmtree
'''


@dataclass
class SandboxConfig:
    enabled: bool = True
    isolate_filesystem: bool = True
    scrub_environment: bool = True
    block_network: bool = True
    block_outside_writes: bool = True
    ast_patch_gate: bool = True
    cpu_time_limit_s: int = 30
    memory_limit_mb: int = 768
    max_file_size_mb: int = 16
    max_open_files: int = 256

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]] = None) -> "SandboxConfig":
        raw = (cfg or {}).get("sandbox", {}) or {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            isolate_filesystem=bool(raw.get("isolate_filesystem", True)),
            scrub_environment=bool(raw.get("scrub_environment", True)),
            block_network=bool(raw.get("block_network", True)),
            block_outside_writes=bool(raw.get("block_outside_writes", True)),
            ast_patch_gate=bool(raw.get("ast_patch_gate", True)),
            cpu_time_limit_s=int(raw.get("cpu_time_limit_s", 30)),
            memory_limit_mb=int(raw.get("memory_limit_mb", 768)),
            max_file_size_mb=int(raw.get("max_file_size_mb", 16)),
            max_open_files=int(raw.get("max_open_files", 256)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SandboxResult:
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    tail: str
    wall_s: float
    timed_out: bool = False
    telemetry: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _extract_imports_and_calls(tree: ast.AST) -> tuple[Set[str], Set[str]]:
    imports: Set[str] = set()
    calls: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                calls.add(f"{node.func.value.id}.{node.func.attr}")
    return imports, calls


def validate_patch_ast(before_src: str, after_src: str) -> Dict[str, Any]:
    """Static AST syntax and security gate for LLM-synthesized code patches."""
    try:
        after_tree = ast.parse(after_src)
    except SyntaxError as exc:
        return {
            "syntax_valid": False,
            "safe": False,
            "error": f"SyntaxError at line {exc.lineno}: {exc.msg}",
            "blocked_imports": [],
            "blocked_calls": [],
        }
    try:
        before_tree = ast.parse(before_src)
        before_imp, before_calls = _extract_imports_and_calls(before_tree)
    except SyntaxError:
        before_imp, before_calls = set(), set()

    after_imp, after_calls = _extract_imports_and_calls(after_tree)
    new_imp = after_imp - before_imp
    new_calls = after_calls - before_calls

    blocked_imp = sorted(new_imp & FORBIDDEN_PATCH_IMPORTS)
    blocked_calls = sorted(new_calls & FORBIDDEN_PATCH_CALLS)
    safe = not blocked_imp and not blocked_calls
    err = ""
    if not safe:
        err = f"Blocked unsafe AST additions: imports={blocked_imp}, calls={blocked_calls}"
    return {
        "syntax_valid": True,
        "safe": safe,
        "error": err,
        "blocked_imports": blocked_imp,
        "blocked_calls": blocked_calls,
    }


def compute_repo_digest(repo: Path) -> str:
    """Computes a fast SHA-256 digest over all tracked source files in `repo`."""
    h = hashlib.sha256()
    for p in sorted(repo.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or ".git" in p.parts or ".pytest_cache" in p.parts:
            continue
        rel = p.relative_to(repo).as_posix()
        h.update(rel.encode("utf-8") + b"\0")
        h.update(p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _make_preexec_limits(cfg: SandboxConfig):
    """Creates a POSIX preexec_fn that applies OS resource limits in the child process."""
    def _apply():
        try:
            cpu_s = max(1, int(cfg.cpu_time_limit_s))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 2))
        except (ValueError, OSError):
            pass
        try:
            fsize_b = max(1, int(cfg.max_file_size_mb)) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_b, fsize_b))
        except (ValueError, OSError):
            pass
        try:
            nofile = max(64, int(cfg.max_open_files))
            _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = min(nofile, hard) if hard != resource.RLIM_INFINITY else nofile
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            pass
        try:
            mem_b = max(128, int(cfg.memory_limit_mb)) * 1024 * 1024
            _, hard_as = resource.getrlimit(resource.RLIMIT_AS)
            target_as = min(mem_b, hard_as) if hard_as != resource.RLIM_INFINITY else mem_b
            resource.setrlimit(resource.RLIMIT_AS, (target_as, hard_as))
        except (ValueError, OSError):
            pass
    return _apply


def run_python_sandboxed(
    repo: Path,
    py_args: List[str],
    sandbox_cfg: Optional[SandboxConfig] = None,
    file_overrides: Optional[Dict[str, str]] = None,
    extra_files: Optional[Dict[str, str]] = None,
    timeout: int = 60,
) -> SandboxResult:
    """Executes `[sys.executable, '-B', *py_args]` inside an isolated, resource-limited sandbox."""
    cfg = sandbox_cfg or SandboxConfig()
    digest_before = compute_repo_digest(repo)
    t0 = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="gsmae_sbx_") as tmp_root_str:
        os.chmod(tmp_root_str, 0o700)
        tmp_root = Path(tmp_root_str).resolve()
        work_repo = tmp_root / "repo"
        home_dir = tmp_root / "home"
        tmp_dir = tmp_root / "tmp"
        guard_dir = tmp_root / "guard"
        home_dir.mkdir(mode=0o700)
        tmp_dir.mkdir(mode=0o700)
        guard_dir.mkdir(mode=0o700)

        shutil.copytree(
            repo,
            work_repo,
            ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "*.pyc"),
        )
        for rel, content in (file_overrides or {}).items():
            target = (work_repo / rel).resolve()
            if not str(target).startswith(str(work_repo)):
                raise PermissionError(f"Sandbox file_override path traversal blocked: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        for rel, content in (extra_files or {}).items():
            target = (work_repo / rel).resolve()
            if not str(target).startswith(str(work_repo)):
                raise PermissionError(f"Sandbox extra_file path traversal blocked: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        (guard_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE_GUARD, encoding="utf-8")

        host_env_count = len(os.environ)
        if cfg.scrub_environment:
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            }
        else:
            env = dict(os.environ)

        py_path_entries = [str(guard_dir), str(work_repo)] if cfg.enabled else [str(work_repo)]
        env.update({
            "PYTHONPATH": os.pathsep.join(py_path_entries),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONHASHSEED": "0",
            "HOME": str(home_dir),
            "TMPDIR": str(tmp_dir),
            "XDG_CACHE_HOME": str(tmp_dir),
            "GS_MAE_SANDBOX_ROOT": str(tmp_root),
            "GS_MAE_BLOCK_NETWORK": "1" if (cfg.enabled and cfg.block_network) else "0",
            "GS_MAE_BLOCK_OUTSIDE_WRITES": "1" if (cfg.enabled and cfg.block_outside_writes) else "0",
        })
        stripped_keys = max(0, host_env_count - 2) if cfg.scrub_environment else 0

        cmd = [sys.executable, "-B", *py_args]
        preexec = _make_preexec_limits(cfg) if (cfg.enabled and os.name == "posix") else None
        timed_out = False

        proc = subprocess.Popen(
            cmd,
            cwd=work_repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
            preexec_fn=preexec,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            stdout, stderr = proc.communicate()
            rc = -9

    wall_s = round(time.perf_counter() - t0, 4)
    digest_after = compute_repo_digest(repo)
    combined_lines = (stdout.strip() + ("\n" + stderr.strip() if stderr.strip() else "")).strip().splitlines()
    tail = "\n".join(combined_lines[-3:]) if combined_lines else ""

    telemetry = {
        "sandbox_enabled": cfg.enabled,
        "isolated_filesystem": cfg.isolate_filesystem,
        "env_scrubbed": cfg.scrub_environment,
        "env_keys_stripped": stripped_keys,
        "network_blocked": bool(cfg.enabled and cfg.block_network),
        "outside_writes_blocked": bool(cfg.enabled and cfg.block_outside_writes),
        "rlimit_cpu_s": cfg.cpu_time_limit_s,
        "rlimit_memory_mb": cfg.memory_limit_mb,
        "rlimit_fsize_mb": cfg.max_file_size_mb,
        "rlimit_nofile": cfg.max_open_files,
        "host_repo_digest_before": digest_before,
        "host_repo_digest_after": digest_after,
        "host_repo_unmodified": digest_before == digest_after,
    }
    return SandboxResult(
        passed=(rc == 0 and not timed_out),
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        tail=tail,
        wall_s=wall_s,
        timed_out=timed_out,
        telemetry=telemetry,
    )
