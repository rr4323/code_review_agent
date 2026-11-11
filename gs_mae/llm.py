"""LLM backends: Ollama (real Gemma 4 inference) and a mock used only by unit tests."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

import requests


@dataclass
class CallRecord:
    role: str               # e.g. "category:security", "merge", "flat", "repair"
    module_id: str
    prompt_tokens: int      # tokens actually evaluated by the model (cache hits excluded by llama.cpp)
    output_tokens: int
    prompt_eval_s: float
    eval_s: float
    wall_s: float
    prompt_chars: int
    parse_ok: bool
    response_text: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class UsageLedger:
    calls: List[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, rec: CallRecord) -> None:
        with self._lock:
            self.calls.append(rec)

    def summary(self) -> Dict:
        c = self.calls
        return {
            "llm_calls": len(c),
            "prompt_tokens": sum(r.prompt_tokens for r in c),
            "output_tokens": sum(r.output_tokens for r in c),
            "prompt_eval_s": round(sum(r.prompt_eval_s for r in c), 2),
            "eval_s": round(sum(r.eval_s for r in c), 2),
            "wall_s": round(sum(r.wall_s for r in c), 2),
            "max_prompt_chars": max((r.prompt_chars for r in c), default=0),
            "json_parse_failures": sum(1 for r in c if not r.parse_ok),
        }


def parse_json_loose(text: str) -> Optional[Dict]:
    text = text.strip()
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else {"items": val}
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


class OllamaClient:
    def __init__(self, model: str, url: str = "http://127.0.0.1:11434", num_ctx: int = 8192,
                 temperature: float = 0.0, seed: int = 7, max_output_tokens: int = 384,
                 timeout_s: int = 600):
        self.model, self.url = model, url.rstrip("/")
        self.num_ctx, self.temperature, self.seed = num_ctx, temperature, seed
        self.max_output_tokens, self.timeout_s = max_output_tokens, timeout_s
        self.ledger = UsageLedger()

    @classmethod
    def from_config(cls, cfg: Dict) -> "OllamaClient":
        rt = cfg.get("model_runtime", {})
        return cls(model=rt.get("model", "gemma4:e2b-it-qat"), url=rt.get("ollama_url", "http://127.0.0.1:11434"),
                   num_ctx=int(rt.get("num_ctx", 8192)), temperature=float(rt.get("temperature", 0.0)),
                   seed=int(rt.get("seed", 7)), max_output_tokens=int(rt.get("max_output_tokens", 384)),
                   timeout_s=int(rt.get("request_timeout_s", 600)))

    def health(self) -> bool:
        try:
            tags = requests.get(f"{self.url}/api/tags", timeout=5).json()
            return any(m.get("name") == self.model or m.get("model") == self.model for m in tags.get("models", []))
        except requests.RequestException:
            return False

    def chat_json(self, system: str, user: str, schema: Optional[Dict], role: str, module_id: str,
                  max_tokens: Optional[int] = None) -> Dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            "format": schema if schema else "json",
            "keep_alive": "60m",
            "options": {"temperature": self.temperature, "seed": self.seed, "num_ctx": self.num_ctx,
                        "num_predict": max_tokens or self.max_output_tokens},
        }
        t0 = time.perf_counter()
        resp = requests.post(f"{self.url}/api/chat", json=body, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        wall = time.perf_counter() - t0
        text = data.get("message", {}).get("content", "")
        parsed = parse_json_loose(text)
        self.ledger.add(CallRecord(
            role=role, module_id=module_id,
            prompt_tokens=int(data.get("prompt_eval_count", 0)), output_tokens=int(data.get("eval_count", 0)),
            prompt_eval_s=data.get("prompt_eval_duration", 0) / 1e9, eval_s=data.get("eval_duration", 0) / 1e9,
            wall_s=wall, prompt_chars=len(system) + len(user), parse_ok=parsed is not None,
            response_text=text[:2000],
        ))
        return parsed or {}


class MockClient:
    """Deterministic stand-in for unit tests ONLY. Never used to produce reported results."""

    def __init__(self, responder: Optional[Callable[[str, str, str], Dict]] = None):
        self.responder = responder or self._default_responder
        self.ledger = UsageLedger()
        self.model = "mock"
        self.url = "mock://local"

    @staticmethod
    def _default_responder(role: str, module_id: str, user: str) -> Dict:
        if role.startswith("category:correctness"):
            return {"analysis": "checked", "findings": []}
        if role.startswith("category:"):
            return {"verdicts": []}
        if role == "merge":
            return {"summary": f"Reviewed {module_id}", "risks_for_callers": [], "compound_risks": []}
        return {}

    def health(self) -> bool:
        return True

    def chat_json(self, system: str, user: str, schema: Optional[Dict], role: str, module_id: str,
                  max_tokens: Optional[int] = None) -> Dict:
        out = self.responder(role, module_id, user)
        self.ledger.add(CallRecord(role, module_id, len(user) // 4, 50, 0.0, 0.0, 0.0, len(system) + len(user), True))
        return out


def make_client(cfg: Dict):
    backend = cfg.get("model_runtime", {}).get("backend", "ollama")
    if backend == "ollama":
        return OllamaClient.from_config(cfg)
    if backend == "mock":
        return MockClient()
    raise ValueError(f"Unsupported backend for real runs: {backend}")
