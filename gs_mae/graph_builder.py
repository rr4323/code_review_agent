"""
Phase 1 - Call-graph snapshot and downward cone filter.

Parsing is delegated to a language adapter (`gs_mae/lang_adapters.py`).
The default adapters are Python and Jupyter notebooks. Rust, Go, and C/C++
adapters are available and registered by the caller when those trees should
be included.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from gs_mae.lang_adapters import (
    FunctionContract,
    LanguageAdapter,
    LayerSignals,
    ParsedModule,
    default_adapters,
    is_test_path,
)

__all__ = [
    "FunctionContract",
    "LanguageAdapter",
    "MerkleGraphCache",
    "ModuleGraphBuilder",
    "ModuleNode",
    "ScopedGraph",
]

DEFAULT_LAYER_HINTS = [
    ("presentation", (
        "controller", "view", "api", "route", "handler", "endpoint", "presentation", "ui",
        "cli", "cmd", "command", "main", "app", "entrypoint", "driver", "notebook", "experiment",
    )),
    ("serialization", (
        "serializer", "schema", "dto", "formatter", "codec", "parser", "encoder", "decoder",
        "wire", "marshaler", "proto", "serde",
    )),
    ("data", (
        "repo", "repository", "dao", "db", "store", "storage", "data", "model",
        "loader", "reader", "writer", "sink", "adapter", "client", "dataset",
    )),
]
LAYER_KEYWORDS = DEFAULT_LAYER_HINTS

_SKIP_DIR_NAMES = ("__pycache__", "venv", ".venv", "target", "node_modules", "build")


def _path_or_directive_layer(
    rel_path: str,
    source: str,
    custom_layer_hints: Optional[List[Tuple[str, Tuple[str, ...]]]] = None,
) -> Optional[str]:
    """Explicit `gsmae: layer=` directive, then filename and directory hints. None lets the adapter decide."""
    for line in source.splitlines()[:5]:
        if "gsmae: layer=" in line:
            return line.split("gsmae: layer=")[1].strip().split()[0]
    stem = Path(rel_path).stem.lower()
    parts = [p.lower() for p in Path(rel_path).parts]
    hints = custom_layer_hints if custom_layer_hints is not None else DEFAULT_LAYER_HINTS
    for layer, keys in hints:
        if any(k in stem for k in keys) or any(k in p for p in parts[:-1] for k in keys):
            return layer
    return None


@dataclass
class FileInfo:
    rel_path: str
    module_name: str
    layer: str
    source: str
    ast_hash: str
    language: str = "python"
    imports: Set[str] = field(default_factory=set)
    calls: Set[str] = field(default_factory=set)
    functions: List[FunctionContract] = field(default_factory=list)
    classes: List[str] = field(default_factory=list)
    is_test: bool = False
    signals: LayerSignals = field(default_factory=LayerSignals)

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.source) // 4)

    def to_snapshot_dict(self) -> Dict:
        return {
            "rel_path": self.rel_path,
            "language": self.language,
            "ast_hash": self.ast_hash,
            "calls": sorted(self.calls),
            "contracts": [f.to_dict() for f in self.functions],
            "classes": list(self.classes),
            "signals": asdict(self.signals),
            "is_test": self.is_test,
            "source": self.source,
        }

    @classmethod
    def from_snapshot_dict(cls, module_id: str, data: Dict) -> "FileInfo":
        calls = set(data.get("calls", []))
        return cls(
            rel_path=data["rel_path"],
            module_name=module_id,
            layer="domain",
            source=data.get("source", ""),
            ast_hash=data["ast_hash"],
            language=data.get("language", "python"),
            imports=set(calls),
            calls=calls,
            functions=[FunctionContract(**c) for c in data.get("contracts", [])],
            classes=list(data.get("classes", [])),
            is_test=bool(data.get("is_test", False)),
            signals=LayerSignals(**data.get("signals", {})),
        )


@dataclass
class ModuleNode:
    module_id: str
    rel_path: str
    layer: str
    source: str
    contracts: List[FunctionContract]
    language: str = "python"
    parents: Set[str] = field(default_factory=set)
    children: Set[str] = field(default_factory=set)
    ast_hash: str = ""
    merkle_hash: str = ""

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.source) // 4)

    def numbered_source(self) -> str:
        return "\n".join(f"{i:>3}| {line}" for i, line in enumerate(self.source.splitlines(), start=1))


class MerkleGraphCache:
    """Content-addressable cache: module_id -> {merkle_hash, evaluation}."""

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._store: Dict[str, Dict] = {}
        if self.path and self.path.exists():
            try:
                self._store = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._store = {}

    def get(self, module_id: str, merkle_hash: str) -> Optional[Dict]:
        entry = self._store.get(module_id)
        if entry and entry.get("merkle_hash") == merkle_hash:
            return entry["evaluation"]
        return None

    def put(self, module_id: str, merkle_hash: str, evaluation: Dict) -> None:
        self._store[module_id] = {"merkle_hash": merkle_hash, "evaluation": evaluation}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._store, indent=1), encoding="utf-8")


@dataclass
class ScopedGraph:
    root: str
    modules: Dict[str, ModuleNode]
    waves: List[List[str]]
    all_module_count: int
    test_files: Dict[str, str]
    roots: List[str] = field(default_factory=list)

    def edges(self) -> List[Tuple[str, str]]:
        return sorted((p, c) for p, m in self.modules.items() for c in m.children)


class ModuleGraphBuilder:
    def __init__(
        self,
        repo_root: Path,
        config_fingerprint: str = "",
        adapters: Optional[Sequence[LanguageAdapter]] = None,
        graph_path: Optional[Path] = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.config_fp = config_fingerprint
        self.adapters: Tuple[LanguageAdapter, ...] = tuple(adapters) if adapters is not None else default_adapters()
        self.graph_path: Optional[Path] = Path(graph_path) if graph_path is not None else None
        self.files: Dict[str, FileInfo] = {}
        self._adapter_by_module: Dict[str, LanguageAdapter] = {}
        self._path_index: Dict[str, Tuple[LanguageAdapter, List[str]]] = {}

    def _adapter_for(self, path: Path) -> Optional[LanguageAdapter]:
        for adapter in self.adapters:
            if adapter.accepts(path):
                return adapter
        return None

    def _adapter_for_language(self, language: str, rel_path: str) -> Optional[LanguageAdapter]:
        for adapter in self.adapters:
            if adapter.language == language:
                return adapter
        return self._adapter_for(Path(rel_path))

    def changed_files_from_git(self, base: str = "HEAD~1", head: str = "HEAD") -> List[str]:
        out = subprocess.run(
            ["git", "diff", "--name-only", base, head], cwd=self.repo_root,
            capture_output=True, text=True, check=True,
        ).stdout
        return [line for line in out.splitlines() if self._adapter_for(Path(line)) is not None]

    # ---------- Step 1: path index (no source body parsing for non-test modules) ----------
    def _index_paths(self) -> Tuple[int, Dict[str, str]]:
        self._path_index.clear()
        self._adapter_by_module.clear()
        test_files: Dict[str, str] = {}
        non_test_modules: Set[str] = set()

        for fpath in sorted(self.repo_root.rglob("*")):
            if not fpath.is_file():
                continue
            rel = fpath.relative_to(self.repo_root)
            if any(part.startswith(".") or part in _SKIP_DIR_NAMES for part in rel.parts):
                continue
            adapter = self._adapter_for(fpath)
            if adapter is None:
                continue
            rel_str = str(rel)
            if is_test_path(rel_str):
                raw = fpath.read_text(encoding="utf-8", errors="replace")
                prepped = adapter.prepare(raw)
                if prepped is not None:
                    test_files[rel_str] = prepped
                continue
            mid = adapter.module_id(rel_str)
            non_test_modules.add(mid)
            self._adapter_by_module[mid] = adapter
            if mid not in self._path_index:
                self._path_index[mid] = (adapter, [])
            self._path_index[mid][1].append(rel_str)

        return len(non_test_modules), test_files

    # ---------- Step 2: load / save snapshot ----------
    def _load_snapshot(self) -> None:
        self.files.clear()
        if not self.graph_path or not self.graph_path.exists():
            return
        try:
            raw = json.loads(self.graph_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        if raw.get("config_fingerprint") != self.config_fp:
            return
        modules_data = raw.get("modules", {})
        if not isinstance(modules_data, dict):
            return
        for mid, mdata in modules_data.items():
            info = FileInfo.from_snapshot_dict(mid, mdata)
            adapter = self._adapter_for_language(info.language, info.rel_path)
            if adapter is None:
                continue
            self.files[mid] = info
            self._adapter_by_module[mid] = adapter

    def _save_snapshot(self) -> None:
        if not self.graph_path:
            return
        payload = {
            "config_fingerprint": self.config_fp,
            "modules": {mid: info.to_snapshot_dict() for mid, info in sorted(self.files.items())},
        }
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.graph_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    # ---------- Step 3: resolve every non-test path in the file list ----------
    def resolve_roots(self, changed_files: Sequence[str]) -> List[str]:
        roots: List[str] = []
        for cf in changed_files:
            cf_path = Path(cf)
            if is_test_path(str(cf_path)):
                continue
            matched: Optional[str] = None
            # Exact relative path wins; a bare filename only resolves when it is unambiguous,
            # otherwise e.g. every `urls.py` / `__init__.py` collapses onto the first match.
            for mid, (adapter, rels) in self._path_index.items():
                if str(cf_path) in rels:
                    matched = mid
                    break
            if matched is None:
                for mid, info in self.files.items():
                    if not info.is_test and info.rel_path == str(cf_path):
                        matched = mid
                        break
            if matched is None:
                by_name = {mid for mid, (_a, rels) in self._path_index.items()
                           if any(Path(r).name == cf_path.name for r in rels)}
                if len(by_name) == 1:
                    matched = next(iter(by_name))
            if matched is not None and matched not in roots:
                roots.append(matched)
        if not roots:
            raise ValueError(f"None of the changed files map to a source module: {changed_files}")
        return roots

    def resolve_root(self, changed_files: Sequence[str]) -> str:
        return self.resolve_roots(changed_files)[0]

    # ---------- Step 4: parse on demand from path index ----------
    def _read_combined_source(self, mid: str) -> Optional[Tuple[LanguageAdapter, str, str]]:
        entry = self._path_index.get(mid)
        if entry is None:
            info = self.files.get(mid)
            if info is None:
                return None
            adapter = self._adapter_by_module.get(mid)
            if adapter is None:
                return None
            fpath = self.repo_root / info.rel_path
            if not fpath.is_file():
                return None
            prepped = adapter.prepare(fpath.read_text(encoding="utf-8", errors="replace"))
            return (adapter, info.rel_path, prepped) if prepped is not None else None
        adapter, rels = entry
        parts: List[Tuple[str, str]] = []
        for r in rels:
            fpath = self.repo_root / r
            if fpath.is_file():
                parts.append((r, fpath.read_text(encoding="utf-8", errors="replace")))
        if not parts:
            return None
        rel_path, combined = adapter.combine(parts)
        prepped = adapter.prepare(combined)
        if prepped is None:
            return None
        return adapter, rel_path, prepped

    def _parse_module_from_disk(self, mid: str, known: Set[str]) -> Optional[FileInfo]:
        loaded = self._read_combined_source(mid)
        if loaded is None:
            return None
        adapter, rel_path, source = loaded
        parsed = adapter.parse(rel_path, source, known)
        if parsed is None:
            return None
        parsed.imports.discard(parsed.module_id)
        parsed.calls.discard(parsed.module_id)
        valid_calls = {c for c in parsed.calls if c in known and c != parsed.module_id}
        info = FileInfo(
            rel_path=parsed.rel_path,
            module_name=parsed.module_id,
            layer=self._layer_for(adapter, parsed.rel_path, parsed.source, parsed.signals, False, False),
            source=parsed.source,
            ast_hash=parsed.content_digest,
            language=parsed.language,
            imports=parsed.imports,
            calls=valid_calls,
            functions=list(parsed.contracts),
            classes=list(parsed.classes),
            is_test=parsed.is_test,
            signals=parsed.signals,
        )
        self.files[mid] = info
        self._adapter_by_module[mid] = adapter
        return info

    def _layer_for(
        self,
        adapter: LanguageAdapter,
        rel_path: str,
        source: str,
        signals: LayerSignals,
        is_root_without_parents: bool,
        is_leaf_without_children: bool,
    ) -> str:
        hinted = _path_or_directive_layer(rel_path, source)
        if hinted:
            return hinted
        return adapter.infer_layer(
            signals,
            is_root_without_parents=is_root_without_parents,
            is_leaf_without_children=is_leaf_without_children,
        )

    # ---------- Step 5: build downward call cone, save snapshot, compute waves & Merkle ----------
    def build(self, changed_files: Sequence[str]) -> ScopedGraph:
        all_module_count, test_files = self._index_paths()
        self._load_snapshot()
        known = set(self._path_index.keys()) | set(self.files.keys())

        roots = self.resolve_roots(changed_files)
        root = roots[0]

        # Parse each listed module from disk and replace its snapshot entry
        for r_mid in roots:
            self._parse_module_from_disk(r_mid, known)

        # Walk stored calls: reuse children already in snapshot, or parse from path index when absent
        reachable: Set[str] = set()
        q = deque(roots)
        while q:
            cur = q.popleft()
            if cur in reachable:
                continue
            if cur not in self.files:
                if self._parse_module_from_disk(cur, known) is None:
                    continue
            reachable.add(cur)
            for child in sorted(self.files[cur].calls):
                if child not in reachable and (child in self._path_index or child in self.files):
                    if child not in self.files:
                        self._parse_module_from_disk(child, known)
                    if child in self.files and not self.files[child].is_test:
                        q.append(child)

        self._save_snapshot()

        modules: Dict[str, ModuleNode] = {}
        for mid in sorted(reachable):
            info = self.files[mid]
            modules[mid] = ModuleNode(
                module_id=mid,
                rel_path=info.rel_path,
                layer=info.layer,
                source=info.source,
                contracts=list(info.functions),
                language=info.language,
                ast_hash=info.ast_hash,
                children={c for c in info.calls if c in reachable},
            )
        for mid, module in modules.items():
            for child in module.children:
                modules[child].parents.add(mid)
        for mid, module in modules.items():
            info = self.files[mid]
            module.layer = self._layer_for(
                self._adapter_by_module[mid],
                module.rel_path,
                info.source,
                info.signals,
                is_root_without_parents=(not module.parents and bool(module.children)),
                is_leaf_without_children=(not module.children and bool(module.parents)),
            )

        waves: List[List[str]] = []
        done: Set[str] = set()
        while len(done) < len(modules):
            ready = sorted(m for m in modules if m not in done and modules[m].children <= done)
            if not ready:  # cycle: break deterministically by picking the module with fewest pending children
                pending = sorted(set(modules) - done, key=lambda m: (len(modules[m].children - done), m))
                ready = [pending[0]]
            waves.append(ready)
            done.update(ready)

        for wave in waves:
            for mid in wave:
                module = modules[mid]
                child_h = "|".join(sorted(modules[c].merkle_hash for c in module.children if modules[c].merkle_hash))
                blob = f"{mid}:{module.ast_hash}:[{child_h}]:{self.config_fp}"
                module.merkle_hash = hashlib.sha256(blob.encode()).hexdigest()[:16]

        return ScopedGraph(
            root=root,
            modules=modules,
            waves=waves,
            all_module_count=all_module_count,
            test_files=test_files,
            roots=roots,
        )
