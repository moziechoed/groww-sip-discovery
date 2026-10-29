"""P8 Analyse + Surface: all mocked. The real Gemini API is never called."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from core.budget import Usage
from pipeline import analyse as A
from pipeline import surface as S
from tests.conftest import FakeProvider


def enriched(i, source="reddit", rel=True, status="ok", outcome="stopped", branch="voluntary_stop", ids=(1,),
             unmet="wants a pause option", span="I stopped my SIP when rent went up", seg_fit=0.6, nfs=False,
             stage="month_2_3", checking="", platform="groww"):
    text = f"Post {i}. I stopped my SIP when rent went up. More words here."
    e = {"post_id": f"{source}:{i}", "is_relevant": rel, "outcome": outcome, "kpi_branch": branch,
         "stop_scenario_ids": list(ids), "trigger_type": "life_event_financial_pressure",
         "seeded_category_primary": "life_event_financial_pressure", "journey_stage": stage,
         "unmet_need": unmet, "in_app_confusion": None, "start_reason": None, "expectation": None,
         "checking_what": checking, "platform_tag": platform, "no_fund_stock_flag": nfs,
         "segment_confidence": "medium", "evidence_span": span,
         "segment_signals": {"employment": "salaried"}}
    return {"id": f"{source}:{i}", "source": source, "url": f"https://x/{i}", "date": f"2026-0{1 + i % 9}-01T00:00:00Z",
            "text": text, "clean_text": text, "status": status, "enrichment": e,
            "evidence_verified": status == "ok" and bool(span), "segment_fit": seg_fit if rel else None}


def corpus():
    rows = [enriched(i) for i in range(20)]                                      # scenario 1, n=20
    rows += [enriched(100 + i, source="play_store", ids=(11,), branch="involuntary_payment_failure",
                      outcome="unknown", unmet="wants autopay to work") for i in range(16)]  # scenario 11
    rows += [enriched(200 + i, ids=(9,)) for i in range(3)]                      # emerging
    rows += [enriched(300 + i, outcome="continued", branch="continuing", ids=()) for i in range(10)]
    rows += [enriched(400 + i, rel=False) for i in range(5)]
    rows += [enriched(500, status="evidence_span_failed")]
    rows += [enriched(600 + i, ids=(1,), nfs=True) for i in range(4)]
    return rows


@pytest.fixture
def ws(make_settings, tmp_path):
    s = make_settings()
    s.paths.enriched_dir = str(tmp_path / "enriched")
    d = Path(s.paths.enriched_dir)
    d.mkdir(parents=True)
    with open(d / "enriched_posts.jsonl", "w", encoding="utf-8") as f:
        for r in corpus():
            f.write(json.dumps(r) + "\n")
    Path(s.paths.outputs).mkdir(parents=True, exist_ok=True)
    return s


def themes_reply(n_items):
    half = n_items // 2
    body = {"themes": [{"name": "Wants a pause option", "description": "d", "item_ids": list(range(1, half + 1))},
                       {"name": "Wants autopay to work", "description": "d",
                        "item_ids": list(range(half + 1, n_items + 1)) + [999, 1]}]}
    return json.dumps(body), Usage(input_tokens=1000, output_tokens=200)


def test_populations(ws):
    df = A.load_frame(Path(ws.paths.enriched_dir) / "enriched_posts.jsonl")
    assert int(df["relevant"].sum()) == 20 + 16 + 3 + 10 + 1 + 4
    assert int(df["labelled"].sum()) == 20 + 16 + 3 + 10 + 4
    assert int(df["event"].sum()) == 20 + 16 + 3 + 10 + 4


def test_balanced_shares_average_per_source():
    base = pd.DataFrame({"source": ["a"] * 40 + ["b"] * 40, "x": ["p"] * 40 + ["p"] * 10 + ["q"] * 30})
    out = A.balanced_shares(base, "x")
    p = next(v for v in out["values"] if v["value"] == "p")
    assert p["pct"] == 62.5 and p["balanced_pct"] == 62.5
    base2 = pd.DataFrame({"source": ["a"] * 90 + ["b"] * 30, "x": ["p"] * 90 + ["q"] * 30})
    p2 = next(v for v in A.balanced_shares(base2, "x")["values"] if v["value"] == "p")
    assert p2["pct"] == 75.0 and p2["balanced_pct"] == 50.0


def test_apply_theme_response_dedupes_and_tracks_unassigned():
    items = [{"i": i, "post_id": f"p{i}", "text": "t"} for i in range(1, 6)]
    resp = A.ThemesResponse.model_validate({"themes": [
        {"name": "A", "description": "", "item_ids": [1, 2, 2, 99]},
        {"name": "B", "description": "", "item_ids": [2, 3]}]})
    out = A.apply_theme_response(resp, items)
    assert out["themes"][0]["post_ids"] == ["p1", "p2"]
    assert out["themes"][1]["post_ids"] == ["p3"]
    assert sorted(out["unclustered"]) == ["p4", "p5"]


def test_run_analyse_not_approved_makes_no_call(ws):
    fp = FakeProvider([])
    assert A.run_analyse(ws, approve=lambda: False, provider=fp) == 0
    assert fp.calls == [] and not (Path(ws.paths.outputs) / "analysis.json").exists()


def test_run_analyse_then_cache(ws):
    df = A.load_frame(Path(ws.paths.enriched_dir) / "enriched_posts.jsonl")
    n = len(A.theme_items(df, "unmet_need"))
    fp = FakeProvider([themes_reply(n)])  # only unmet_need has items in this corpus
    assert A.run_analyse(ws, approve=lambda: True, provider=fp) == 0
    assert len(fp.calls) == 1
    a = json.loads((Path(ws.paths.outputs) / "analysis.json").read_text())
    assert a["q9_unmet_needs"]["themes"]["clustered"] is True
    assert a["continuer_floor"]["continued"] == 10
    assert a["kpi_sizing"]["scenarios_primary"]["values"][0]["value"] == 1
    fp2 = FakeProvider([])
    assert A.run_analyse(ws, approve=lambda: False, provider=fp2) == 0  # cached: no approval needed
    assert fp2.calls == []


def cards_reply(ids, title="Rent goes up, SIP goes", words=""):
    cards = [{"cluster_id": i, "title": title, "description": "Investors stop when costs rise." + words,
              "actionability": 2 if i == 1 else 3, "rationale": "Groww can add a pause."} for i in ids]
    return json.dumps({"cards": cards}), Usage(input_tokens=800, output_tokens=300)


def test_surface_clusters_scores_and_quotes(ws):
    fp = FakeProvider([cards_reply([1, 11, 9], words=" Don't miss out, urgent!")])
    assert S.run_surface(ws, approve=lambda: True, provider=fp) == 0
    out = json.loads((Path(ws.paths.outputs) / "opportunities.json").read_text())
    cards = out["cards"]
    assert [c["scenario_id"] for c in cards] == [11, 1] or [c["scenario_id"] for c in cards] == [1, 11]
    assert cards[0]["score_display"] == 100.0 and cards[0]["rank"] == 1
    assert all(c["n"] >= S.MIN_CARD_N for c in cards)
    assert next(c for c in cards if c["scenario_id"] == 1)["n"] == 20      # no_fund_stock posts excluded
    assert [e["scenario_id"] for e in out["emerging"]] == [9]
    q = cards[0]["quotes"]
    assert 1 <= len(q) <= 5 and all(len(x["quote"].split()) <= 30 for x in q)
    assert "urgent" in cards[0]["guardrail_flags"] and "don't miss" in cards[0]["guardrail_flags"]
    assert (Path(ws.paths.outputs) / "opportunities.csv").exists()
    fp2 = FakeProvider([])
    assert S.run_surface(ws, approve=lambda: False, provider=fp2) == 0     # cached
    assert fp2.calls == []


def test_specificity_levels():
    th = {"high": 0.6, "medium": 0.3}
    assert S.specificity_level(0.7, th) == 3
    assert S.specificity_level(0.3, th) == 2
    assert S.specificity_level(0.1, th) == 1
