"""Lightweight local control-plane API server for live profiling and OpenAPI exercises.

Exposes the control-plane endpoints (/health, /openapi.json, /api/audit-events/)
using standard library http.server for profiling and latency telemetry.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Tuple

OPENAPI_SPEC = {
    "openapi": "3.0.0",
    "info": {
        "title": "Delegate Control Plane API",
        "version": "1.0.0",
        "description": "POC Control Plane API for delegate task dispatch and audit trails.",
    },
    "paths": {
        "/api/audit-events/": {
            "get": {
                "operationId": "list_audit_events",
                "summary": "Retrieve paginated audit logs for delegates and executions",
                "responses": {
                    "200": {
                        "description": "List of recorded audit events",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
        "/health": {
            "get": {
                "operationId": "health_check",
                "summary": "Service liveness and readiness probe",
                "responses": {"200": {"description": "Service is healthy"}},
            }
        },
    },
}


class ControlPlaneApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "control_plane"})
        elif path in ("/openapi.json", "/openapi.yaml"):
            self._send_json(200, OPENAPI_SPEC)
        elif path == "/api/audit-events/":
            self._send_json(
                200,
                {
                    "events": [
                        {
                            "event_type": "DELEGATE_REGISTERED",
                            "tenant_id": "tenant-demo",
                            "delegate_id": "del-001",
                            "detail": "name=vm-delegate-1",
                        }
                    ]
                },
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/health", "/openapi.json", "/openapi.yaml", "/api/audit-events/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Suppress noisy standard server logging during profiling
        pass


def run(host: str = "127.0.0.1", port: int = 18000) -> None:
    server_address: Tuple[str, int] = (host, port)
    httpd = HTTPServer(server_address, ControlPlaneApiHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18000
    run(port=port)
