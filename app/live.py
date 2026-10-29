"""Live Analysis for the Streamlit demo (PRD §8.4, UI4, UI7, AC10).

One Gemini call per post does both the safety check and the enrichment (enrich prompt v3 + live addendum).
Before any call, cheap local checks decline empty, over-long and fund-recommendation inputs (0 cost).
After the call, the evidence span is verified verbatim; labels without verifiable evidence are downgraded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

from core.budget import BudgetTracker, Usage, compute_cost_inr, estimate_tokens
from core.config import Settings
from core.llm_client import LLMClient, LLMProvider
from core.schemas import EnrichmentFields
from core.utils import verify_evidence_span
from pipeline.clean import scrub_pii
from pipeline.enrich import downgrade, needs_evidence, postprocess

LIVE_PROMPT_FILE = "live_v1_20261028.txt"
LIVE_POST_ID = "live-1"
CONF_RANK = {"low": 0, "medium": 1, "high": 2}

AMFI_URL = "https://www.amfiindia.com/investor"
MFSH_URL = "https://www.mutualfundssahihai.com/"
DECLINE_FUND = ("This demo doesn't recommend funds, stocks or amounts. For neutral investor education, see "
                f"AMFI ({AMFI_URL}) or Mutual Funds Sahi Hai ({MFSH_URL}).")
NO_SIGNAL = "Not enough signal to classify."
TOO_LONG = "Please paste at most {n:,} characters."
EMPTY_INPUT = "Paste a post first."

# Local pre-check: obvious requests for picks are declined without a Gemini call.
FUND_ASK = re.compile(
    r"\b(which|best|top)\b[^.?!\n]{0,30}\b(funds?|stocks?|schemes?)\b[^.!\n]{0,40}\?"
    r"|\b(suggest|recommend)(\s+me)?(\s+(some|a|any|good|best))?\s+(funds?|stocks?|schemes?)\b"
    r"|\bshould i (buy|invest in)\b"
    r"|\b(kaun ?sa|konsa)\s+(fund|mutual fund|stock)\b", re.I)


class LiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    safety: Literal["ok", "off_topic", "fund_recommendation_request"]
    items: List[EnrichmentFields]


@dataclass
class LiveResult:
    status: str                       # ok | declined_fund | no_signal | too_long | empty | error
    message: str = ""
    fields: Optional[Dict[str, Any]] = None
    evidence_verified: Optional[bool] = None
    cost_inr: float = 0.0
    called_llm: bool = False
    notes: List[str] = field(default_factory=list)


def precheck(text: str, max_chars: int) -> Optional[LiveResult]:
    t = (text or "").strip()
    if not t:
        return LiveResult("empty", EMPTY_INPUT)
    if len(t) > max_chars:
        return LiveResult("too_long", TOO_LONG.format(n=max_chars))
    if FUND_ASK.search(t):
        return LiveResult("declined_fund", DECLINE_FUND)
    return None


def system_prompt(settings: Settings) -> str:
    pdir = settings.paths.resolve("prompts")
    return (pdir / settings.enrich.prompt_file).read_text(encoding="utf-8") + \
        (pdir / LIVE_PROMPT_FILE).read_text(encoding="utf-8")


def user_prompt(text: str) -> str:
    import json
    return "Posts (JSON):\n" + json.dumps([{"post_id": LIVE_POST_ID, "source": "live_demo",
                                             "platform_hint": "unspecified", "clean_borderline": False,
                                             "hinglish": False, "text": text}], ensure_ascii=False)


def analyse_post(text: str, settings: Settings, provider: Optional[LLMProvider] = None,
                 api_key: Optional[str] = None, tracker: Optional[BudgetTracker] = None) -> LiveResult:
    live = settings.live
    pre = precheck(text, live.max_input_chars)
    if pre:
        return pre
    masked, _ = scrub_pii(text.strip())
    system = system_prompt(settings)
    prompt = user_prompt(masked)
    tracker = tracker or BudgetTracker(settings.budget, settings.paths.resolve("spend_log"), stage="live",
                                       model=settings.llm.model, prompt_version=LIVE_PROMPT_FILE.rsplit(".", 1)[0])
    client = LLMClient(settings, tracker, provider=provider, api_key=api_key)
    max_out = settings.llm.max_output_tokens.get("live", 800)
    exp = compute_cost_inr(Usage(input_tokens=estimate_tokens(system) + estimate_tokens(prompt), output_tokens=300),
                           settings.budget)
    res = client.generate_json(prompt=prompt, schema=LiveResponse, system=system, batch_id="live",
                               max_output_tokens=max_out, call_type="tagging", expected_cost_inr=exp)
    cost = compute_cost_inr(res.usage, settings.budget)
    if res.status != "ok" or not res.data.items:
        return LiveResult("error", "The analysis failed. Please try again.", cost_inr=cost, called_llm=True)
    if res.data.safety == "fund_recommendation_request":
        return LiveResult("declined_fund", DECLINE_FUND, cost_inr=cost, called_llm=True)
    f = res.data.items[0]
    if res.data.safety == "off_topic" or not f.is_relevant:
        return LiveResult("no_signal", NO_SIGNAL, cost_inr=cost, called_llm=True)
    if CONF_RANK.get(f.enrichment_confidence, 0) < CONF_RANK.get(live.min_confidence, 1):
        return LiveResult("no_signal", NO_SIGNAL, cost_inr=cost, called_llm=True,
                          notes=["The engine's confidence was below the demo threshold."])
    f, _ = postprocess(f, {"raw_meta": {}})
    verified = None
    notes = []
    if needs_evidence(f):
        verified = verify_evidence_span(f.evidence_span, masked)
        if not verified:
            f, _ = downgrade(f)
            notes.append("The quoted evidence could not be found word for word in the post, so the outcome and "
                         "trigger were set to unknown.")
    return LiveResult("ok", "", fields=f.model_dump(exclude={"post_id"}), evidence_verified=verified,
                      cost_inr=cost, called_llm=True, notes=notes)
