"""The local review API resolves a checkout by name and writes its report."""

import json
import threading
from urllib.request import Request, urlopen

from gs_mae.server import ReviewServer


def _call(server, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(f"http://127.0.0.1:{server.server_address[1]}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request) as response:
        payload = response.read()
        return response.status, response.headers.get("Content-Type", ""), payload


def test_api_reviews_named_checkout_and_writes_report(tmp_path):
    repos = tmp_path / "checkouts"
    repo = repos / "sample"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("def ping() -> dict:\n    return {'ok': True}\n")
    server = ReviewServer(("127.0.0.1", 0), repos, tmp_path / "reports", None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, content_type, raw = _call(server, "GET", "/repos")
        assert status == 200 and json.loads(raw) == {"repos": ["sample"]}
        status, content_type, raw = _call(server, "POST", "/reviews", {"repo": "sample", "system": "gsmae", "backend": "mock"})
        report = json.loads(raw)
        assert status == 200 and content_type.startswith("application/json")
        assert report["status"] in {"PASSED", "FAILED_QUALITY_GATE"}
        assert report["file_source"] == "all-source"
        assert (tmp_path / "reports" / "sample" / "review_report.md").is_file()
        status, content_type, raw = _call(server, "GET", "/reports/sample")
        assert status == 200 and content_type.startswith("text/markdown") and b"GS-MAE review" in raw
    finally:
        server.shutdown()
        server.server_close()


def test_api_rejects_path_like_repo_names(tmp_path):
    server = ReviewServer(("127.0.0.1", 0), tmp_path, tmp_path / "reports", None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/reviews",
            data=json.dumps({"repo": "../secret"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urlopen(request)
            assert False, "path traversal was accepted"
        except Exception as exc:
            assert getattr(exc, "code", None) == 400
    finally:
        server.shutdown()
        server.server_close()
