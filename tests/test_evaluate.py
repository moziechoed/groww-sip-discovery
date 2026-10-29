"""P9 Evaluate: agreement rules (no LLM)."""
from pipeline.evaluate import compare, engine_labels, field_agrees


def _post(rel=True, status="ok", **fields):
    e = {"is_relevant": rel, "outcome": "stopped", "kpi_branch": "voluntary_stop", "stop_scenario_ids": [1, 2],
         "trigger_type": "life_event_financial_pressure", "seeded_category_primary": None,
         "journey_stage": "month_2_3", "outside_app_behaviour": "unknown", "segment_confidence": "low"}
    e.update(fields)
    return {"id": "p1", "status": status, "enrichment": e}


def _human(pid="p1", rel="true", bucket="stratified", **kw):
    row = {"post_id": pid, "bucket": bucket, "source": "reddit", "is_relevant": rel, "outcome": "stopped",
           "kpi_branch": "voluntary_stop", "stop_scenario_ids": "2", "trigger_type": "life_event_financial_pressure",
           "seeded_category_primary": "none", "journey_stage": "month_2_3", "outside_app_behaviour": "",
           "segment_confidence": ""}
    row.update(kw)
    return row


def test_engine_labels_format():
    lab = engine_labels(_post())
    assert lab["is_relevant"] == "true"
    assert lab["stop_scenario_ids"] == "1;2"
    assert lab["seeded_category_primary"] == "none"


def test_engine_labels_irrelevant_and_failed():
    assert engine_labels(_post(rel=False))["outcome"] == ""
    assert engine_labels(_post(status="enrichment_failed"))["is_relevant"] == "false"


def test_stop_scenario_any_overlap_and_none():
    assert field_agrees("stop_scenario_ids", "1;2", "2")[0] is True
    assert field_agrees("stop_scenario_ids", "1;2", "2") == (True, False)
    assert field_agrees("stop_scenario_ids", "none", "none") == (True, True)
    assert field_agrees("stop_scenario_ids", "3", "none")[0] is False


def test_seeded_none_equivalence():
    assert field_agrees("seeded_category_primary", "", "none")[0]
    assert field_agrees("seeded_category_primary", "none", "")[0]


def test_compare_counts_downstream_only_on_human_relevant():
    human = [_human("p1"), _human("p2", rel="false")]
    eng = {"p1": engine_labels(_post()), "p2": engine_labels(_post())}
    report, failures = compare(human, eng)
    assert report["per_field"]["is_relevant"] == {"agree": 1, "n": 2, "pct": 50.0}
    assert report["per_field"]["outcome"]["n"] == 1
    assert report["overall"]["n"] == 2 + 6
    assert [f["field"] for f in failures] == ["is_relevant"]
    assert report["relevance_confusion"]["engine_true_human_false"] == 1


def test_engine_says_irrelevant_counts_as_downstream_disagreement():
    report, failures = compare([_human("p1")], {"p1": engine_labels(_post(rel=False))})
    assert report["per_field"]["outcome"]["agree"] == 0
    # seeded_category "none" matches an empty engine value, so 6 of the 7 fields disagree
    assert len(failures) == 6


def test_event_relevance_and_reasons():
    human = [_human("p1"), _human("p2", rel="false")]
    eng = {"p1": engine_labels(_post()), "p2": engine_labels(_post(kpi_branch="not_applicable"))}
    report, failures = compare(human, eng, {("p2", "is_relevant"): ("definition gap", "drafted")})
    assert report["event_relevance_vs_human_relevance"]["agree"] == 2
    assert failures[0]["likely_reason"] == "definition gap"


def test_ac9_flags_and_buckets():
    human = [_human("p1", bucket="ambiguous", outcome="paused")]
    report, _ = compare(human, {"p1": engine_labels(_post())})
    assert report["ac9"]["outcome_pass"] is False
    assert report["by_bucket"]["ambiguous"]["n"] == 7
    assert report["weakest_field"] == "outcome"
