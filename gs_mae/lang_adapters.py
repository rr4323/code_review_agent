"""Language adapters for the module graph.

`ModuleGraphBuilder` stays language-neutral. It asks a `LanguageAdapter` to turn
one source file into imports, `FunctionContract`s, a content digest, and layer
signals. The default registry is Python plus Jupyter notebooks (a Python
frontend with cell extraction). Register another adapter to extend the graph:

    ModuleGraphBuilder(repo, fingerprint, adapters=(PythonAdapter(), RustAdapter()))
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


_PRESENTATION_DECORS = ("route", "get", "post", "put", "delete", "patch", "api_view", "endpoint", "handler", "command")
_PRESENTATION_IMPORTS = (
    "fastapi", "flask", "django", "starlette", "aiohttp", "tornado", "falcon",
    "argparse", "click", "typer", "axum", "actix_web", "clap", "gin", "net/http", "cobra",
)
# Frameworks whose non-web submodules (settings, ORM, management) appear in every module.
_PRESENTATION_SUBMODULES = {
    "django": ("django.http", "django.urls", "django.views", "django.shortcuts", "django.template"),
}


def _is_presentation_import(module: str) -> bool:
    root = module.split(".")[0]
    if root not in _PRESENTATION_IMPORTS and module not in _PRESENTATION_IMPORTS:
        return False
    allowed = _PRESENTATION_SUBMODULES.get(root)
    if allowed is None:
        return True
    return any(module == a or module.startswith(a + ".") for a in allowed)
_SERIALIZATION_BASES = ("BaseModel", "Serializer", "ModelSerializer", "Schema", "TypedDict", "Serialize", "Deserialize")
_SERIALIZATION_PREFIXES = (
    "serialize", "deserialize", "to_dict", "from_dict", "format_", "dump_", "load_",
    "encode", "decode", "parse", "unparse", "marshal", "unmarshal",
)
_IO_OR_DB_SIGNALS = (
    "execute", "executemany", "fetchone", "fetchall", "cursor", "raw_sql",
    "read_text", "read_bytes", "write_text", "write_bytes", "read_csv", "read_parquet",
    "sqlx::", "diesel::", "rusqlite::", "std::fs::", "std::ifstream", "std::ofstream",
    "fopen", "database/sql", "os.ReadFile", "os.Open", "DataLoader",
)
_CPP_IMPL_SUFFIXES = (".c", ".cc", ".cpp", ".cxx")


@dataclass
class FunctionContract:
    """Interface facts of a public function, shared with parent modules."""

    name: str
    lineno: int
    signature: str
    doc: str
    may_return_none: bool
    raises: List[str]
    annotated: bool

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class LayerSignals:
    """Structural facts an adapter reports. The builder applies path hints and topology."""

    entrypoint: bool = False
    http_status: bool = False
    serde: bool = False
    io_db: bool = False
    domain_exception: bool = False


@dataclass
class ParsedModule:
    module_id: str
    rel_path: str
    language: str
    source: str
    imports: Set[str]
    contracts: List[FunctionContract]
    classes: List[str]
    content_digest: str
    is_test: bool
    signals: LayerSignals = field(default_factory=LayerSignals)
    calls: Set[str] = field(default_factory=set)


def resolve_import(dotted: str, known: Set[str]) -> Optional[str]:
    """Resolve an import string to a repo module, tolerating leading package prefixes."""
    parts = [p for p in dotted.split(".") if p and p not in ("crate", "super", "self")]
    for start in range(len(parts)):
        for end in range(len(parts), start, -1):
            cand = ".".join(parts[start:end])
            if cand in known:
                return cand
            matches = [k for k in known if k.endswith("." + cand)]
            if len(matches) == 1:
                return matches[0]
    return None


def is_test_path(rel_path: str) -> bool:
    fname = Path(rel_path).name.lower()
    return (
        fname.startswith("test_")
        or fname.endswith(("_test.py", "_test.go", "_test.rs", "_test.cpp"))
        or "tests" in Path(rel_path).parts
    )


def _content_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:16]


def _preceding_comment_doc(lines: List[str], lineno: int) -> str:
    doc_parts: List[str] = []
    idx = lineno - 2
    while idx >= 0:
        s = lines[idx].strip()
        if s.startswith(("///", "//", "/*", "*")):
            doc_parts.append(s.lstrip("/* ").rstrip("*/ "))
            idx -= 1
        else:
            break
    return " ".join(reversed([p for p in doc_parts if p]))[:240]


def extract_notebook_python(raw_ipynb: str) -> str:
    """Extract executable Python from a Jupyter `.ipynb`, commenting out `%` / `!` magics."""
    try:
        nb = json.loads(raw_ipynb)
    except json.JSONDecodeError:
        return ""
    lines: List[str] = ["# gsmae: language=notebook"]
    for idx, cell in enumerate(nb.get("cells", []), start=1):
        if cell.get("cell_type") != "code":
            continue
        lines.append(f"# --- cell {idx} ---")
        src = cell.get("source", [])
        cell_text = "".join(src) if isinstance(src, list) else str(src)
        for line in cell_text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("%", "!")):
                lines.append(f"# [magic] {line}")
            else:
                lines.append(line)
    return "\n".join(lines) + "\n"


class LanguageAdapter:
    """Parse one language into the shared module record.

    Subclasses set `language` and `extensions`, and implement `parse`.
    Override `prepare` to normalize source before hashing, and `combine`
    when several files share one module id (for example a C++ header and its implementation).
    """

    language: str = ""
    extensions: Tuple[str, ...] = ()

    def accepts(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def module_id(self, rel_path: str) -> str:
        p = Path(rel_path).with_suffix("")
        parts = list(p.parts)
        if parts and parts[-1] in ("__init__", "mod"):
            parts = parts[:-1] or parts
        return ".".join(parts)

    def combine(self, parts: List[Tuple[str, str]]) -> Tuple[str, str]:
        """Collapse files that share a module id. Default: the last path wins."""
        return parts[-1]

    def prepare(self, source: str) -> Optional[str]:
        """Return source to parse, or None to skip the file. Default: unchanged text."""
        return source

    def parse(self, rel_path: str, source: str, known_modules: Set[str]) -> Optional[ParsedModule]:
        raise NotImplementedError

    def infer_layer(
        self,
        signals: LayerSignals,
        *,
        is_root_without_parents: bool = False,
        is_leaf_without_children: bool = False,
    ) -> str:
        return "domain"

    def _parsed(
        self,
        rel_path: str,
        source: str,
        imports: Set[str],
        contracts: List[FunctionContract],
        classes: List[str],
        digest_input: bytes,
        signals: LayerSignals,
        calls: Optional[Set[str]] = None,
    ) -> ParsedModule:
        return ParsedModule(
            module_id=self.module_id(rel_path),
            rel_path=rel_path,
            language=self.language,
            source=source,
            imports=set(imports),
            contracts=contracts,
            classes=classes,
            content_digest=_content_digest(digest_input),
            is_test=is_test_path(rel_path),
            signals=signals,
            calls=set(calls) if calls is not None else set(),
        )


def _python_signals(tree: ast.AST) -> LayerSignals:
    has_route_decor = False
    has_http_status_dict = False
    has_handler_fn = False
    has_serializer_signal = False
    has_db_signal = False
    has_domain_exception = False
    has_main_guard = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Module paths only (not imported symbol names), matched by dotted component so that
            # "gin" does not match "logging" and "django.conf" is not treated as a web layer.
            if isinstance(node, ast.Import):
                mod_names = [a.name.lower() for a in node.names]
            else:
                mod_names = [node.module.lower()] if node.module else []
            if any(_is_presentation_import(m) for m in mod_names):
                has_route_decor = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fname = node.name.lower()
            if fname == "main" or fname.startswith(("handle_", "on_", "endpoint_", "route_", "cli_")):
                has_handler_fn = True
            if fname.startswith(_SERIALIZATION_PREFIXES):
                has_serializer_signal = True
            for dec in node.decorator_list:
                dec_txt = ast.unparse(dec).lower()
                if any(d in dec_txt for d in _PRESENTATION_DECORS):
                    has_route_decor = True
        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            if any(b.endswith(_SERIALIZATION_BASES) for b in bases):
                has_serializer_signal = True
            if any("Error" in node.name or "Exception" in node.name for b in bases if "Exception" in b):
                has_domain_exception = True
        elif isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if "status" in keys or "status_code" in keys:
                has_http_status_dict = True
        elif isinstance(node, ast.Call):
            cname = (
                node.func.id.lower() if isinstance(node.func, ast.Name)
                else node.func.attr.lower() if isinstance(node.func, ast.Attribute)
                else ""
            )
            if cname in _IO_OR_DB_SIGNALS:
                has_db_signal = True
        elif isinstance(node, ast.If):
            try:
                if "__name__" in ast.unparse(node.test) and "__main__" in ast.unparse(node.test):
                    has_main_guard = True
            except Exception:
                pass

    return LayerSignals(
        entrypoint=has_route_decor or has_handler_fn or has_main_guard,
        http_status=has_http_status_dict,
        serde=has_serializer_signal,
        io_db=has_db_signal,
        domain_exception=has_domain_exception,
    )


def _func_contract(node: ast.AST) -> FunctionContract:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    may_none = False
    raises: List[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return):
            if sub.value is None or (isinstance(sub.value, ast.Constant) and sub.value.value is None):
                may_none = True
        if isinstance(sub, ast.Raise) and sub.exc is not None:
            exc = sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc
            raises.append(ast.unparse(exc))
    if node.returns is not None and "Optional" in ast.unparse(node.returns) or (
        node.returns is not None and "None" in ast.unparse(node.returns) and ast.unparse(node.returns) != "None"
    ):
        may_none = True
    params = [a for a in node.args.args if a.arg not in ("self", "cls")]
    annotated = node.returns is not None and all(a.annotation is not None for a in params)
    doc = (ast.get_docstring(node) or "").strip().replace("\n", " ")
    return FunctionContract(
        name=node.name,
        lineno=node.lineno,
        signature=f"def {node.name}({args}){ret}",
        doc=doc[:240],
        may_return_none=may_none,
        raises=sorted(set(raises)),
        annotated=annotated,
    )


class PythonAdapter(LanguageAdapter):
    """Reference adapter. Digest is the AST dump, so comments and whitespace do not change the Merkle hash."""

    language = "python"
    extensions = (".py",)

    def prepare(self, source: str) -> Optional[str]:
        try:
            ast.parse(source)
        except SyntaxError:
            return None
        return source

    def parse(self, rel_path: str, source: str, known_modules: Set[str]) -> Optional[ParsedModule]:
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            return None
        module_id = self.module_id(rel_path)
        pkg_parts = module_id.split(".")[:-1] if not rel_path.endswith("__init__.py") else module_id.split(".")
        imports: Set[str] = set()
        bindings: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    resolved = resolve_import(alias.name, known_modules)
                    if resolved:
                        imports.add(resolved)
                        if alias.asname:
                            bindings[alias.asname] = resolved
                        else:
                            bindings[alias.name] = resolved
                            bindings[alias.name.split(".")[0]] = resolved
                            bindings[alias.name.split(".")[-1]] = resolved
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    anchor = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
                    base = ".".join([*anchor, base] if base else anchor)
                base_resolved = resolve_import(base, known_modules) if base else None
                for alias in node.names:
                    cand = f"{base}.{alias.name}" if base else alias.name
                    resolved = resolve_import(cand, known_modules) or base_resolved
                    if resolved:
                        imports.add(resolved)
                        bindings[alias.asname or alias.name] = resolved

        calls: Set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in bindings:
                calls.add(bindings[fn.id])
            elif isinstance(fn, ast.Attribute):
                val = fn.value
                if isinstance(val, ast.Name) and val.id in bindings:
                    calls.add(bindings[val.id])
                else:
                    dotted = ast.unparse(val)
                    if dotted in bindings:
                        calls.add(bindings[dotted])
                    else:
                        cur = val
                        while isinstance(cur, ast.Attribute):
                            cur = cur.value
                        if isinstance(cur, ast.Name) and cur.id in bindings:
                            calls.add(bindings[cur.id])

        functions: List[FunctionContract] = []
        classes: List[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                functions.append(_func_contract(node))
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.name.startswith("_"):
                        contract = _func_contract(sub)
                        contract.name = f"{node.name}.{contract.name}"
                        functions.append(contract)
        return self._parsed(
            rel_path, source, imports, functions, classes, ast.dump(tree).encode(), _python_signals(tree), calls=calls,
        )

    def infer_layer(
        self,
        signals: LayerSignals,
        *,
        is_root_without_parents: bool = False,
        is_leaf_without_children: bool = False,
    ) -> str:
        if signals.entrypoint or (signals.http_status and is_root_without_parents):
            return "presentation"
        if self.language == "notebook" and is_root_without_parents:
            return "presentation"
        if signals.io_db and is_leaf_without_children and not signals.serde:
            return "data"
        if signals.serde and not signals.domain_exception:
            return "serialization"
        if signals.io_db and not signals.http_status and not signals.domain_exception:
            return "data"
        return "domain"


class NotebookAdapter(PythonAdapter):
    """Jupyter notebooks are Python after cell extraction. The language tag stays `notebook`."""

    language = "notebook"
    extensions = (".ipynb",)

    def prepare(self, source: str) -> Optional[str]:
        return super().prepare(extract_notebook_python(source))


def _regex_signals(source: str) -> LayerSignals:
    src_lower = source.lower()
    has_main = bool(re.search(r"\b(fn\s+main|func\s+main|int\s+main)\s*\(", source))
    has_web_or_cli = any(k in src_lower for k in _PRESENTATION_IMPORTS) or bool(
        re.search(r"\b(handle_| route|endpoint)\w*\s*\(", src_lower)
    )
    has_serde = any(
        k.lower() in src_lower
        for k in ("serde", "serialize", "deserialize", "protobuf", "json::", "encoding/json", "to_dict", "marshal")
    )
    has_io_db = any(k.lower() in src_lower for k in _IO_OR_DB_SIGNALS) or bool(
        re.search(r"\b(select|insert|update|delete)\s+.+\bfrom\b", src_lower)
    )
    return LayerSignals(entrypoint=has_main or has_web_or_cli, serde=has_serde, io_db=has_io_db)


class RegexLanguageAdapter(LanguageAdapter):
    """Shared digest and layer rules for adapters that scan source text."""

    def infer_layer(
        self,
        signals: LayerSignals,
        *,
        is_root_without_parents: bool = False,
        is_leaf_without_children: bool = False,
    ) -> str:
        if signals.entrypoint or (is_root_without_parents and not signals.io_db and not signals.serde):
            return "presentation"
        if signals.io_db and (is_leaf_without_children or not signals.serde):
            return "data"
        if signals.serde:
            return "serialization"
        return "domain"

    def _from_scan(
        self,
        rel_path: str,
        source: str,
        imports: Set[str],
        contracts: List[FunctionContract],
        classes: List[str],
    ) -> ParsedModule:
        calls: Set[str] = set()
        for imp_mod in imports:
            short = imp_mod.split(".")[-1]
            if re.search(rf"\b{re.escape(short)}\s*(?:::|\.)\s*[A-Za-z_]\w*\s*\(", source):
                calls.add(imp_mod)
        if not calls and len(imports) == 1:
            defined = {c.name.split(".")[-1] for c in contracts} | {
                "if", "for", "while", "switch", "catch", "return", "sizeof", "Ok", "Err", "Some",
            }
            for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", source):
                if m.group(1) not in defined:
                    calls = set(imports)
                    break
        return self._parsed(
            rel_path, source, imports, contracts, classes, source.encode("utf-8"), _regex_signals(source), calls=calls,
        )


class RustAdapter(RegexLanguageAdapter):
    language = "rust"
    extensions = (".rs",)

    def parse(self, rel_path: str, source: str, known_modules: Set[str]) -> Optional[ParsedModule]:
        imports: Set[str] = set()
        functions: List[FunctionContract] = []
        classes: List[str] = []
        lines = source.splitlines()
        for m in re.finditer(r"^\s*(?:pub\s+)?(?:use|mod)\s+([a-zA-Z0-9_:]+)", source, re.M):
            dotted = m.group(1).replace("::", ".").strip(".")
            resolved = resolve_import(dotted, known_modules)
            if resolved:
                imports.add(resolved)
        for m in re.finditer(r"^\s*pub\s+struct\s+([A-Za-z_]\w*)", source, re.M):
            classes.append(m.group(1))
        fn_pat = re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:->\s*([^{]+))?\s*\{",
            re.M,
        )
        for m in fn_pat.finditer(source):
            name, params, ret = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
            if name.startswith("_"):
                continue
            lineno = source[: m.start()].count("\n") + 1
            body_slice = "\n".join(lines[lineno - 1: min(len(lines), lineno + 25)])
            may_none = "Option<" in ret or bool(re.search(r"\bNone\b", body_slice))
            raises: List[str] = []
            res_m = re.search(r"Result\s*<[^,]+,\s*([A-Za-z0-9_:<>]+)\s*>", ret)
            if res_m:
                raises.append(res_m.group(1).strip())
            if "panic!(" in body_slice:
                raises.append("panic!")
            doc = _preceding_comment_doc(lines, lineno)
            sig = f"fn {name}({params})" + (f" -> {ret}" if ret else "")
            functions.append(FunctionContract(name, lineno, sig, doc, may_none, sorted(set(raises)), True))
        return self._from_scan(rel_path, source, imports, functions, classes)


class GoAdapter(RegexLanguageAdapter):
    language = "go"
    extensions = (".go",)

    def parse(self, rel_path: str, source: str, known_modules: Set[str]) -> Optional[ParsedModule]:
        imports: Set[str] = set()
        functions: List[FunctionContract] = []
        classes: List[str] = []
        lines = source.splitlines()
        for m in re.finditer(r'"([^"]+)"', source):
            dotted = m.group(1).replace("/", ".")
            resolved = resolve_import(dotted, known_modules)
            if resolved:
                imports.add(resolved)
        for m in re.finditer(r"^\s*type\s+([A-Za-z_]\w*)\s+struct\b", source, re.M):
            classes.append(m.group(1))
        fn_pat = re.compile(
            r"^\s*func\s+(?:\([^)]+\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)\)\s*([^{]*)\{",
            re.M,
        )
        for m in fn_pat.finditer(source):
            name, params, ret = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
            lineno = source[: m.start()].count("\n") + 1
            body_slice = "\n".join(lines[lineno - 1: min(len(lines), lineno + 25)])
            may_none = "*" in ret or "nil" in body_slice
            raises = []
            if "error" in ret or "fmt.Errorf" in body_slice or "errors.New" in body_slice:
                raises.append("error")
            doc = _preceding_comment_doc(lines, lineno)
            sig = f"func {name}({params})" + (f" -> {ret}" if ret else "")
            functions.append(FunctionContract(name, lineno, sig, doc, may_none, raises, True))
        return self._from_scan(rel_path, source, imports, functions, classes)


class CppAdapter(RegexLanguageAdapter):
    language = "cpp"
    extensions = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh")

    def combine(self, parts: List[Tuple[str, str]]) -> Tuple[str, str]:
        rel, src = parts[0]
        for next_rel, next_src in parts[1:]:
            if Path(next_rel).suffix.lower() in _CPP_IMPL_SUFFIXES:
                rel = next_rel
            src = src + "\n" + next_src
        return rel, src

    def parse(self, rel_path: str, source: str, known_modules: Set[str]) -> Optional[ParsedModule]:
        imports: Set[str] = set()
        functions: List[FunctionContract] = []
        classes: List[str] = []
        lines = source.splitlines()
        for m in re.finditer(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', source, re.M):
            inc = Path(m.group(1)).with_suffix("").as_posix().replace("/", ".")
            resolved = resolve_import(inc, known_modules)
            if resolved:
                imports.add(resolved)
        for m in re.finditer(r"^\s*(?:class|struct)\s+([A-Za-z_]\w*)", source, re.M):
            classes.append(m.group(1))
        fn_pat = re.compile(
            r"^\s*([A-Za-z_][\w:<>\s\*&]*?)\s+([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*(?:const\s*)?\{",
            re.M,
        )
        for m in fn_pat.finditer(source):
            ret, name, params = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            if name in ("if", "for", "while", "switch", "catch") or ret.startswith("//"):
                continue
            lineno = source[: m.start()].count("\n") + 1
            body_slice = "\n".join(lines[lineno - 1: min(len(lines), lineno + 25)])
            may_none = ("*" in ret or "optional" in ret.lower() or "nullptr" in body_slice or "NULL" in body_slice)
            raises = sorted(set(re.findall(r"\bthrow\s+([A-Za-z0-9_:]+)", body_slice)))
            doc = _preceding_comment_doc(lines, lineno)
            sig = f"def {name}({params}) -> {ret}"
            functions.append(FunctionContract(name, lineno, sig, doc, may_none, raises, True))
        return self._from_scan(rel_path, source, imports, functions, classes)


def default_adapters() -> Tuple[LanguageAdapter, ...]:
    """Python and notebooks. Pass `RustAdapter`, `GoAdapter`, or `CppAdapter` explicitly to opt in."""
    return (PythonAdapter(), NotebookAdapter())
