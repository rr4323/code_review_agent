"""Local review API.

Start one process, then ask it to review a repository that was cloned by hand
into the configured directory:

    python -m gs_mae.server --repos /path/to/checkouts

    curl -s http://127.0.0.1:8765/repos
    curl -s -X POST http://127.0.0.1:8765/reviews \\
      -H 'Content-Type: application/json' \\
      -d '{"repo":"my-service","system":"tools_only"}'
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from gs_mae.config import load_config
from gs_mae.graph_builder import ModuleGraphBuilder
from gs_mae.llm import make_client
from gs_mae.pipeline import SYSTEMS, render_markdown, review, save_report

_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ReviewError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def find_repo(repos_root: Path, name: str) -> Path:
    """Resolve a single directory name under the checkout root."""
    if not _REPO_NAME.fullmatch(name or ""):
        raise ReviewError(400, "repo must be a directory name, not a path")
    root = Path(repos_root).resolve()
    repo = (root / name).resolve()
    if repo.parent != root or not repo.is_dir():
        raise ReviewError(404, f"no checkout named {name} under {root}")
    return repo


def files_to_review(repo: Path, changed: Optional[List[str]]) -> Tuple[List[str], str]:
    """Use the requested files, otherwise the last commit, otherwise every source file."""
    if changed:
        return list(changed), "request"
    builder = ModuleGraphBuilder(repo)
    try:
        diff = builder.changed_files_from_git()
    except subprocess.CalledProcessError:
        diff = []
    if diff:
        return diff, "git"
    builder._index_paths()
    listed = sorted(rel for _adapter, rels in builder._path_index.values() for rel in rels)
    if not listed:
        raise ReviewError(400, f"{repo.name} has no source files the adapters can review")
    return listed, "all-source"


SERVER_SYSTEMS = ("gsmae", "flat", "gsmae_no_msg")


def run_named_review(repos_root: Path, out_root: Path, body: Dict, config_path: Optional[Path] = None) -> Dict:
    name = str(body.get("repo", ""))
    repo = find_repo(repos_root, name)
    system = str(body.get("system", "gsmae"))
    if system not in SERVER_SYSTEMS:
        raise ReviewError(400, f"system must be one of {', '.join(SERVER_SYSTEMS)} (reviews always run tools + LLM)")
    changed = body.get("changed")
    if changed is not None and not isinstance(changed, list):
        raise ReviewError(400, "changed must be a list of file paths")
    files, source = files_to_review(repo, changed)
    overrides = {}
    if body.get("model"):
        overrides["model_runtime.model"] = body["model"]
    if body.get("backend"):
        overrides["model_runtime.backend"] = body["backend"]
    cfg = load_config(config_path, overrides if overrides else None)
    client = make_client(cfg)
    if not client.health():
        raise ReviewError(503, f"Model {client.model} is not available at {client.url}")
    report_dir = Path(out_root).resolve() / name
    result = review(repo, files, cfg, client, system, cache_path=report_dir / "merkle.json", repair=bool(body.get("repair")))
    save_report(result, report_dir)
    return {
        "repo": name,
        "path": str(repo),
        "file_source": source,
        "changed_files": files,
        "status": result["status"],
        "findings": len(result["findings"]),
        "report_dir": str(report_dir),
        "report_json": str(report_dir / "review_report.json"),
        "report_markdown": str(report_dir / "review_report.md"),
        "markdown": render_markdown(result),
    }


class ReviewServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], repos_root: Path, out_root: Path, config_path: Optional[Path]):
        super().__init__(address, ReviewHandler)
        self.repos_root = Path(repos_root).resolve()
        self.out_root = Path(out_root).resolve()
        self.config_path = config_path
        self.review_lock = threading.Lock()


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, fmt: str, *args) -> None:
        print(f"[gs-mae] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok", "repos_root": str(self.server.repos_root)})
            return
        if path == "/repos":
            names = sorted(p.name for p in self.server.repos_root.iterdir() if p.is_dir() and _REPO_NAME.fullmatch(p.name))
            self._send(200, {"repos": names})
            return
        prefix = "/reports/"
        if path.startswith(prefix):
            try:
                repo = find_repo(self.server.repos_root, path[len(prefix):])
            except ReviewError as exc:
                self._send(exc.status, {"error": str(exc)})
                return
            report = self.server.out_root / repo.name / "review_report.md"
            if not report.is_file():
                self._send(404, {"error": f"no report for {repo.name}"})
                return
            body = report.read_text(encoding="utf-8")
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/reviews":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > 1_000_000:
            self._send(400, {"error": "JSON body is required and must be under 1 MB"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self._send(400, {"error": "body must be JSON"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return
        if not self.server.review_lock.acquire(blocking=False):
            self._send(409, {"error": "a review is already running"})
            return
        try:
            print(f"[gs-mae] reviewing {body.get('repo')}")
            result = run_named_review(self.server.repos_root, self.server.out_root, body, self.server.config_path)
            print(f"[gs-mae] {result['repo']} {result['status']} -> {result['report_markdown']}")
            self._send(200, result)
        except ReviewError as exc:
            self._send(exc.status, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc)})
        finally:
            self.server.review_lock.release()

    def _send(self, status: int, payload: Dict) -> None:
        data = json.dumps(payload, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gs_mae.server")
    ap.add_argument("--repos", required=True, type=Path, help="directory of manually cloned repositories")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default=8765, type=int)
    ap.add_argument("--out", type=Path, default=Path("outputs/api"))
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args(argv)
    repos = args.repos.resolve()
    if not repos.is_dir():
        print(f"Checkout directory does not exist: {repos}")
        return 2
    server = ReviewServer((args.host, args.port), repos, args.out, args.config)
    print(f"[gs-mae] listening on http://{args.host}:{args.port}  repos={repos}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[gs-mae] stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
