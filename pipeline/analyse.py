"""Stage 4: Analyse (PRD §9.4, FR4.1–FR4.7).

    python run.py --stage analyse

1. Theme clustering (the only Gemini calls): the short free-text fields written by Enrich
   (unmet_need, in_app_confusion, start_reason, expectation) are grouped into 5–12 themes each.
   Four calls, behind the usual estimate + typed `yes`. Results are cached in outputs/themes.json and
   reused (0 calls) until the items or the prompt change.
2. Statistics (pandas, no LLM): KPI-tree sizing, the 9 research questions, continuers vs drop-offs,
   segment cuts with n, co-occurrence, seeded vs discovered, continuer floor. Written to outputs/analysis.json.

Populations
- relevant : is_relevant and not enrichment_failed (counts, free-text themes)
- labelled : relevant and status == ok (label distributions; evidence-failed posts had their labels downgraded)
- event    : labelled and kpi_branch != not_applicable (the post states a SIP change or failure)
Reddit was down-sampled in P6, so every headline share is shown raw and "source-balanced"
(the mean of per-source shares, over sources with at least 30 posts in the base).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from core.budget import BudgetError, BudgetTracker, Estimate, Usage, compute_cost_inr, estimate_tokens, format_estimate
from core.config import Settings
from core.llm_client import LLMClient, LLMProvider, LLMUnavailable
from core.logger import get_logger

log = get_logger("analyse")

STAGE = "analyse"
THEME_PROMPT_FILE = "themes_v1_20261028.txt"

SCENARIOS: Dict[int, str] = {
    1: "Money pressure or a life event", 2: "Returns below expectation", 3: "Not understanding how a SIP works",
    4: "Lost the reason to invest", 5: "Lured into other investments", 6: "Someone told them to stop or switch",
    7: "Stopped to fix the portfolio, never finished", 8: "Lost trust in the platform",
    9: "Low balance on debit day", 10: "Bank changed, mandate on old account", 11: "Mandate or autopay problems",
    12: "KYC issues", 13: "Fund stopped taking SIPs", 14: "Temporary pause became permanent",
    15: "Restarting is a hassle", 16: "Avoidance or guilt", 17: "Moved to another platform",
}
BRANCHES: Dict[str, str] = {
    "voluntary_stop": "A. Chose to stop", "involuntary_payment_failure": "B. Money didn't move",
    "lapse_and_return": "C. Lapse and return", "platform_switch": "Moved to another platform",
    "continuing": "Continuing", "not_applicable": "No SIP change stated",
}
STOP_BRANCHES = ["voluntary_stop", "involuntary_payment_failure", "lapse_and_return", "platform_switch"]
DROP_OFF = {"paused", "reduced", "stopped"}
SEGMENT_KEYS = ["age_band", "employment", "years_of_work", "city_tier", "income_stability", "source_of_money",
                "influencer"]
THEME_FIELDS: Dict[str, str] = {
    "unmet_need": "what the investor wished the app, process or information did for them (Q9 unmet needs)",
    "in_app_confusion": "what confused or worried the investor inside the app (Q5)",
    "start_reason": "why the investor started a SIP (Q1)",
    "expectation": "what the investor expected to happen after starting (Q1)",
}
EMPTY = {None, "", "none", "unknown", "null", "n/a", "na"}
MIN_SOURCE_N = 30
LOW_SAMPLE_N = 30
CONTINUER_FLOOR_PCT, CONTINUER_FLOOR_N = 15.0, 300


# --------------------------------------------------------------------------- loading


def _paths(settings: Settings) -> Dict[str, Path]:
    out = settings.paths.resolve("outputs")
    return {
        "enriched": settings.paths.resolve("enriched_dir") / "enriched_posts.jsonl",
        "themes": out / "themes.json",
        "analysis": out / "analysis.json",
        "eval": out / "eval_report.json",
        "prompt": settings.paths.resolve("prompts") / THEME_PROMPT_FILE,
    }


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in EMPTY)


def post_row(p: dict) -> dict:
    e = p.get("enrichment") or {}
    seg = e.get("segment_signals") or {}
    ids = [int(x) for x in (e.get("stop_scenario_ids") or [])]
    rel = bool(e.get("is_relevant")) and p.get("status") != "enrichment_failed"
    row = {
        "id": p["id"], "source": p["source"], "url": p.get("url") or "", "date": p.get("date") or "",
        "status": p.get("status") or "", "text": p.get("clean_text") or p.get("text") or "",
        "relevant": rel,
        "outcome": e.get("outcome") or "unknown",
        "kpi_branch": e.get("kpi_branch") or "not_applicable",
        "stop_ids": ids, "primary_scenario": ids[0] if ids else 0,
        "trigger_type": e.get("trigger_type") or "unknown",
        "seeded": e.get("seeded_category_primary") or "none",
        "discovered": e.get("discovered_category") or "",
        "journey_stage": e.get("journey_stage") or "unknown",
        "checking_what": e.get("checking_what") or "",
        "checking_frequency": e.get("checking_frequency") or "unknown",
        "outside_app_behaviour": e.get("outside_app_behaviour") or "unknown",
        "platform_tag": e.get("platform_tag") or "unspecified",
        "no_fund_stock_flag": bool(e.get("no_fund_stock_flag")),
        "segment_confidence": e.get("segment_confidence") or "low",
        "is_first_time_investor": e.get("is_first_time_investor") or "unknown",
        "evidence_span": e.get("evidence_span") or "",
        "evidence_verified": bool(p.get("evidence_verified")),
        "segment_fit": p.get("segment_fit"),
    }
    for f in THEME_FIELDS:
        v = e.get(f)
        row[f] = "" if _blank(v) else str(v).strip()
    for k in SEGMENT_KEYS:
        row[k] = seg.get(k) or "unknown"
    return row


def load_frame(path: Path) -> pd.DataFrame:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(post_row(json.loads(line)))
    df = pd.DataFrame(rows)
    df["labelled"] = df["relevant"] & (df["status"] == "ok")
    df["event"] = df["labelled"] & (df["kpi_branch"] != "not_applicable")
    df["group"] = df["outcome"].map(lambda o: "continuer" if o == "continued" else "drop_off" if o in DROP_OFF
                                    else "considering" if o == "considering_stop" else "restarted"
                                    if o == "restarted" else "unknown")
    return df


# --------------------------------------------------------------------------- helpers


def pct(a: float, n: float) -> Optional[float]:
    return round(100.0 * a / n, 1) if n else None


def dist(s: pd.Series, top: Optional[int] = None, drop_unknown: bool = False) -> dict:
    n = int(len(s))
    vc = s.value_counts()
    known = int(sum(int(c) for v, c in vc.items() if not _blank(v)))
    vals = [{"value": str(v), "n": int(c), "pct": pct(int(c), n)} for v, c in vc.items()
            if not (drop_unknown and _blank(v))]
    return {"n": n, "known": known, "coverage_pct": pct(known, n), "values": vals[:top] if top else vals}


def balanced_shares(base: pd.DataFrame, col: str, explode: bool = False, labels: Optional[dict] = None) -> dict:
    """Raw share of each value in `base`, per-source shares, and the source-balanced share."""
    n = len(base)
    sources = [s for s, c in base["source"].value_counts().items() if c >= MIN_SOURCE_N]
    work = base[["source", col]].explode(col) if explode else base[["source", col]]
    work = work[work[col].map(lambda v: not _blank(v) and v != 0)]
    out = []
    for v, c in work[col].value_counts().items():
        by_src = {}
        for s in sources:
            sn = int((base["source"] == s).sum())
            by_src[s] = pct(int(((work["source"] == s) & (work[col] == v)).sum()), sn)
        bal = round(sum(by_src.values()) / len(by_src), 1) if by_src else None
        key = v.item() if hasattr(v, "item") else v
        out.append({"value": key, "label": (labels or {}).get(v, str(v)), "n": int(c), "pct": pct(int(c), n),
                    "balanced_pct": bal, "by_source_pct": by_src})
    return {"base_n": n, "sources_balanced": sources, "values": out}


def crosstab(df: pd.DataFrame, row: str, col: str, rows_keep: Optional[List[str]] = None) -> dict:
    t = pd.crosstab(df[row], df[col])
    if rows_keep:
        t = t.reindex([r for r in rows_keep if r in t.index])
    return {"rows": [str(r) for r in t.index], "cols": [str(c) for c in t.columns],
            "counts": [[int(x) for x in r] for r in t.values.tolist()]}


# --------------------------------------------------------------------------- themes (LLM)


class ThemeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    item_ids: List[int] = Field(default_factory=list)


class ThemesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    themes: List[ThemeOut]


def theme_items(df: pd.DataFrame, field: str) -> List[dict]:
    sub = df[df["relevant"] & (df[field] != "")].sort_values("id")
    return [{"i": i + 1, "post_id": r.id, "text": str(r[field])[:200]} for i, (_, r) in enumerate(sub.iterrows())]


def items_hash(items: List[dict], prompt_text: str) -> str:
    h = hashlib.sha256(prompt_text.encode())
    for it in items:
        h.update(f"{it['post_id']}|{it['text']}\n".encode())
    return h.hexdigest()[:16]


def theme_prompt(field: str, items: List[dict]) -> str:
    lines = "\n".join(f"{it['i']} | {it['text']}" for it in items)
    return f"Field: {field}\nMeaning: {THEME_FIELDS[field]}\nItems ({len(items)}):\n{lines}"


def theme_max_output(n_items: int) -> int:
    return min(16000, 800 + 5 * n_items)


def theme_expected_output(n_items: int) -> int:
    return 400 + 3 * n_items


def apply_theme_response(resp: ThemesResponse, items: List[dict]) -> dict:
    """Map numbered ids back to post ids. Each id is kept in its first theme; unknown ids are dropped;
    unassigned items go to 'Unclustered'."""
    by_i = {it["i"]: it["post_id"] for it in items}
    seen: set = set()
    themes = []
    for t in resp.themes:
        pids = []
        for i in t.item_ids:
            if i in by_i and i not in seen:
                seen.add(i)
                pids.append(by_i[i])
        if pids:
            themes.append({"name": t.name.strip()[:60], "description": t.description.strip()[:200], "post_ids": pids})
    unassigned = [by_i[i] for i in by_i if i not in seen]
    themes.sort(key=lambda t: -len(t["post_ids"]))
    return {"themes": themes, "unclustered": unassigned, "n_items": len(items)}


def run_themes(settings: Settings, df: pd.DataFrame, approve: Callable[[], bool],
               provider: Optional[LLMProvider] = None, api_key: Optional[str] = None) -> Optional[dict]:
    """Returns the themes dict (cached or fresh), or None if not approved / no key."""
    p = _paths(settings)
    system = p["prompt"].read_text(encoding="utf-8")
    cached = json.loads(p["themes"].read_text(encoding="utf-8")) if p["themes"].exists() else {}
    todo, result = [], {}
    for field in THEME_FIELDS:
        items = theme_items(df, field)
        hsh = items_hash(items, system)
        if cached.get(field, {}).get("hash") == hsh:
            result[field] = cached[field]
        elif items:
            todo.append((field, items, hsh))
        else:
            result[field] = {"hash": hsh, "themes": [], "unclustered": [], "n_items": 0}
    if not todo:
        print("Themes: cached in outputs/themes.json (0 Gemini calls).")
        return result

    tracker = BudgetTracker(settings.budget, settings.paths.resolve("spend_log"), stage=STAGE,
                            model=settings.llm.model, prompt_version=THEME_PROMPT_FILE.rsplit(".", 1)[0])
    tracker.check_can_start()
    in_tok = sum(estimate_tokens(system) + estimate_tokens(theme_prompt(f, it)) for f, it, _ in todo)
    out_tok = sum(theme_expected_output(len(it)) for _, it, _ in todo)
    worst_out = sum(theme_max_output(len(it)) for _, it, _ in todo)
    est = Estimate(STAGE + " (theme clustering)", len(todo), in_tok, out_tok,
                   compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=out_tok), settings.budget),
                   compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=worst_out), settings.budget),
                   note=", ".join(f"{f}: {len(it)} items" for f, it, _ in todo))
    print(format_estimate(est, tracker))
    if not approve():
        return None
    client = LLMClient(settings, tracker, provider=provider, api_key=api_key)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    for field, items, hsh in todo:
        prompt = theme_prompt(field, items)
        exp = compute_cost_inr(Usage(input_tokens=estimate_tokens(system) + estimate_tokens(prompt),
                                     output_tokens=theme_expected_output(len(items))), settings.budget)
        res = client.generate_json(prompt=prompt, schema=ThemesResponse, batch_id=f"themes-{stamp}-{field}",
                                   max_output_tokens=theme_max_output(len(items)), system=system,
                                   call_type="clustering", expected_cost_inr=exp)
        if res.status != "ok":
            print(f"Theme clustering for {field} failed ({res.status}); it will be retried on the next run.")
            continue
        out = apply_theme_response(res.data, items)
        out.update({"hash": hsh, "prompt": THEME_PROMPT_FILE,
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        result[field] = out
        cached[field] = out
        p["themes"].write_text(json.dumps(cached, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {field}: {len(out['themes'])} themes, {len(out['unclustered'])} unclustered of {len(items)}")
    print(f"Theme spend ₹{tracker.run_spend:.2f}   project ₹{tracker.project_spend:.2f}")
    return result


def theme_column(df: pd.DataFrame, themes: Optional[dict], field: str) -> pd.Series:
    m: Dict[str, str] = {}
    for t in (themes or {}).get(field, {}).get("themes", []):
        for pid in t["post_ids"]:
            m[pid] = t["name"]
    return df["id"].map(lambda i: m.get(i, ""))


# --------------------------------------------------------------------------- statistics


def theme_summary(df: pd.DataFrame, themes: Optional[dict], field: str, base: pd.Series) -> dict:
    col = theme_column(df, themes, field)
    t = (themes or {}).get(field)
    if not t:
        return {"clustered": False, "n_items": int((df[base][field] != "").sum()), "themes": []}
    n = t["n_items"]
    desc = {x["name"]: x["description"] for x in t["themes"]}
    sub = df[base & (col != "")]
    out = []
    for name, c in col[base & (col != "")].value_counts().items():
        rows = sub[col[sub.index] == name]
        out.append({"theme": name, "description": desc.get(name, ""), "n": int(c), "pct_of_items": pct(int(c), n),
                    "groww_n": int((rows["platform_tag"] == "groww").sum()),
                    "by_source": {k: int(v) for k, v in rows["source"].value_counts().items()},
                    "examples": [str(x) for x in rows[field].head(3)]})
    return {"clustered": True, "n_items": n, "unclustered": len(t.get("unclustered", [])), "themes": out}


def continuers_vs_dropoffs(df: pd.DataFrame, unmet: pd.Series) -> dict:
    lab = df[df["labelled"]]
    groups = {"continuer": lab[lab["group"] == "continuer"], "drop_off": lab[lab["group"] == "drop_off"],
              "considering": lab[lab["group"] == "considering"]}
    fields = ["journey_stage", "trigger_type", "is_first_time_investor", "platform_tag", "segment_confidence",
              "employment", "income_stability", "influencer", "source_of_money", "outside_app_behaviour",
              "checking_frequency"]
    table = {}
    for f in fields:
        vals = sorted(set().union(*[set(g[f]) for g in groups.values()]))
        rows = []
        for v in vals:
            rows.append({"value": str(v), **{k: {"n": int((g[f] == v).sum()), "pct": pct(int((g[f] == v).sum()), len(g))}
                                             for k, g in groups.items()}})
        rows.sort(key=lambda r: -(r["continuer"]["n"] + r["drop_off"]["n"]))
        table[f] = rows
    um = {}
    for k, g in groups.items():
        s = unmet[g.index]
        s = s[s != ""]
        um[k] = [{"theme": str(v), "n": int(c), "pct": pct(int(c), len(s))} for v, c in s.value_counts().head(6).items()]
    return {"n": {k: int(len(g)) for k, g in groups.items()}, "fields": table, "unmet_need_themes": um,
            "note": "drop_off = paused, reduced or stopped (FR4.1); considering = considering_stop, shown separately."}


def segment_cuts(df: pd.DataFrame) -> dict:
    lab = df[df["labelled"]]
    out = {}
    for k in SEGMENT_KEYS + ["is_first_time_investor"]:
        rows = []
        for v, g in lab.groupby(k):
            if _blank(v):
                continue
            n = len(g)
            ev = g[g["event"]]
            rows.append({"value": str(v), "n": int(n), "low_sample": n < LOW_SAMPLE_N,
                         "continuer_pct": pct(int((g["group"] == "continuer").sum()), n),
                         "drop_off_pct": pct(int((g["group"] == "drop_off").sum()), n),
                         "considering_pct": pct(int((g["group"] == "considering").sum()), n),
                         "event_n": int(len(ev)),
                         "top_scenarios": [{"id": int(s), "label": SCENARIOS[int(s)], "n": int(c)}
                                           for s, c in ev[ev["primary_scenario"] > 0]["primary_scenario"]
                                           .value_counts().head(3).items()]})
        rows.sort(key=lambda r: -r["n"])
        out[k] = {"known_n": int(sum(r["n"] for r in rows)), "coverage_pct": pct(sum(r["n"] for r in rows), len(lab)),
                  "values": rows}
    return out


def eval_rates(path: Path) -> dict:
    if not path.exists():
        return {}
    r = json.loads(path.read_text(encoding="utf-8"))
    return {f: v.get("pct") for f, v in r.get("per_field", {}).items()} | {
        "event_relevance": r.get("event_relevance_vs_human_relevance", {}).get("pct")}


def build_analysis(df: pd.DataFrame, themes: Optional[dict], eval_path: Optional[Path] = None) -> dict:
    rel = df["relevant"]
    lab = df["labelled"]
    ev = df["event"]
    stop_ev = ev & df["kpi_branch"].isin(STOP_BRANCHES)
    unmet = theme_column(df, themes, "unmet_need")
    rates = eval_rates(eval_path) if eval_path else {}
    low_conf = sorted(f for f, v in rates.items() if v is not None and v < 60)

    n_rel, n_lab = int(rel.sum()), int(lab.sum())
    continued = int((df[lab]["outcome"] == "continued").sum())
    floor_ok = pct(continued, n_lab) >= CONTINUER_FLOOR_PCT or continued >= CONTINUER_FLOOR_N

    stages_known = df[ev & (df["journey_stage"] != "unknown")]
    stage_rows = []
    for stg, g in stages_known.groupby("journey_stage"):
        u = unmet[g.index]
        stage_rows.append({
            "stage": str(stg), "n": int(len(g)), "low_sample": len(g) < LOW_SAMPLE_N,
            "outcomes": {str(k): int(v) for k, v in g["outcome"].value_counts().items()},
            "top_triggers": [{"value": str(k), "n": int(v)} for k, v in g["trigger_type"].value_counts().head(3).items()],
            "top_scenarios": [{"id": int(k), "label": SCENARIOS[int(k)], "n": int(v)}
                              for k, v in g[g["primary_scenario"] > 0]["primary_scenario"].value_counts().head(3).items()],
            "top_unmet_need_themes": [{"theme": str(k), "n": int(v)} for k, v in u[u != ""].value_counts().head(3).items()],
        })
    stage_order = ["pre_start", "month_0_1", "month_2_3", "month_4_6", "month_7_12", "month_12_plus", "post_stop"]
    stage_rows.sort(key=lambda r: stage_order.index(r["stage"]) if r["stage"] in stage_order else 99)

    combos = Counter()
    for _, r in df[stop_ev & (df["journey_stage"] != "unknown") & (df["primary_scenario"] > 0)].iterrows():
        combos[(int(r.primary_scenario), r.journey_stage, r.outcome)] += 1
    trig = Counter()
    for _, r in df[ev & ~df["trigger_type"].isin(["unknown", "none"])].iterrows():
        trig[(r.trigger_type, r.journey_stage, r.outcome)] += 1

    dropish = lab & df["group"].isin(["drop_off", "considering"])
    disc = df[rel & (df["discovered"] != "")]["discovered"].value_counts()

    analysis = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "populations": {
            "total_posts": int(len(df)), "relevant": n_rel, "labelled": n_lab,
            "evidence_failed_relevant": int((rel & (df["status"] == "evidence_span_failed")).sum()),
            "event": int(ev.sum()), "stop_event": int(stop_ev.sum()),
            "relevant_by_source": {k: int(v) for k, v in df[rel]["source"].value_counts().items()},
            "event_by_source": {k: int(v) for k, v in df[ev]["source"].value_counts().items()},
            "groww_tagged_relevant": int((rel & (df["platform_tag"] == "groww")).sum()),
            "definitions": {
                "relevant": "is_relevant and not enrichment_failed",
                "labelled": "relevant and evidence verified (status ok)",
                "event": "labelled and the post states a SIP change or failure (kpi_branch set)",
            },
        },
        "label_confidence": {
            "golden_agreement_pct": rates,
            "low_confidence_fields": low_conf,
            "note": "Fields under 60% golden agreement are shown but not used for headline claims.",
        },
        "kpi_sizing": {
            "branches_among_labelled": balanced_shares(df[lab], "kpi_branch", labels=BRANCHES),
            "branches_among_events": balanced_shares(df[ev], "kpi_branch", labels=BRANCHES),
            "scenarios_any_mention": balanced_shares(df[stop_ev], "stop_ids", explode=True, labels=SCENARIOS),
            "scenarios_primary": balanced_shares(df[stop_ev], "primary_scenario", labels=SCENARIOS),
            "note": "Shares of stop-event posts (voluntary, involuntary, lapse, platform switch). "
                    "balanced_pct averages per-source shares to undo Reddit down-sampling.",
        },
        "q1_start": {
            "start_reason_themes": theme_summary(df, themes, "start_reason", rel),
            "expectation_themes": theme_summary(df, themes, "expectation", rel),
            "pre_start_posts": int((lab & (df["journey_stage"] == "pre_start")).sum()),
        },
        "q2_behaviour_change": {"journey_stage_among_events": dist(df[ev]["journey_stage"]),
                                "stages": stage_rows,
                                "outcome_by_stage": crosstab(df[ev], "journey_stage", "outcome", stage_order)},
        "q3_triggers": {
            "base": "drop-offs (paused/reduced/stopped) + considering_stop",
            "n": int(dropish.sum()),
            "stop_scenarios": balanced_shares(df[dropish & ev], "primary_scenario", labels=SCENARIOS),
            "trigger_type": dist(df[dropish]["trigger_type"], drop_unknown=False),
            "trigger_type_low_confidence": "trigger_type" in low_conf,
        },
        "q4_checking": {"checking_frequency": dist(df[lab]["checking_frequency"]),
                        "checking_what_top": [{"value": k, "n": int(v)} for k, v in
                                              df[lab & (df["checking_what"] != "")]["checking_what"]
                                              .str.lower().str.strip().value_counts().head(12).items()],
                        "checking_what_coverage_pct": pct(int((df[lab]["checking_what"] != "").sum()), n_lab)},
        "q5_in_app_confusion": {
            "all": theme_summary(df, themes, "in_app_confusion", rel),
            "groww_only_n": int((rel & (df["platform_tag"] == "groww") & (df["in_app_confusion"] != "")).sum()),
        },
        "q6_outside_app": dist(df[lab]["outside_app_behaviour"]),
        "q7_continuers_vs_dropoffs": continuers_vs_dropoffs(df, unmet),
        "q8_segments": {"cuts": segment_cuts(df),
                        "segment_confidence": dist(df[lab]["segment_confidence"]),
                        "segment_fit_mean": round(float(df[lab]["segment_fit"].dropna().mean()), 3)
                        if df[lab]["segment_fit"].notna().any() else None,
                        "note": f"Cuts with n < {LOW_SAMPLE_N} are flagged low_sample (FR4.3)."},
        "q9_unmet_needs": {"themes": theme_summary(df, themes, "unmet_need", rel)},
        "co_occurrence": {
            "scenario_stage_outcome": [{"scenario": s, "label": SCENARIOS[s], "journey_stage": j, "outcome": o, "n": c}
                                       for (s, j, o), c in combos.most_common(10)],
            "trigger_stage_outcome": [{"trigger_type": t, "journey_stage": j, "outcome": o, "n": c}
                                      for (t, j, o), c in trig.most_common(10)],
        },
        "categories": {"seeded_primary": dist(df[lab]["seeded"]),
                       "discovered": [{"value": k, "n": int(v), "status": "emerging" if v < 15 else "promoted"}
                                      for k, v in disc.items()]},
        "continuer_floor": {"continued": continued, "labelled": n_lab, "pct": pct(continued, n_lab),
                            "target": f">= {CONTINUER_FLOOR_PCT}% or >= {CONTINUER_FLOOR_N} posts", "met": bool(floor_ok)},
        "outcomes": dist(df[lab]["outcome"]),
        "platform_tag": dist(df[rel]["platform_tag"]),
    }
    return analysis


PUBLIC_COLUMNS = ["id", "source", "url", "date", "platform_tag", "outcome", "kpi_branch", "primary_scenario",
                  "stop_ids", "journey_stage", "trigger_type", "seeded", "segment_confidence", "segment_fit",
                  "is_first_time_investor", "evidence_span", "unmet_need_theme", "confusion_theme", "event"]


def export_public(df: pd.DataFrame, themes: Optional[dict], path: Path) -> int:
    """Relevant, evidence-verified posts with labels and the short verbatim evidence span only — no post text,
    no author hash. This is the only post-level file the Streamlit app reads (raw/clean data is never deployed)."""
    sub = df[df["labelled"]].copy()
    sub["unmet_need_theme"] = theme_column(df, themes, "unmet_need")[sub.index]
    sub["confusion_theme"] = theme_column(df, themes, "in_app_confusion")[sub.index]
    sub["evidence_span"] = sub.apply(lambda r: r.evidence_span if r.evidence_verified else "", axis=1)
    sub["stop_ids"] = sub["stop_ids"].map(lambda ids: ";".join(str(i) for i in ids))
    sub["date"] = sub["date"].str[:10]
    sub[PUBLIC_COLUMNS].to_csv(path, index=False)
    return len(sub)


# --------------------------------------------------------------------------- entry point


def run_analyse(settings: Settings, approve: Callable[[], bool] = lambda: False,
                provider: Optional[LLMProvider] = None, api_key: Optional[str] = None) -> int:
    p = _paths(settings)
    if not p["enriched"].exists():
        print("No enriched posts yet. Run --stage enrich first.")
        return 2
    df = load_frame(p["enriched"])
    print(f"\n== Stage 4: Analyse ==\nPosts: {len(df):,}   relevant: {int(df['relevant'].sum()):,}   "
          f"labelled: {int(df['labelled'].sum()):,}   event: {int(df['event'].sum()):,}")
    try:
        themes = run_themes(settings, df, approve, provider=provider, api_key=api_key)
    except BudgetError as e:
        print(f"Budget stop: {e}")
        return 1
    except LLMUnavailable as e:
        print(f"Gemini unavailable (HTTP {e.code or 'unknown'}): {e.error_message}")
        return 1
    if themes is None:
        return 0
    analysis = build_analysis(df, themes, p["eval"])
    p["analysis"].write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    n_pub = export_public(df, themes, p["analysis"].parent / "posts_public.csv")

    ks = analysis["kpi_sizing"]
    print("\nKPI branches among event posts (raw % / source-balanced %):")
    for v in ks["branches_among_events"]["values"]:
        print(f"  {v['label']:<28} n={v['n']:<4} {v['pct']}% / {v['balanced_pct']}%")
    print("Top stop scenarios (primary, among stop events):")
    for v in ks["scenarios_primary"]["values"][:8]:
        print(f"  {v['value']:>2} {v['label']:<44} n={v['n']:<4} {v['pct']}% / {v['balanced_pct']}%")
    cf = analysis["continuer_floor"]
    print(f"Continuer floor: {cf['continued']} of {cf['labelled']} ({cf['pct']}%) → {'met' if cf['met'] else 'NOT met'}")
    if analysis["label_confidence"]["low_confidence_fields"]:
        print(f"Low-confidence fields (golden <60%): {', '.join(analysis['label_confidence']['low_confidence_fields'])}")
    print(f"Wrote outputs/{p['analysis'].name} and outputs/posts_public.csv ({n_pub} labelled posts, no post text)")
    return 0
