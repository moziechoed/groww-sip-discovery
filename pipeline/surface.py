"""Stage 5: Surface (PRD §9.5, FR5.1–FR5.3).

    python run.py --stage surface

Opportunity areas = the 17 stop scenarios of KPI Tree v1, using each stop-event post's primary (first) scenario.
(P8 change: PRD §9.5 suggested trigger_type × journey_stage × unmet_need, but on the golden set trigger_type
agreed 25% with the PM and journey_stage is unknown for most posts, while stop_scenario_ids agreed 75% and
maps directly onto the KPI tree.)

Score = Frequency × Segment Fit × Behavioural Specificity × Actionability (each 0–1), × 100.
- Frequency: cluster n ÷ largest cluster n.
- Segment Fit: mean post-level segment fit (scoring/segment_fit.py).
- Behavioural Specificity: share of posts with verified evidence AND a stated checking_what, mapped to 1–3 by
  scoring.specificity_thresholds, ÷ 3.
- Actionability: Gemini, 1–3 against written anchors, ÷ 3, with a one-line rationale (the PM spot-checks each).
`score_display` rescales so the top card = 100. Recency (median post date) breaks ties.
Clusters with n < 15 are "emerging" and not scored (FR5.2). Posts with no_fund_stock_flag are excluded (FR5.1).

One Gemini call writes titles, descriptions and actionability for all clusters (estimate + typed `yes`).
The result is cached in outputs/surface_llm.json and reused while the clusters are unchanged.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from core.budget import BudgetError, BudgetTracker, Estimate, Usage, compute_cost_inr, estimate_tokens, format_estimate
from core.config import Settings
from core.llm_client import LLMClient, LLMProvider, LLMUnavailable
from core.logger import get_logger
from pipeline.analyse import BRANCHES, SCENARIOS, STOP_BRANCHES, load_frame, theme_column

log = get_logger("surface")

STAGE = "surface"
PROMPT_FILE = "surface_v1_20261028.txt"
MIN_CARD_N = 15
MAX_QUOTE_WORDS = 30
QUOTES_PER_CARD = 5
GUARDRAIL_WORDS = re.compile(r"\b(?:urgent|hurry|fomo|don'?t miss|last chance|panic|fear|guaranteed|recommend(?:ed)?|best fund|buy now)\b", re.I)


class CardOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_id: int
    title: str
    description: str
    actionability: int = Field(ge=1, le=3)
    rationale: str


class CardsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cards: List[CardOut]


def _paths(settings: Settings) -> Dict[str, Path]:
    out = settings.paths.resolve("outputs")
    return {
        "enriched": settings.paths.resolve("enriched_dir") / "enriched_posts.jsonl",
        "themes": out / "themes.json",
        "llm": out / "surface_llm.json",
        "json": out / "opportunities.json",
        "csv": out / "opportunities.csv",
        "prompt": settings.paths.resolve("prompts") / PROMPT_FILE,
    }


def specificity_level(share: float, thresholds: Dict[str, float]) -> int:
    if share >= thresholds.get("high", 0.6):
        return 3
    if share >= thresholds.get("medium", 0.3):
        return 2
    return 1


# Words that show a quote is about its scenario (ranking only; quotes stay verbatim).
SCENARIO_CUES = {
    1: r"job|salary|expense|loan|emi|rent|marriage|wedding|emergency|debt|medical|fees|paise nahi|income",
    2: r"return|loss|negative|red|xirr|underperform|profit|growth|less than|fd",
    3: r"understand|confus|lock|nav|how|kya|kaise|kyu|why|unit|meaning|samajh",
    4: r"goal|reason|tax|purpose|need",
    5: r"stock|f&o|option|crypto|fd|gold|real estate|trading|ulip",
    6: r"friend|family|told|advice|colleague|youtube|influencer|father|papa",
    7: r"portfolio|consolidat|overlap|rebalanc|too many|direct|regular|switch fund|restructur",
    8: r"support|refund|trust|scam|fraud|delay|charged|deduct|customer care|complain|money",
    9: r"balance|insufficient|salary date|payday|bounce",
    10: r"bank|account change|new account|old account",
    11: r"mandate|autopay|auto pay|auto-pay|debit|nach|upi|e-?mandate|bounce|failed",
    12: r"kyc|verification|document",
    13: r"fund|scheme|amc|stopped accepting|paused by|merge",
    14: r"forgot|remind|pause|skip",
    15: r"restart|again|resume|re-?start",
    16: r"later|wait|guilt|market fall|correction",
    17: r"zerodha|coin|kuvera|switch|mov(?:e|ed|ing)|shift|transfer|another (?:app|platform|broker)|other app",
}
EXPECTED_BRANCH = {**{i: "voluntary_stop" for i in range(1, 9)},
                   **{i: "involuntary_payment_failure" for i in range(9, 14)},
                   **{i: "lapse_and_return" for i in range(14, 17)}, 17: "platform_switch"}
# Quotes that name a fund house or scheme are skipped so cards never read as fund commentary (guardrail).
FUND_NAMES = re.compile(r"\b(?:sbi|hdfc|icici|axis|kotak|nippon|parag|ppfas|quant|motilal|mirae|jm|uti|tata|dsp|canara|"
                        r"edelweiss|bandhan|franklin|aditya|birla|hsbc|invesco|mahindra|navi|whiteoak|bajaj|lic|"
                        r"sundaram|baroda|union|flexi ?cap|small ?cap|mid ?cap|large ?cap|nifty|sensex)\b", re.I)


def pick_quotes(g: pd.DataFrame, k: int = QUOTES_PER_CARD, scenario_id: Optional[int] = None) -> List[dict]:
    """Verbatim, verified evidence spans (≤30 words, no fund names), ranked by: mentions the scenario,
    branch matches the scenario, Groww-tagged, segment fit, recency; at most 2 per source on the first pass."""
    cand = g[g["evidence_verified"] & (g["evidence_span"] != "")].copy()
    cand["words"] = cand["evidence_span"].str.split().str.len()
    cand = cand[(cand["words"] <= MAX_QUOTE_WORDS) & ~cand["evidence_span"].str.contains(FUND_NAMES)]
    cue = SCENARIO_CUES.get(scenario_id or 0)
    cand["cue"] = cand["evidence_span"].str.contains(cue, case=False, regex=True).astype(int) if cue else 0
    cand["branch_ok"] = (cand["kpi_branch"] == EXPECTED_BRANCH.get(scenario_id or 0, "")).astype(int)
    cand["groww"] = (cand["platform_tag"] == "groww").astype(int)
    cand["seg"] = cand["segment_fit"].fillna(0)
    cand = cand.sort_values(["cue", "branch_ok", "groww", "seg", "date"], ascending=False)
    chosen, seen_text, src_count = [], set(), {}
    for _, r in cand.iterrows():  # first pass: at most 2 per source
        key = r.evidence_span.strip().lower()
        if key in seen_text or src_count.get(r.source, 0) >= 2:
            continue
        chosen.append(r)
        seen_text.add(key)
        src_count[r.source] = src_count.get(r.source, 0) + 1
        if len(chosen) == k:
            break
    if len(chosen) < k:
        for _, r in cand.iterrows():
            key = r.evidence_span.strip().lower()
            if key not in seen_text:
                chosen.append(r)
                seen_text.add(key)
                if len(chosen) == k:
                    break
    return [{"quote": r.evidence_span, "source": r.source, "url": r.url, "date": r.date[:10], "post_id": r.id,
             "outcome": r.outcome, "journey_stage": r.journey_stage} for r in chosen]


def build_clusters(df: pd.DataFrame, settings: Settings, themes: Optional[dict] = None) -> List[dict]:
    base = df[df["event"] & df["kpi_branch"].isin(STOP_BRANCHES) & (df["primary_scenario"] > 0)
              & ~df["no_fund_stock_flag"]]
    unmet = theme_column(df, themes, "unmet_need")
    confusion = theme_column(df, themes, "in_app_confusion")
    n_relevant = int(df["relevant"].sum())
    n_stop = len(base)
    thresholds = settings.scoring.specificity_thresholds
    clusters = []
    for sid, g in base.groupby("primary_scenario"):
        sid = int(sid)
        n = len(g)
        spec_share = float((g["evidence_verified"] & (g["checking_what"] != "")).mean())
        seg = g["segment_fit"].dropna()
        stages = g[g["journey_stage"] != "unknown"]["journey_stage"].value_counts()
        cont = int((g["outcome"].isin(["continued", "restarted"])).sum())
        drop = int((g["outcome"].isin(["stopped", "paused", "reduced"])).sum())
        u = unmet[g.index]
        c = confusion[g.index]
        dates = pd.to_datetime(g["date"], errors="coerce", utc=True).dropna()
        clusters.append({
            "cluster_id": sid, "scenario_id": sid, "scenario": SCENARIOS[sid], "n": n,
            "status": "scored" if n >= MIN_CARD_N else "emerging",
            "pct_of_relevant": round(100 * n / n_relevant, 1) if n_relevant else None,
            "pct_of_stop_events": round(100 * n / n_stop, 1) if n_stop else None,
            "segment_fit": round(float(seg.mean()), 3) if len(seg) else 0.0,
            "specificity_share": round(spec_share, 3),
            "specificity_level": specificity_level(spec_share, thresholds),
            "kpi_branches": {BRANCHES[k]: int(v) for k, v in g["kpi_branch"].value_counts().items()},
            "outcomes": {str(k): int(v) for k, v in g["outcome"].value_counts().items()},
            "continuer_vs_dropoff": {"continued_or_restarted": cont, "stopped_paused_reduced": drop,
                                     "considering_stop": int((g["outcome"] == "considering_stop").sum())},
            "journey_stages": {str(k): int(v) for k, v in stages.items()},
            "journey_stage_known_pct": round(100 * stages.sum() / n, 1),
            "source_distribution": {str(k): int(v) for k, v in g["source"].value_counts().items()},
            "groww_tagged": int((g["platform_tag"] == "groww").sum()),
            "segment_coverage_pct": round(100 * float(g["segment_confidence"].isin(["medium", "high"]).mean()), 1),
            "top_unmet_need_themes": [{"theme": str(k), "n": int(v)} for k, v in u[u != ""].value_counts().head(3).items()],
            "top_confusion_themes": [{"theme": str(k), "n": int(v)} for k, v in c[c != ""].value_counts().head(3).items()],
            "median_date": dates.sort_values().iloc[len(dates) // 2].strftime("%Y-%m-%d") if len(dates) else "",
            "quotes": pick_quotes(g, scenario_id=sid),
        })
    clusters.sort(key=lambda c: -c["n"])
    return clusters


def clusters_hash(clusters: List[dict], prompt_text: str) -> str:
    h = hashlib.sha256(prompt_text.encode())
    for c in clusters:
        h.update(f"{c['cluster_id']}|{c['n']}|{[q['post_id'] for q in c['quotes']]}\n".encode())
    return h.hexdigest()[:16]


def llm_payload(clusters: List[dict]) -> str:
    rows = []
    for c in clusters:
        rows.append({"cluster_id": c["cluster_id"], "stop_scenario": c["scenario"], "posts": c["n"],
                     "kpi_branches": c["kpi_branches"], "journey_stages": c["journey_stages"],
                     "unmet_need_themes": [t["theme"] for t in c["top_unmet_need_themes"]],
                     "quotes": [q["quote"] for q in c["quotes"]]})
    return json.dumps({"clusters": rows}, ensure_ascii=False)


def score_cards(clusters: List[dict], llm: Dict[int, dict]) -> List[dict]:
    scored = [c for c in clusters if c["status"] == "scored"]
    max_n = max((c["n"] for c in scored), default=1)
    for c in clusters:
        info = llm.get(c["cluster_id"], {})
        c["title"] = info.get("title") or c["scenario"]
        c["description"] = info.get("description", "")
        c["actionability"] = info.get("actionability")
        c["actionability_rationale"] = info.get("rationale", "")
        text = " ".join([c["title"], c["description"], c["actionability_rationale"]])
        c["guardrail_flags"] = sorted({m.group(0).lower() for m in GUARDRAIL_WORDS.finditer(text)})
        if c["status"] != "scored":
            continue
        f = {"frequency": round(c["n"] / max_n, 3),
             "segment_fit": round(c["segment_fit"], 3),
             "behavioural_specificity": round(c["specificity_level"] / 3, 3),
             "actionability": round((c["actionability"] or 0) / 3, 3)}
        c["factors"] = f
        c["score"] = round(100 * f["frequency"] * f["segment_fit"] * f["behavioural_specificity"] * f["actionability"], 2)
    top = max((c["score"] for c in scored), default=0) or 1
    for c in scored:
        c["score_display"] = round(100 * c["score"] / top, 1)
    scored.sort(key=lambda c: (-c["score"], -int(c["median_date"].replace("-", "") or 0)))
    for i, c in enumerate(scored, 1):
        c["rank"] = i
    return scored


def run_llm(settings: Settings, clusters: List[dict], approve: Callable[[], bool],
            provider: Optional[LLMProvider], api_key: Optional[str]) -> Optional[Dict[int, dict]]:
    p = _paths(settings)
    system = p["prompt"].read_text(encoding="utf-8")
    hsh = clusters_hash(clusters, system)
    if p["llm"].exists():
        cached = json.loads(p["llm"].read_text(encoding="utf-8"))
        if cached.get("hash") == hsh:
            print("Card text + actionability: cached in outputs/surface_llm.json (0 Gemini calls).")
            return {int(k): v for k, v in cached["cards"].items()}
    payload = llm_payload(clusters)
    in_tok = estimate_tokens(system) + estimate_tokens(payload)
    exp_out, max_out = 120 * len(clusters) + 200, 250 * len(clusters) + 500
    tracker = BudgetTracker(settings.budget, settings.paths.resolve("spend_log"), stage=STAGE,
                            model=settings.llm.model, prompt_version=PROMPT_FILE.rsplit(".", 1)[0])
    tracker.check_can_start()
    est = Estimate(STAGE + " (card text + actionability)", 1, in_tok, exp_out,
                   compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=exp_out), settings.budget),
                   compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=max_out), settings.budget),
                   note=f"{len(clusters)} clusters in one call")
    print(format_estimate(est, tracker))
    if not approve():
        return None
    client = LLMClient(settings, tracker, provider=provider, api_key=api_key)
    res = client.generate_json(prompt=payload, schema=CardsResponse, system=system, call_type="clustering",
                               batch_id=f"surface-{datetime.now(timezone.utc):%Y%m%d%H%M%S}",
                               max_output_tokens=max_out, expected_cost_inr=est.cost_inr)
    if res.status != "ok":
        print(f"Card call failed ({res.status}); cards will use scenario names and no actionability.")
        return {}
    out = {c.cluster_id: c.model_dump() for c in res.data.cards}
    p["llm"].write_text(json.dumps({"hash": hsh, "prompt": PROMPT_FILE, "cards": out,
                                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                   indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Surface spend ₹{tracker.run_spend:.2f}   project ₹{tracker.project_spend:.2f}")
    return out


CSV_COLS = ["rank", "status", "scenario_id", "title", "description", "score_display", "score", "frequency",
            "segment_fit", "behavioural_specificity", "actionability", "actionability_rationale", "n",
            "pct_of_relevant", "pct_of_stop_events", "continued_or_restarted", "stopped_paused_reduced",
            "considering_stop", "top_journey_stage", "groww_tagged", "segment_coverage_pct", "quote_1", "quote_2",
            "quote_3", "guardrail_flags"]


def _csv_row(c: dict) -> dict:
    f = c.get("factors", {})
    cv = c["continuer_vs_dropoff"]
    qs = [f"{q['quote']} ({q['source']})" for q in c["quotes"][:3]]
    return {"rank": c.get("rank", ""), "status": c["status"], "scenario_id": c["scenario_id"], "title": c["title"],
            "description": c["description"], "score_display": c.get("score_display", ""), "score": c.get("score", ""),
            "frequency": f.get("frequency", ""), "segment_fit": c["segment_fit"],
            "behavioural_specificity": f.get("behavioural_specificity", ""), "actionability": c.get("actionability", ""),
            "actionability_rationale": c["actionability_rationale"], "n": c["n"], "pct_of_relevant": c["pct_of_relevant"],
            "pct_of_stop_events": c["pct_of_stop_events"], "continued_or_restarted": cv["continued_or_restarted"],
            "stopped_paused_reduced": cv["stopped_paused_reduced"], "considering_stop": cv["considering_stop"],
            "top_journey_stage": next(iter(c["journey_stages"]), "unknown"), "groww_tagged": c["groww_tagged"],
            "segment_coverage_pct": c["segment_coverage_pct"],
            **{f"quote_{i + 1}": q for i, q in enumerate(qs)}, "guardrail_flags": ";".join(c["guardrail_flags"])}


def run_surface(settings: Settings, approve: Callable[[], bool] = lambda: False,
                provider: Optional[LLMProvider] = None, api_key: Optional[str] = None) -> int:
    p = _paths(settings)
    if not p["enriched"].exists():
        print("No enriched posts yet. Run --stage enrich first.")
        return 2
    df = load_frame(p["enriched"])
    themes = json.loads(p["themes"].read_text(encoding="utf-8")) if p["themes"].exists() else None
    if themes is None:
        print("Note: outputs/themes.json not found (run --stage analyse first for unmet-need themes on cards).")
    clusters = build_clusters(df, settings, themes)
    print(f"\n== Stage 5: Surface ==\nStop-event clusters: {len(clusters)}  "
          f"(scored n≥{MIN_CARD_N}: {sum(c['status'] == 'scored' for c in clusters)}, "
          f"emerging: {sum(c['status'] == 'emerging' for c in clusters)})")
    try:
        llm = run_llm(settings, clusters, approve, provider, api_key)
    except BudgetError as e:
        print(f"Budget stop: {e}")
        return 1
    except LLMUnavailable as e:
        print(f"Gemini unavailable (HTTP {e.code or 'unknown'}): {e.error_message}")
        return 1
    if llm is None:
        return 0
    scored = score_cards(clusters, llm)
    emerging = [c for c in clusters if c["status"] == "emerging"]
    disc = df[df["relevant"] & (df["discovered"] != "")]["discovered"].value_counts()
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": {
            "clusters": "primary stop scenario (KPI Tree v1) of each stop-event post; no_fund_stock_flag posts excluded",
            "score": "100 x frequency x segment_fit x behavioural_specificity x actionability; score_display: top = 100",
            "min_card_n": MIN_CARD_N, "prompt": PROMPT_FILE,
        },
        "cards": scored,
        "emerging": sorted(emerging, key=lambda c: -c["n"]),
        "emerging_discovered_categories": [{"category": k, "n": int(v)} for k, v in disc.items()],
    }
    p["json"].write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(p["csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for c in scored + out["emerging"]:
            w.writerow(_csv_row(c))
    print("\nRanked opportunity cards:")
    for c in scored:
        fx = c["factors"]
        print(f"  #{c['rank']} [{c['score_display']:>5}] {c['title']}  (n={c['n']}, A={c['actionability']}, "
              f"F={fx['frequency']}, SF={fx['segment_fit']}, BS={fx['behavioural_specificity']})")
        if c["guardrail_flags"]:
            print(f"      guardrail words to check: {', '.join(c['guardrail_flags'])}")
    print(f"Emerging (n<{MIN_CARD_N}): " + ", ".join(f"{c['scenario']} ({c['n']})" for c in out["emerging"]))
    print("Spot-check every actionability rationale before using a card in the deck.")
    print(f"Wrote outputs/{p['json'].name} and outputs/{p['csv'].name}")
    return 0
