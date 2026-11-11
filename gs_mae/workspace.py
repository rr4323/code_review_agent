"""Isolated workspace manager providing full traceability for each autonomous agent."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class WrittenFileRecord:
    path: str
    rel_path: str
    size_bytes: int
    sha256: str
    written_at: str
    category: str


class AgentWorkspace:
    """Dedicated workspace folder for a single agent, recording all its inputs, prompts, completions, and outputs."""

    def __init__(self, workspace_dir: Path, agent_id: str, role: str,
                 module_id: str = "", category: str = "", username: str = "anonymous"):
        self.workspace_dir = Path(workspace_dir).resolve()
        self.agent_id = agent_id
        self.role = role
        self.module_id = module_id
        self.category = category
        self.username = username
        self._lock = threading.RLock()
        self._seq = 0
        self._written_files: List[WrittenFileRecord] = []

        # Create subdirectories
        self.prompts_dir = self.workspace_dir / "prompts"
        self.responses_dir = self.workspace_dir / "responses"
        self.artifacts_dir = self.workspace_dir / "artifacts"

        for d in (self.prompts_dir, self.responses_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.log_file = self.workspace_dir / "activity.log"
        self._init_metadata()
        self.log("INIT", f"Agent workspace initialized for {self.agent_id} (role={self.role})")

    def _init_metadata(self) -> None:
        meta = {
            "agent_id": self.agent_id,
            "role": self.role,
            "module_id": self.module_id,
            "category": self.category,
            "username": self.username,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workspace_path": str(self.workspace_dir),
        }
        (self.workspace_dir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def log(self, action: str, detail: str = "", data: Optional[Dict] = None) -> None:
        """Append an entry to activity.log."""
        with self._lock:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            line = f"[{ts}] [{action.upper():<10}] {detail}"
            if data:
                line += f" | {json.dumps(data)}"
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _track_write(self, file_path: Path, cat: str) -> None:
        """Record a file written by this agent."""
        data = file_path.read_bytes()
        rec = WrittenFileRecord(
            path=str(file_path),
            rel_path=str(file_path.relative_to(self.workspace_dir)),
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            written_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            category=cat,
        )
        self._written_files.append(rec)
        manifest_path = self.workspace_dir / "written_manifest.json"
        manifest_data = [asdict(r) for r in self._written_files]
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    def record_prompt(self, name: str, prompt_text: str) -> Path:
        """Record prompt content sent to an LLM or tool."""
        with self._lock:
            filename = f"{name}.txt" if not name.endswith((".txt", ".json")) else name
            target = self.prompts_dir / filename
            target.write_text(prompt_text, encoding="utf-8")
            self._track_write(target, "prompt")
            self.log("PROMPT", f"Recorded prompt {filename} ({len(prompt_text)} chars)")
            return target

    def record_response(self, name: str, response_data: Any) -> Path:
        """Record raw response content received from an LLM or tool."""
        with self._lock:
            if isinstance(response_data, str):
                filename = f"{name}.txt" if not name.endswith((".txt", ".json")) else name
                target = self.responses_dir / filename
                target.write_text(response_data, encoding="utf-8")
            else:
                filename = f"{name}.json" if not name.endswith(".json") else name
                target = self.responses_dir / filename
                target.write_text(json.dumps(response_data, indent=2), encoding="utf-8")
            self._track_write(target, "response")
            self.log("RESPONSE", f"Recorded response {filename}")
            return target

    def record_artifact(self, name: str, artifact_data: Any) -> Path:
        """Record structured findings, diffs, or evaluation artifacts created by the agent."""
        with self._lock:
            if isinstance(artifact_data, str):
                target = self.artifacts_dir / name
                target.write_text(artifact_data, encoding="utf-8")
            else:
                filename = f"{name}.json" if not name.endswith(".json") else name
                target = self.artifacts_dir / filename
                target.write_text(json.dumps(artifact_data, indent=2), encoding="utf-8")
            self._track_write(target, "artifact")
            self.log("ARTIFACT", f"Saved deliverable artifact {target.name}")
            return target

    def get_summary(self) -> Dict:
        """Return a traceability summary of what this agent wrote."""
        with self._lock:
            total_bytes = sum(r.size_bytes for r in self._written_files)
            return {
                "agent_id": self.agent_id,
                "role": self.role,
                "module_id": self.module_id,
                "category": self.category,
                "workspace_path": str(self.workspace_dir),
                "total_files_written": len(self._written_files),
                "total_bytes_written": total_bytes,
                "written_files": [asdict(r) for r in self._written_files],
            }


class WorkspaceManager:
    """Manages all agent workspaces for a review session, providing central traceability."""

    def __init__(self, root_dir: Path, username: str = "anonymous"):
        self.root_dir = Path(root_dir).resolve()
        self.username = username
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._workspaces: Dict[str, AgentWorkspace] = {}
        self._lock = threading.RLock()

    def get_or_create(self, agent_id: str, role: str, module_id: str = "",
                      category: str = "") -> AgentWorkspace:
        """Retrieve existing agent workspace or create a new dedicated one."""
        with self._lock:
            ws = self._workspaces.get(agent_id)
            if ws is None:
                agent_dir = self.root_dir / agent_id
                ws = AgentWorkspace(
                    workspace_dir=agent_dir,
                    agent_id=agent_id,
                    role=role,
                    module_id=module_id,
                    category=category,
                    username=self.username,
                )
                self._workspaces[agent_id] = ws
            return ws

    def generate_manifest(self) -> Dict:
        """Generate a complete manifest of all agent workspaces and everything each wrote."""
        with self._lock:
            summaries = {aid: ws.get_summary() for aid, ws in self._workspaces.items()}
            manifest = {
                "workspaces_root": str(self.root_dir),
                "username": self.username,
                "total_agents": len(self._workspaces),
                "total_files_written": sum(s["total_files_written"] for s in summaries.values()),
                "total_bytes_written": sum(s["total_bytes_written"] for s in summaries.values()),
                "agents": summaries,
            }
            manifest_path = self.root_dir / "workspace_manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            return manifest
