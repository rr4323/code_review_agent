"""Run every system on every CrossMod-Bench instance with real Gemma 4 (resumable)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List

from gs_mae.config import load_config
from gs_mae.llm import OllamaClient
from gs_mae.pipeline import review

ORDER_KEY = {"none_dereference": 0, "unit_mismatch": 1, "exception_mismatch": 2, "argument_order": 3, None: 4}


class RssSampler(threading.Thread):
    """Samples the resident memory of all ollama processes (model weights + KV cache)."""

    def __init__(self, interval: float = 1.0):
        super().__init__(daemon=True)
        self.interval, self.peak_mb, self._halt = interval, 0.0, threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            total = 0
            for pid in filter(str.isdigit, os.listdir("/proc")):
                try:
                    with open(f"/proc/{pid}/comm") as f:
                        comm = f.read()
                        if "ollama" not in comm and "llama-server" not in comm:
                            continue
                    with open(f"/proc/{pid}/status") as f:
                        for line in f:
                            if line.startswith("VmRSS:"):
                                total += int(line.split()[1])
                except OSError:
                    continue
            self.peak_mb = max(self.peak_mb, total / 1024)
            time.sleep(self.interval)

    def stop(self) -> float:
        self._halt.set()
        return round(self.peak_mb, 1)


def ordered_instances(root: Path) -> List[Path]:
    """Round-robin over bug types so partial runs stay balanced."""
    insts = sorted(p for p in root.iterdir() if (p / "ground_truth.json").exists())
    gts = {p: json.loads((p / "ground_truth.json").read_text()) for p in insts}
    buckets: Dict = {}
    for p in insts:
        buckets.setdefault(gts[p]["bug_type"], []).append(p)
    out = []
    while any(buckets.values()):
        for k in sorted(buckets, key=lambda k: ORDER_KEY[k]):
            if buckets[k]:
                out.append(buckets[k].pop(0))
    return out


def run_one(inst: Path, system: str, cfg: Dict, client, out_dir: Path) -> Dict:
    gt = json.loads((inst / "ground_truth.json").read_text())
    extra = {"tests/test_hidden.py": (inst / "hidden_test.py").read_text()}
    sampler = RssSampler()
    sampler.start()
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "merkle.json" if system == "gsmae" else None
        r = review(inst / "repo", gt["changed_files"], cfg, client, system, cache_path=cache,
                   repair=(system == "gsmae"), repair_extra_tests=extra)
        if system == "gsmae":
            # warm re-run on unchanged code: every node must come from the Merkle cache
            n0 = len(client.ledger.calls)
            t0 = time.perf_counter()
            warm = review(inst / "repo", gt["changed_files"], cfg, client, system, cache_path=cache)
            r["warm_rerun"] = {"cache_hits": warm["cache_hits"], "modules": len(warm["graph"]["modules"]),
                               "llm_calls": len(client.ledger.calls) - n0,
                               "seconds": round(time.perf_counter() - t0, 2)}
    r["peak_ollama_rss_mb"] = sampler.stop()
    r["ground_truth"] = gt
    r["instance_id"] = gt["instance_id"]
    (out_dir / f"{gt['instance_id']}__{system}.json").write_text(json.dumps(r, indent=1))
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", type=Path, default=Path("benchmark/instances"))
    ap.add_argument("--out", type=Path, default=Path("benchmark/results"))
    ap.add_argument("--systems", nargs="+", default=["tools_only", "flat", "gsmae_no_msg", "gsmae"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=None)
    a = ap.parse_args()

    cfg = load_config(overrides={"model_runtime.model": a.model} if a.model else None)
    client = OllamaClient.from_config(cfg)
    assert client.health(), f"model {client.model} not available"
    out_dir = a.out / client.model.replace(":", "_")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run_config.json").write_text(json.dumps(cfg, indent=1))
    insts = ordered_instances(a.instances)
    if a.limit:
        insts = insts[: a.limit]
    t_start = time.time()
    for i, inst in enumerate(insts, 1):
        for system in a.systems:
            gt = json.loads((inst / "ground_truth.json").read_text())
            target = out_dir / f"{gt['instance_id']}__{system}.json"
            if target.exists():
                continue
            t0 = time.time()
            r = run_one(inst, system, cfg, client if system != "tools_only" else None, out_dir)
            print(f"[{i}/{len(insts)}] {gt['instance_id']:<42} {system:<13} findings={len(r['findings']):<3} "
                  f"calls={r['usage']['llm_calls']:<3} {time.time() - t0:6.1f}s  (elapsed {(time.time() - t_start) / 60:.1f} min)",
                  flush=True)


if __name__ == "__main__":
    main()
