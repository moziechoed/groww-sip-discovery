"""Loads config.yaml (validated) and secrets (.env locally, Streamlit Secrets in deployment)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


class _Strict(BaseModel):
    # Unknown keys are an error, so a typo in config.yaml can't silently disable a guardrail.
    model_config = ConfigDict(extra="forbid")


class RunCfg(_Strict):
    window_months: int = 24
    target_min: int = 3000
    target_max: int = 4000
    source_cap_pct: int = Field(40, ge=1, le=100)
    prompt_version: str = "v1"
    sample_size: int = 20


class KeywordsCfg(_Strict):
    core: List[str]
    continuer: List[str]
    use_continuer_group: bool = False
    anchors: List[str] = Field(default_factory=list)    # P1 app-store matcher: standalone SIP/MF terms
    hinglish: List[str] = Field(default_factory=list)   # P1 app-store matcher: Roman Hinglish phrases (core group)


class CollectCfg(_Strict):
    page_delay_sec: float = Field(1.0, ge=0)
    max_retries: int = Field(3, ge=0, le=5)
    backoff_base_sec: float = Field(2, ge=0)
    request_timeout_sec: float = Field(20, gt=0)
    old_streak_stop: int = Field(20, ge=1)


class LLMCfg(_Strict):
    provider: str = "gemini"
    model: str
    batch_size: int = Field(30, ge=1, le=40)
    max_retries: int = Field(3, ge=0, le=3)
    backoff_base_sec: float = 2
    inter_batch_pause_sec: float = 4
    temperature: float = Field(0.1, ge=0.0, le=0.2)
    thinking_param: Literal["thinking_level", "thinking_budget"] = "thinking_level"
    thinking_level_lowest: str = "minimal"
    thinking_level_clustering: str = "minimal"
    max_input_chars_per_post: int = 1500
    max_output_tokens: Dict[str, int]


class LiveCfg(_Strict):
    session_call_limit: int = 3
    daily_demo_cap: int = 50
    min_confidence: Literal["high", "medium", "low"] = "medium"
    max_input_chars: int = 2000


class EstimateDefaults(_Strict):
    avg_input_tokens_per_post: int = 350
    avg_output_tokens_per_post: int = 200
    prompt_overhead_tokens_per_call: int = 1500


class BudgetCfg(_Strict):
    per_run_cap_inr: float = 300
    project_cap_inr: float = 1000
    usd_inr_rate: float = 96
    input_price_per_m_usd: float = 0.75
    output_price_per_m_usd: float = 3.75
    cached_input_price_per_m_usd: float = 0.075
    thinking_budget_tagging: int = 0
    # Literal[True]: setting this to false is a config error. The approval gate has no bypass.
    require_approval: Literal[True] = True
    per_call_anomaly_multiplier: float = 3.0
    thinking_warn_ratio: float = 0.10
    estimate_defaults: EstimateDefaults = EstimateDefaults()


class ScoringCfg(_Strict):
    specificity_thresholds: Dict[str, float]


class CleanCfg(_Strict):
    min_words: int = Field(5, ge=1)
    min_chars: int = Field(20, ge=1)
    non_latin_max_ratio: float = Field(0.50, ge=0.0, le=1.0)
    near_dupe_threshold: float = Field(90.0, ge=50.0, le=100.0)
    near_dupe_min_chars: int = Field(40, ge=1)
    reddit_stratification_damping: Literal["sqrt", "proportional"] = "sqrt"


class EnrichCfg(_Strict):
    """P7 Enrich + golden-set settings."""
    prompt_file: str = "enrich_v3_20261027.txt"      # FR3.4: versioned, dated prompt in /prompts
    output_floor_tokens_per_post: int = Field(500, ge=50)   # max_output_tokens never below this per post
    output_headroom: float = Field(1.5, ge=1.0, le=3.0)     # measured avg output/post x this
    test_sample_size: int = Field(20, ge=1, le=100)
    golden_size_stratified: int = Field(12, ge=0)
    golden_size_ambiguous: int = Field(8, ge=0)
    golden_seed: int = 42
    # The full run refuses to start until golden_human.csv is labelled, so the PM labels blind (FR6.2).
    require_golden_labels_before_full_run: bool = True
    retry_missing_once: bool = True
    retry_evidence_once: bool = True


class PathsCfg(_Strict):
    outputs: str = "outputs"
    prompts: str = "prompts"
    checkpoints: str = "data/checkpoints"
    raw_dir: str = "data/raw"
    clean_dir: str = "data/clean"
    context_dir: str = "data/context"
    enriched_dir: str = "data/enriched"
    seeds_dir: str = "data/seeds"
    drop_log: str = "outputs/drop_log.csv"
    spend_log: str = "outputs/spend_log.csv"
    corpus_stats: str = "outputs/corpus_stats.json"

    def resolve(self, name: str) -> Path:
        p = Path(getattr(self, name))
        return p if p.is_absolute() else ROOT / p


class Settings(_Strict):
    run: RunCfg
    keywords: KeywordsCfg
    sources: Dict[str, Dict[str, Any]]
    collect: CollectCfg = CollectCfg()
    clean: CleanCfg = CleanCfg()
    enrich: EnrichCfg = EnrichCfg()
    llm: LLMCfg
    live: LiveCfg
    budget: BudgetCfg
    scoring: ScoringCfg
    paths: PathsCfg = PathsCfg()


def load_raw_config(path: Optional[Path] = None) -> Dict[str, Any]:
    with open(path or DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(path: Optional[Path] = None) -> Settings:
    return Settings.model_validate(load_raw_config(path))


_PLACEHOLDER_PREFIXES = ("your-", "your_", "<", "changeme", "xxx")


def get_api_key(name: str = "GEMINI_API_KEY") -> Optional[str]:
    """Return the key from .env / environment, else Streamlit Secrets. Placeholders count as missing."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass
    value = os.environ.get(name)
    if not value:
        try:
            import streamlit as st  # only present in the deployed app

            value = st.secrets.get(name)
        except Exception:
            value = None
    if not value:
        return None
    value = value.strip()
    if not value or value.lower().startswith(_PLACEHOLDER_PREFIXES):
        return None
    return value
