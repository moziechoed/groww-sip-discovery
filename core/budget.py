"""Token + cost tracking and hard spend caps (Guardrails §6, Architecture §4.12).

Cost formula (per call):
    cost_usd = ( input_tokens                     * input_price_per_m_usd
               + (output_tokens + thinking_tokens) * output_price_per_m_usd
               + cached_tokens                     * cached_input_price_per_m_usd ) / 1_000_000
    cost_inr = cost_usd * usd_inr_rate

`input_tokens` excludes cached tokens (those are billed separately at the cached price).
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import BudgetCfg

SPEND_LOG_COLUMNS = [
    "timestamp", "stage", "batch_id", "model", "prompt_version",
    "input_tokens", "output_tokens", "thinking_tokens", "cached_tokens",
    "cost_inr", "cumulative_cost_inr", "retry_count", "call_status",
]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0      # non-cached prompt tokens
    output_tokens: int = 0     # candidate (visible output) tokens
    thinking_tokens: int = 0   # billed at the output price
    cached_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.thinking_tokens + other.thinking_tokens,
            self.cached_tokens + other.cached_tokens,
        )


def compute_cost_inr(usage: Usage, cfg: BudgetCfg) -> float:
    usd = (
        usage.input_tokens * cfg.input_price_per_m_usd
        + (usage.output_tokens + usage.thinking_tokens) * cfg.output_price_per_m_usd
        + usage.cached_tokens * cfg.cached_input_price_per_m_usd
    ) / 1_000_000
    return usd * cfg.usd_inr_rate


def estimate_tokens(text: str) -> int:
    """Rough pre-call token estimate (~4 chars/token). Replaced by measured averages after the 20-post test."""
    return max(1, math.ceil(len(text or "") / 4))


class BudgetError(Exception):
    """Base class: any budget stop. The caller must checkpoint and exit; never retry."""


class BudgetExceeded(BudgetError):
    """Per-run cap would be exceeded."""


class ProjectCapReached(BudgetError):
    """Project (cumulative) cap reached or would be exceeded."""


class CallCostAnomaly(BudgetError):
    """A single call cost more than `per_call_anomaly_multiplier` x its estimate."""


def read_project_spend(spend_log_path: Path) -> float:
    path = Path(spend_log_path)
    if not path.exists() or path.stat().st_size == 0:
        return 0.0
    total = 0.0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                total += float(row.get("cost_inr") or 0)
            except ValueError:
                continue
    return total


class BudgetTracker:
    """One instance per pipeline run (per stage invocation)."""

    def __init__(self, cfg: BudgetCfg, spend_log_path: Path, stage: str, model: str, prompt_version: str):
        self.cfg = cfg
        self.spend_log_path = Path(spend_log_path)
        self.stage = stage
        self.model = model
        self.prompt_version = prompt_version
        self.project_spend_at_start = read_project_spend(self.spend_log_path)
        self.run_spend = 0.0
        self.calls = 0
        self.halt_reason: Optional[str] = None

    @property
    def project_spend(self) -> float:
        return self.project_spend_at_start + self.run_spend

    @property
    def per_run_remaining(self) -> float:
        return self.cfg.per_run_cap_inr - self.run_spend

    @property
    def project_remaining(self) -> float:
        return self.cfg.project_cap_inr - self.project_spend

    def check_can_start(self) -> None:
        if self.project_spend >= self.cfg.project_cap_inr:
            raise ProjectCapReached(
                f"Project spend ₹{self.project_spend:.2f} has reached the ₹{self.cfg.project_cap_inr:.0f} cap. "
                "Refusing to start. Only the PM can raise budget.project_cap_inr manually."
            )

    def preflight(self, worst_case_cost_inr: float) -> None:
        """Called before every Gemini call. Stops *before* a call that could breach a cap."""
        if self.halt_reason:
            raise CallCostAnomaly(self.halt_reason)
        if self.run_spend + worst_case_cost_inr > self.cfg.per_run_cap_inr:
            raise BudgetExceeded(
                f"Per-run cap: spent ₹{self.run_spend:.2f} this run; next call could cost up to "
                f"₹{worst_case_cost_inr:.2f} (cap ₹{self.cfg.per_run_cap_inr:.0f})."
            )
        if self.project_spend + worst_case_cost_inr > self.cfg.project_cap_inr:
            raise ProjectCapReached(
                f"Project cap: total ₹{self.project_spend:.2f}; next call could cost up to "
                f"₹{worst_case_cost_inr:.2f} (cap ₹{self.cfg.project_cap_inr:.0f})."
            )

    def record(
        self,
        usage: Usage,
        batch_id: str,
        retry_count: int = 0,
        call_status: str = "ok",
        expected_cost_inr: Optional[float] = None,
    ) -> dict:
        """Log one call. Never raises: the result of a paid call is never thrown away.
        Anomalies are flagged here and enforced at the next preflight()."""
        cost = compute_cost_inr(usage, self.cfg)
        self.run_spend += cost
        self.calls += 1
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stage": self.stage,
            "batch_id": batch_id,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "thinking_tokens": usage.thinking_tokens,
            "cached_tokens": usage.cached_tokens,
            "cost_inr": round(cost, 6),
            "cumulative_cost_inr": round(self.project_spend, 6),
            "retry_count": retry_count,
            "call_status": call_status,
        }
        self._append(row)
        if expected_cost_inr and expected_cost_inr > 0 and cost > self.cfg.per_call_anomaly_multiplier * expected_cost_inr:
            self.halt_reason = (
                f"Call {batch_id} cost ₹{cost:.4f}, more than {self.cfg.per_call_anomaly_multiplier:g}x the "
                f"estimate (₹{expected_cost_inr:.4f}). Stopping to report."
            )
        return row

    def _append(self, row: dict) -> None:
        self.spend_log_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.spend_log_path.exists() or self.spend_log_path.stat().st_size == 0
        with open(self.spend_log_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SPEND_LOG_COLUMNS)
            if new_file:
                w.writeheader()
            w.writerow(row)


# --------------------------------------------------------------------------- estimates


@dataclass
class Estimate:
    stage: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_inr: float
    worst_case_cost_inr: float
    posts_total: Optional[int] = None
    posts_done: int = 0
    batch_size: Optional[int] = None
    thinking_tokens: int = 0
    note: str = ""


def estimate_batched(
    stage: str,
    posts_total: int,
    posts_done: int,
    batch_size: int,
    cfg: BudgetCfg,
    max_output_tokens_per_post: int,
    avg_input_tokens_per_post: Optional[float] = None,
    avg_output_tokens_per_post: Optional[float] = None,
    prompt_overhead_tokens_per_call: Optional[int] = None,
) -> Estimate:
    d = cfg.estimate_defaults
    a_in = avg_input_tokens_per_post or d.avg_input_tokens_per_post
    a_out = avg_output_tokens_per_post or d.avg_output_tokens_per_post
    overhead = prompt_overhead_tokens_per_call or d.prompt_overhead_tokens_per_call
    remaining = max(posts_total - posts_done, 0)
    calls = math.ceil(remaining / batch_size) if remaining else 0
    in_tok = int(remaining * a_in + calls * overhead)
    out_tok = int(remaining * a_out)
    cost = compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=out_tok), cfg)
    worst = compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=remaining * max_output_tokens_per_post), cfg)
    return Estimate(stage, calls, in_tok, out_tok, cost, worst, posts_total, posts_done, batch_size)


def estimate_single_call(stage: str, input_tokens: int, expected_output_tokens: int,
                         max_output_tokens: int, cfg: BudgetCfg, note: str = "") -> Estimate:
    cost = compute_cost_inr(Usage(input_tokens=input_tokens, output_tokens=expected_output_tokens), cfg)
    worst = compute_cost_inr(Usage(input_tokens=input_tokens, output_tokens=max_output_tokens), cfg)
    return Estimate(stage, 1, input_tokens, expected_output_tokens, cost, worst, note=note)


def format_estimate(est: Estimate, tracker: BudgetTracker) -> str:
    bar = "=" * 56
    lines = [bar, f"Pre-run Estimate — Stage: {est.stage}"]
    if est.posts_total is not None:
        lines.append(f"Posts to process : {max(est.posts_total - est.posts_done, 0):,}  (already done: {est.posts_done:,})")
        lines.append(f"Batch size       : {est.batch_size}  →  {est.calls:,} calls")
    else:
        lines.append(f"Calls            : {est.calls}" + (f"  ({est.note})" if est.note else ""))
    lines += [
        f"Est. input tokens: ~{est.input_tokens:,}   Est. output tokens: ~{est.output_tokens:,}",
        f"Thinking tokens  : ~{est.thinking_tokens:,} (lowest allowed level)",
        f"Est. cost        : ₹{est.cost_inr:.2f}   (worst case ₹{est.worst_case_cost_inr:.2f})",
        f"Cumulative spend : ₹{tracker.project_spend:.2f}",
        f"Budget remaining : ₹{tracker.per_run_remaining:.2f} (per-run) / ₹{tracker.project_remaining:.2f} (project)",
    ]
    if est.worst_case_cost_inr > tracker.per_run_remaining:
        lines.append(f"NOTE: worst case exceeds the per-run cap; the run will checkpoint and stop at ₹{tracker.cfg.per_run_cap_inr:.0f}.")
    lines.append(bar)
    return "\n".join(lines)
