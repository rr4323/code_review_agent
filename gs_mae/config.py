"""Configuration loading for GS-MAE."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "gs_mae_config.yaml"


def load_config(path: Optional[Path] = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load the YAML config and return the `code_review_agent` section (with optional overrides)."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg = copy.deepcopy(raw.get("code_review_agent", raw))
    for dotted, value in (overrides or {}).items():
        node = cfg
        keys = dotted.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    _validate_live_api(cfg)
    return cfg


def enabled_categories(cfg: Dict[str, Any]) -> List[str]:
    return [c for c, v in cfg.get("categories", {}).items() if (v or {}).get("enabled", False)]


def _validate_live_api(cfg: Dict[str, Any]) -> None:
    """Fail early for unsafe or unusable opt-in live API settings."""
    api = cfg.get("live_api", {}) or {}
    if not api.get("enabled", False):
        return
    command = api.get("start_command")
    if not isinstance(command, (str, list)) or not command:
        raise ValueError("live_api.enabled requires a non-empty live_api.start_command")
    if not str(api.get("base_url", "")).startswith(("http://", "https://")):
        raise ValueError("live_api.base_url must be an HTTP URL")
    for key in ("repetitions", "warmup_runs", "max_operations"):
        if int(api.get(key, 1)) < 0:
            raise ValueError(f"live_api.{key} cannot be negative")


def config_fingerprint(cfg: Dict[str, Any]) -> str:
    """Stable hash of everything that can change an evaluation (rules, categories, model)."""
    relevant = {
        "categories": cfg.get("categories"),
        "guidelines": cfg.get("guidelines"),
        "spawn_policy": cfg.get("spawn_policy"),
        "performance": cfg.get("performance"),
        "live_api": cfg.get("live_api"),
        "execution": cfg.get("execution"),
        "model": cfg.get("model_runtime", {}).get("model"),
        "temperature": cfg.get("model_runtime", {}).get("temperature"),
    }
    blob = json.dumps(relevant, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
