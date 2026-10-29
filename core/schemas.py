"""Pydantic data contracts (Architecture §5, PRD v1.2 §9.3).

Segment sub-field enums are a P0 design choice (the PRD names the fields but not their values);
they are structured so that scoring/segment_fit.py can stay deterministic. Review before P7.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

JourneyStage = Literal[
    "pre_start", "month_0_1", "month_2_3", "month_4_6", "month_7_12", "month_12_plus", "post_stop", "unknown",
]
Outcome = Literal["continued", "paused", "reduced", "stopped", "restarted", "considering_stop", "unknown"]
TriggerType = Literal[
    "life_event_financial_pressure", "emotional", "ux_friction", "financial_literacy_gap", "feature_gap",
    "social_peer_influence", "returns_disappointment", "other", "none", "unknown",
]
CheckingFrequency = Literal["daily", "weekly", "monthly", "rarely", "unknown"]
OutsideAppBehaviour = Literal[
    "asked_family_friends", "youtube", "reddit_forums", "advisor", "news", "did_nothing", "other", "unknown",
]
Confidence = Literal["high", "medium", "low"]
YesNoUnknown = Literal["yes", "no", "unknown"]
SeedCategory = Literal[
    "ux_friction", "emotional_triggers", "financial_literacy_gaps", "feature_gaps",
    "life_event_financial_pressure", "social_peer_influence",
]
Platform = Literal["groww", "zerodha", "indmoney", "paytm_money", "mfcentral", "other", "unspecified"]

# P7: ties each post to the KPI tree (v1, 26 Oct). A = voluntary_stop, B = involuntary_payment_failure,
# C = lapse_and_return, plus platform_switch (scenario 17) and continuing.
KPIBranch = Literal[
    "voluntary_stop", "involuntary_payment_failure", "lapse_and_return", "platform_switch", "continuing",
    "not_applicable",
]
STOP_SCENARIO_MIN, STOP_SCENARIO_MAX, STOP_SCENARIO_MAX_PER_POST = 1, 17, 3

# Segment sub-fields (each allows "unknown"; never inferred without textual evidence, FR3.1)
AgeBand = Literal["under_22", "22_28", "29_35", "over_35", "unknown"]
Employment = Literal["salaried", "self_employed", "student", "other", "unknown"]
YearsOfWork = Literal["0_1", "2_4", "5_plus", "unknown"]
CityTier = Literal["tier_1", "tier_2", "tier_3", "unknown"]
IncomeStability = Literal["stable_monthly", "variable", "unstable", "unknown"]
SourceOfMoney = Literal["salary", "bonus", "savings", "family", "other", "unknown"]
Influencer = Literal[
    "family", "friends_peers", "colleagues", "finfluencer_youtube", "social_media", "advisor",
    "app_marketing", "self", "other", "unknown",
]


WORD_LIMITS = {
    "start_reason": 20, "expectation": 20, "trigger_detail": 25, "checking_what": 15, "unmet_need": 25,
    "in_app_confusion": 25,
}


def _truncate_words(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    words = value.split()
    return " ".join(words[:limit]) if len(words) > limit else value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------- posts


class RawPost(_Model):
    id: str
    source: str
    url: str
    date: str                                   # ISO 8601
    author_hash: str                            # SHA-256 + salt; never a plain username (FR1.4)
    text: str
    collection_method: Literal["scraped", "manual"]
    keyword_group: Literal["core", "continuer", "manual"] = "core"
    raw_meta: Dict[str, Any] = Field(default_factory=dict)


class CleanPost(RawPost):
    hinglish: bool = False
    pii_stripped: bool = False
    clean_text: str
    clean_borderline: bool = False


class SegmentSignals(_Model):
    age_band: AgeBand = "unknown"
    employment: Employment = "unknown"
    years_of_work: YearsOfWork = "unknown"
    city_tier: CityTier = "unknown"
    income_stability: IncomeStability = "unknown"
    source_of_money: SourceOfMoney = "unknown"
    influencer: Influencer = "unknown"


# Gemini generates JSON properties alphabetically unless told otherwise; this order makes it decide
# relevance and outcome first, then pick the evidence that supports them, then the descriptive fields.
ENRICHMENT_PROPERTY_ORDER = [
    "post_id", "is_relevant", "outcome", "journey_stage", "trigger_type", "kpi_branch", "stop_scenario_ids",
    "evidence_span", "seeded_category_primary", "seeded_category_secondary", "discovered_category",
    "trigger_detail", "start_reason", "expectation", "checking_what", "checking_frequency", "in_app_confusion",
    "outside_app_behaviour", "unmet_need", "segment_signals", "segment_confidence", "is_first_time_investor",
    "platform_tag", "no_fund_stock_flag", "enrichment_confidence",
]


class EnrichmentFields(_Model):
    """What the LLM returns per post. Never contains the post text (Guardrails §3)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"propertyOrdering": ENRICHMENT_PROPERTY_ORDER})

    post_id: str
    is_relevant: bool
    journey_stage: JourneyStage = "unknown"
    outcome: Outcome = "unknown"
    start_reason: Optional[str] = None
    expectation: Optional[str] = None
    trigger_type: TriggerType = "unknown"
    trigger_detail: Optional[str] = None
    checking_what: Optional[str] = None
    checking_frequency: CheckingFrequency = "unknown"
    in_app_confusion: Optional[str] = None
    outside_app_behaviour: OutsideAppBehaviour = "unknown"
    unmet_need: Optional[str] = None
    segment_signals: SegmentSignals = Field(default_factory=SegmentSignals)
    segment_confidence: Confidence = "low"
    is_first_time_investor: YesNoUnknown = "unknown"
    seeded_category_primary: Optional[SeedCategory] = None
    seeded_category_secondary: Optional[SeedCategory] = None
    discovered_category: Optional[str] = None
    platform_tag: Platform = "unspecified"
    no_fund_stock_flag: bool = False
    enrichment_confidence: Confidence = "low"
    evidence_span: Optional[str] = None         # verbatim; never truncated or rewritten
    kpi_branch: KPIBranch = "not_applicable"
    # 0–3 ids from the 17 stop scenarios. List[int] (not an integer enum) because Gemini's schema
    # rejects integer enums; the range is enforced here. Out-of-range ids are dropped, not fatal.
    stop_scenario_ids: List[int] = Field(default_factory=list)

    @field_validator("start_reason", "expectation", "trigger_detail", "checking_what", "unmet_need",
                     "in_app_confusion")
    @classmethod
    def _limit_words(cls, v: Optional[str], info):
        return _truncate_words(v, WORD_LIMITS[info.field_name])

    @field_validator("stop_scenario_ids")
    @classmethod
    def _clean_scenarios(cls, v: List[int]) -> List[int]:
        out: List[int] = []
        for i in v or []:
            if STOP_SCENARIO_MIN <= int(i) <= STOP_SCENARIO_MAX and int(i) not in out:
                out.append(int(i))
        return out[:STOP_SCENARIO_MAX_PER_POST]


