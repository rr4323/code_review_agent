"""
Graph-GRPO (Group Relative Policy Optimization) Post-Training & Reward Engine for Gemma 4.

Trains lightweight LoRA adapters over a shared 4-bit Gemma 4 base model:
1. `lora_category_analyzer`: Evaluates (Module_i, Category_k) scoped evidence slices.
2. `lora_node_merger`: Merges K Category Subagent evaluations + Child Node evaluations into
   a schema-valid <256-token `MergedNodeEvaluation` JSON contract.
3. `lora_patch_synthesizer`: Generates unified git diff patches from the Root Node evaluation.

Composite Verifiable Reward Function:
    R_total = alpha * R_loc + beta * R_test + gamma * R_rule - lambda * R_len
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Set


@dataclass
class GRPORewardBreakdown:
    """Detailed breakdown of the verifiable Graph-GRPO reward for a sampled trajectory."""
    r_localization: float
    r_test_and_patch: float
    r_rule_recall: float
    r_length_penalty: float
    r_total: float
    advantage: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)


class GraphGRPORewardEngine:
    """
    Computes execution-verified rewards for Group Relative Policy Optimization (GRPO)
    without requiring a frontier cloud teacher model.
    """

    def __init__(
        self,
        alpha_loc: float = 0.30,
        beta_test: float = 0.40,
        gamma_rule: float = 0.30,
        lambda_len: float = 0.15,
        max_message_tokens: int = 256,
    ):
        self.alpha = alpha_loc
        self.beta = beta_test
        self.gamma = gamma_rule
        self.lambda_len = lambda_len
        self.max_message_tokens = max_message_tokens

    def score_trajectory(
        self,
        predicted_modules: Set[str],
        ground_truth_modules: Set[str],
        predicted_rules: Set[str],
        ground_truth_rules: Set[str],
        patch_passes_tests: bool,
        perf_regression_resolved: bool,
        inter_node_message_tokens: int,
    ) -> GRPORewardBreakdown:
        """Computes R_total for a single sampled multi-agent graph execution trajectory."""
        # 1. Localization F1 Reward (R_loc in [0, 1])
        if not ground_truth_modules and not predicted_modules:
            r_loc = 1.0
        elif not ground_truth_modules or not predicted_modules:
            r_loc = 0.0
        else:
            tp = len(predicted_modules.intersection(ground_truth_modules))
            prec = tp / len(predicted_modules)
            rec = tp / len(ground_truth_modules)
            r_loc = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

        # 2. Execution-Verified Test & Performance Reward (R_test in [0, 1])
        r_test = 0.0
        if patch_passes_tests:
            r_test += 0.70
        if perf_regression_resolved:
            r_test += 0.30

        # 3. Guideline & Security Rule Recall (R_rule in [0, 1])
        if not ground_truth_rules:
            r_rule = 1.0
        else:
            r_rule = len(predicted_rules.intersection(ground_truth_rules)) / len(ground_truth_rules)

        # 4. Message Token Budget Penalty (R_len >= 0)
        overflow = max(0, inter_node_message_tokens - self.max_message_tokens)
        r_len = min(1.0, overflow / float(self.max_message_tokens))

        r_total = (
            self.alpha * r_loc
            + self.beta * r_test
            + self.gamma * r_rule
            - self.lambda_len * r_len
        )

        return GRPORewardBreakdown(
            r_localization=round(r_loc, 4),
            r_test_and_patch=round(r_test, 4),
            r_rule_recall=round(r_rule, 4),
            r_length_penalty=round(r_len, 4),
            r_total=round(r_total, 4),
        )

    def compute_group_advantages(
        self, group_rewards: List[GRPORewardBreakdown]
    ) -> List[GRPORewardBreakdown]:
        """
        Normalizes rewards across a group of G sampled trajectories for GRPO:
            A_i = (r_i - mean(r)) / (std(r) + 1e-8)
        """
        if not group_rewards:
            return []
        totals = [g.r_total for g in group_rewards]
        mean_r = sum(totals) / len(totals)
        var_r = sum((x - mean_r) ** 2 for x in totals) / len(totals)
        std_r = math.sqrt(var_r) + 1e-8

        for g in group_rewards:
            g.advantage = round((g.r_total - mean_r) / std_r, 4)
        return group_rewards

    @staticmethod
    def get_peft_lora_training_config(base_model: str = "google/gemma-4-9b-it") -> Dict:
        """Returns the HuggingFace PEFT + TRL GRPOConfig specification used for post-training."""
        return {
            "base_model_name_or_path": base_model,
            "quantization_config": {
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
                "bnb_4bit_compute_dtype": "bfloat16",
                "bnb_4bit_use_double_quant": True,
            },
            "lora_config": {
                "r": 16,
                "lora_alpha": 32,
                "lora_dropout": 0.05,
                "bias": "none",
                "task_type": "CAUSAL_LM",
                "target_modules": [
                    "q_proj", "k_proj", "v_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj",
                ],
                "adapters": [
                    "lora_category_analyzer",
                    "lora_node_merger",
                    "lora_patch_synthesizer",
                ],
            },
            "grpo_config": {
                "num_generations_per_prompt": 4,
                "max_prompt_length": 3072,
                "max_completion_length": 256,
                "learning_rate": 5e-6,
                "beta_kl_penalty": 0.04,
                "per_device_train_batch_size": 2,
                "gradient_accumulation_steps": 4,
            },
        }
