"""Deterministic post-level Segment Fit (PRD §9.5). Never LLM-judged.

| Signal                                          | Weight | Match | Unknown | Mismatch |
| employment = salaried                           | 0.30   | 1     | 0.4     | 0        |
| age 22–28 or years_of_work 0–4                  | 0.25   | 1     | 0.4     | 0        |
| first-time investor = yes, or SIP < 12 months   | 0.25   | 1     | 0.4     | 0        |
| city_tier = tier_1                              | 0.10   | 1     | 0.5     | 0.2      |
| income_stability = stable_monthly               | 0.10   | 1     | 0.5     | 0.2      |
"""
from __future__ import annotations

from typing import Optional

from core.schemas import EnrichmentFields

EARLY_STAGES = {"month_0_1", "month_2_3", "month_4_6", "month_7_12"}


def _employment(v: str) -> float:
    return 0.4 if v == "unknown" else (1.0 if v == "salaried" else 0.0)


def _age_or_tenure(age: str, years: str) -> float:
    if age == "22_28" or years in ("0_1", "2_4"):
        return 1.0
    if age == "unknown" and years == "unknown":
        return 0.4
    return 0.0  # at least one is known and neither matches


def _first_time(first: str, stage: str) -> float:
    if first == "yes" or stage in EARLY_STAGES:
        return 1.0
    if first == "no" or stage == "month_12_plus":
        return 0.0
    return 0.4


def _city(v: str) -> float:
    return 0.5 if v == "unknown" else (1.0 if v == "tier_1" else 0.2)


def _income(v: str) -> float:
    return 0.5 if v == "unknown" else (1.0 if v == "stable_monthly" else 0.2)


def segment_fit(fields: Optional[EnrichmentFields]) -> Optional[float]:
    """0.0–1.0 for relevant posts; None for irrelevant or failed posts (they are not scored)."""
    if fields is None or not fields.is_relevant:
        return None
    s = fields.segment_signals
    score = (
        0.30 * _employment(s.employment)
        + 0.25 * _age_or_tenure(s.age_band, s.years_of_work)
        + 0.25 * _first_time(fields.is_first_time_investor, fields.journey_stage)
        + 0.10 * _city(s.city_tier)
        + 0.10 * _income(s.income_stability)
    )
    return round(score, 4)