class EnrichmentBatchResponse(_Model):
    """Schema sent to Gemini for one batch call: one item per input post_id."""

    items: List[EnrichmentFields]


class EnrichedPost(CleanPost):
    enrichment: Optional[EnrichmentFields] = None
    evidence_verified: bool = False
    segment_fit: Optional[float] = None
    prompt_version: str
    enriched_at: str
    status: Literal["ok", "enrichment_failed", "evidence_span_failed"] = "ok"
    # When the evidence check fails twice, the labels it could not back up are moved here (audit only;
    # never scored) and the live fields are downgraded to unknown, so AC5 holds by construction.
    unverified_labels: Optional[Dict[str, Any]] = None


# ----------------------------------------------------------------------------- outputs


class Quote(_Model):
    text: str
    source: str
    url: str


class Opportunity(_Model):
    id: str
    rank: int
    title: str
    description: str
    score: float
    factors: Dict[str, float]                   # frequency, segment_fit, specificity, actionability
    actionability_rationale: str
    rubric_notes: Optional[str] = None
    n_posts: int
    pct_corpus: float
    stages: List[JourneyStage]
    continuer_dropoff_ratio: Optional[float] = None
    quotes: List[Quote]
    source_distribution: Dict[str, int]
    category_type: Literal["seeded", "discovered"]
    emerging: bool = False
    segment_coverage: Dict[str, float] = Field(default_factory=dict)


class EvalItem(_Model):
    post_id: str
    bucket: Literal["stratified", "ambiguous"]
    field: str
    engine_label: str
    human_label: str
    agree: bool
    failure_reason: Optional[str] = None


# ----------------------------------------------------------------------------- P0 smoke test


class SmokeTestResult(_Model):
    post_id: str
    is_relevant: bool
    outcome: Outcome
    evidence_span: Optional[str] = None
