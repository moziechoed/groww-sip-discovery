"""P7 Enrich: all mocked. The real Gemini API is never called (Guardrails §9)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from core.budget import Usage
from core.checkpoint import Checkpoint
from core.llm_client import ProviderError, clean_schema_for_gemini
from core.schemas import ENRICHMENT_PROPERTY_ORDER, EnrichmentBatchResponse, EnrichmentFields
from pipeline import enrich as E
from pipeline.golden import CSV_COLUMNS
from tests.conftest import FakeProvider


# ----------------------------------------------------------------------------- fixtures / helpers


def mk(i, source="reddit", text=None, borderline=False, platform="groww"):
    text = text or f"I started my first SIP in January number {i}. In May I stopped it because rent went up."
    return {"id": f"{source}:{i}", "source": source, "url": "https://x", "date": "2026-01-01T00:00:00Z",
            "author_hash": "abc", "text": text, "collection_method": "scraped", "keyword_group": "core",
            "raw_meta": {"platform_tag": platform}, "hinglish": False, "pii_stripped": False,
            "clean_text": text, "clean_borderline": borderline}


@pytest.fixture
def es(make_settings, tmp_path):
    def _make(posts, golden=("golden:1",), labelled=False, batch_size=30, **budget):
        s = make_settings(**budget)
        s.paths.enriched_dir = str(tmp_path / "enriched")
        s.paths.seeds_dir = str(tmp_path / "seeds")
        s.llm.batch_size = batch_size
        clean = Path(s.paths.clean_dir)
        clean.mkdir(parents=True, exist_ok=True)
        allposts = list(posts) + [mk(g.split(":")[1], source=g.split(":")[0]) for g in golden]
        with open(clean / "clean_posts.jsonl", "w", encoding="utf-8") as f:
            for p in allposts:
                f.write(json.dumps(p) + "\n")
        seeds = Path(s.paths.seeds_dir)
        seeds.mkdir(parents=True, exist_ok=True)
        with open(seeds / "golden_human.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            w.writeheader()
            for g in golden:
                row = {"post_id": g, "bucket": "stratified", "source": "golden", "text": "t"}
                if labelled:
                    row.update(is_relevant="false")
                w.writerow(row)
        return s
    return _make


def item(pid, relevant=True, span=None, **kw):
    d = {"post_id": pid, "is_relevant": relevant}
    if relevant:
        d.update(outcome="stopped", trigger_type="life_event_financial_pressure", kpi_branch="voluntary_stop",
                 stop_scenario_ids=[1], evidence_span=span or "In May I stopped it because rent went up",
                 journey_stage="month_4_6", is_first_time_investor="yes")
    d.update(kw)
    return d


def resp(items, out_tokens=200):
    return json.dumps({"items": items}), Usage(input_tokens=4000, output_tokens=out_tokens)


def ids_in_prompt(call):
    return [p["post_id"] for p in json.loads(call["prompt"].split("Posts (JSON):\n", 1)[1].split("\n\nNOTE")[0])]


def read_rows(s):
    p = Path(s.paths.enriched_dir) / E.ENRICHED_NAME
    return [json.loads(l) for l in open(p, encoding="utf-8")] if p.exists() else []


def run(s, provider, limit=None, approve=True):
    return E.run_enrich(s, limit=limit, approve=lambda: approve, provider=provider, sleep=lambda _: None)


# ----------------------------------------------------------------------------- schema


def test_kpi_branch_scenarios_and_schema_for_gemini():
    f = EnrichmentFields(post_id="p", is_relevant=True, kpi_branch="continuing", journey_stage="month_12_plus",
                         stop_scenario_ids=[0, 2, 2, 18, 5, 9, 11])
    assert f.stop_scenario_ids == [2, 5, 9]          # out-of-range dropped, deduped, max 3
    with pytest.raises(Exception):
        EnrichmentFields(post_id="p", is_relevant=True, kpi_branch="maybe")
    schema = json.dumps(clean_schema_for_gemini(EnrichmentBatchResponse))
    assert "additionalProperties" not in schema and "additional_properties" not in schema
    assert sorted(ENRICHMENT_PROPERTY_ORDER) == sorted(EnrichmentFields.model_fields)
    assert ENRICHMENT_PROPERTY_ORDER.index("is_relevant") < ENRICHMENT_PROPERTY_ORDER.index("evidence_span")


# ----------------------------------------------------------------------------- sampling


def test_test_sample_excludes_golden_is_stratified_and_deterministic():
    posts = [mk(i, src, borderline=(i % 3 == 0)) for src in E.TEST_SOURCE_WEIGHTS for i in range(10)]
    golden = {posts[0]["id"], posts[11]["id"]}
    a = E.select_test_sample(posts, golden, 20)
    b = E.select_test_sample(posts, golden, 20)
    assert [p["id"] for p in a] == [p["id"] for p in b]
    assert len(a) == 20 and not golden & {p["id"] for p in a}
    assert {p["source"] for p in a} == set(E.TEST_SOURCE_WEIGHTS)
    assert any(p["clean_borderline"] for p in a) and any(not p["clean_borderline"] for p in a)


# ----------------------------------------------------------------------------- happy path + resume


def test_test_run_writes_rows_checkpoint_calibration_and_full_run_resumes(es):
    posts = [mk(i) for i in range(1, 5)]
    s = es(posts)
    fp = FakeProvider([resp([item("reddit:1"), item("reddit:2"), item("reddit:3", relevant=False, kpi_branch="voluntary_stop"),
                             item("reddit:4", outcome="continued", trigger_type="none", kpi_branch="continuing",
                                  stop_scenario_ids=[], evidence_span="I started my first SIP in January")])])
    assert run(s, fp, limit=2) == 0
    rows = read_rows(s)
    assert len(rows) == 2 and all(r["status"] == "ok" for r in rows)
    sent = ids_in_prompt(fp.calls[0])
    assert "golden:1" not in sent and len(sent) == 2
    for r in rows:
        e = r["enrichment"]
        if e["is_relevant"]:
            assert r["evidence_verified"] and 0 <= r["segment_fit"] <= 1
    assert (Path(s.paths.checkpoints) / E.CALIBRATION_NAME).exists()
    done = Checkpoint("enrich", Path(s.paths.checkpoints))._done
    assert done == set(sent)

    # full run needs golden labels -> refuses without them, makes no call
    fp2 = FakeProvider([])
    assert run(s, fp2) == 2 and fp2.calls == []


def test_full_run_only_sends_pending_posts(es):
    posts = [mk(i) for i in range(1, 5)]
    s = es(posts, labelled=True)
    fp = FakeProvider([resp([item(p["id"]) for p in posts])])   # extra ids in a reply are ignored
    run(s, fp, limit=2)
    first = set(ids_in_prompt(fp.calls[0]))
    remaining = [p["id"] for p in posts if p["id"] not in first] + ["golden:1"]
    fp2 = FakeProvider([resp([item(pid) for pid in remaining])])
    assert run(s, fp2) == 0
    assert set(ids_in_prompt(fp2.calls[0])) == set(remaining)      # never re-sends finished posts
    assert len(read_rows(s)) == 5


def test_irrelevant_forced_not_applicable_and_platform_hint(es):
    s = es([mk(1, platform="zerodha")])
    fp = FakeProvider([resp([item("reddit:1", relevant=False, kpi_branch="voluntary_stop", stop_scenario_ids=[2],
                                  platform_tag="unspecified")])])
    run(s, fp, limit=1)
    e = read_rows(s)[0]["enrichment"]
    assert e["kpi_branch"] == "not_applicable" and e["stop_scenario_ids"] == [] and e["platform_tag"] == "zerodha"
    assert read_rows(s)[0]["segment_fit"] is None


# ----------------------------------------------------------------------------- retries / failures


def test_missing_ids_retried_once_then_marked_failed(es):
    s = es([mk(i) for i in range(1, 4)])
    fp = FakeProvider([resp([item("reddit:1"), item("reddit:2")]), resp([])])
    run(s, fp, limit=3)
    assert len(fp.calls) == 2
    missing_call = fp.calls[1]
    assert "missing or invalid" in missing_call["prompt"] and len(ids_in_prompt(missing_call)) == 1
    st = {r["id"]: r["status"] for r in read_rows(s)}
    assert sorted(st.values()) == ["enrichment_failed", "ok", "ok"]


def test_invalid_item_is_retried_not_whole_batch(es):
    s = es([mk(1), mk(2)])
    bad = item("reddit:2", outcome="not_a_value")
    fp = FakeProvider([resp([item("reddit:1"), bad]), resp([item("reddit:2")])])
    run(s, fp, limit=2)
    assert all(r["status"] == "ok" for r in read_rows(s))
    assert ids_in_prompt(fp.calls[1]) == ["reddit:2"]


def test_evidence_retry_then_downgrade_keeps_ac5(es):
    s = es([mk(1), mk(2)])
    para = "they stopped because of rent"            # not verbatim
    fp = FakeProvider([resp([item("reddit:1", span=para), item("reddit:2", span=para)]),
                       resp([item("reddit:1"), item("reddit:2", span=para)])])   # retry fixes only #1
    run(s, fp, limit=2)
    assert "exact substring" in fp.calls[1]["prompt"]
    rows = {r["id"]: r for r in read_rows(s)}
    ok = next(r for r in rows.values() if r["status"] == "ok")
    failed = next(r for r in rows.values() if r["status"] == "evidence_span_failed")
    assert ok["evidence_verified"] is True
    assert failed["enrichment"]["outcome"] == "unknown" and failed["enrichment"]["trigger_type"] == "unknown"
    assert failed["unverified_labels"]["outcome"] == "stopped" and failed["segment_fit"] is None
    # AC5: every ok row with a non-unknown outcome/trigger has a verified span
    for r in rows.values():
        e = r["enrichment"]
        if r["status"] == "ok" and e["is_relevant"] and (e["outcome"] != "unknown" or e["trigger_type"] not in ("unknown", "none")):
            assert r["evidence_verified"]


class FinishProvider(FakeProvider):
    """Fake provider that also reports a finish_reason per call."""

    def __init__(self, responses, reasons):
        super().__init__(responses)
        self.reasons = list(reasons)
        self.last_finish_reason = None

    def generate(self, **kw):
        self.last_finish_reason = self.reasons.pop(0)
        return super().generate(**kw)


def test_max_tokens_splits_batch_instead_of_blind_retry(es):
    s = es([mk(i) for i in range(1, 5)])
    cut = ('{"items": [{"post_id": "reddit:1", "is_relev', Usage(4000, 1400))
    run_ids = [p["id"] for p in E.select_test_sample([mk(i) for i in range(1, 5)], {"golden:1"}, 4)]
    fp = FinishProvider([cut, resp([item(pid) for pid in run_ids[:2]]), resp([item(pid) for pid in run_ids[2:]])],
                        ["MAX_TOKENS", "STOP", "STOP"])
    run(s, fp, limit=4)
    assert len(fp.calls) == 3
    assert fp.calls[1]["max_output_tokens"] < fp.calls[0]["max_output_tokens"]
    assert "Your previous reply was not valid JSON" not in fp.calls[1]["prompt"]
    assert all(r["status"] == "ok" for r in read_rows(s))
    log_rows = list(csv.DictReader(open(Path(s.paths.spend_log), encoding="utf-8")))
    assert any(r["call_status"] == "max_tokens" for r in log_rows)


# ----------------------------------------------------------------------------- stops


def test_402_stops_cleanly_and_keeps_finished_batches(es):
    s = es([mk(i) for i in range(1, 5)], batch_size=2)
    run_ids = [p["id"] for p in E.select_test_sample([mk(i) for i in range(1, 5)], {"golden:1"}, 4)]
    fp = FakeProvider([resp([item(pid) for pid in run_ids[:2]]), ProviderError("credits", code=402)])
    assert run(s, fp, limit=4) == 1
    rows = read_rows(s)
    assert [r["id"] for r in rows] == run_ids[:2]
    assert Checkpoint("enrich", Path(s.paths.checkpoints))._done == set(run_ids[:2])
    log_rows = list(csv.DictReader(open(Path(s.paths.spend_log), encoding="utf-8")))
    assert log_rows[-1]["call_status"] == "error_402"


def test_budget_cap_stops_before_any_call(es):
    s = es([mk(1), mk(2)], per_run_cap_inr=0.0001)
    fp = FakeProvider([])
    assert run(s, fp, limit=2) == 1 and fp.calls == [] and read_rows(s) == []


def test_no_approval_means_no_calls(es):
    s = es([mk(1)])
    fp = FakeProvider([])
    assert run(s, fp, limit=1, approve=False) == 0 and fp.calls == []


def test_full_run_requires_calibration_first(es):
    s = es([mk(1)], labelled=True)
    fp = FakeProvider([])
    assert run(s, fp) == 2 and fp.calls == []


def test_prompt_sends_only_masked_truncated_text(es):
    long = "My SIP autopay failed, call me on [PHONE]. " + "x" * 3000
    s = es([mk(1, text=long)])
    fp = FakeProvider([resp([item("reddit:1", span="My SIP autopay failed")])])
    run(s, fp, limit=1)
    payload = json.loads(fp.calls[0]["prompt"].split("Posts (JSON):\n", 1)[1])
    assert payload[0]["text"].startswith("My SIP autopay failed, call me on [PHONE]")
    assert len(payload[0]["text"]) == s.llm.max_input_chars_per_post
    assert fp.calls[0]["system"].startswith("You are a research analyst")


# ----------------------------------------------------------------------------- v2: consistency + redo-test


def test_consistency_rules_fix_contradictory_branches():
    f = lambda **kw: EnrichmentFields(post_id="p", is_relevant=True, **kw)
    assert E.consistent_branch(f(outcome="continued", kpi_branch="voluntary_stop", stop_scenario_ids=[2])) == ("continuing", [])
    assert E.consistent_branch(f(outcome="unknown", kpi_branch="voluntary_stop", stop_scenario_ids=[3])) == ("not_applicable", [])
    assert E.consistent_branch(f(outcome="unknown", kpi_branch="involuntary_payment_failure", stop_scenario_ids=[11])) == ("involuntary_payment_failure", [11])
    assert E.consistent_branch(f(outcome="restarted", kpi_branch="voluntary_stop", stop_scenario_ids=[1])) == ("lapse_and_return", [1])
    assert E.consistent_branch(f(outcome="stopped", kpi_branch="continuing")) == ("voluntary_stop", [])
    assert E.consistent_branch(f(outcome="stopped", kpi_branch="platform_switch", stop_scenario_ids=[17])) == ("platform_switch", [17])
    assert E.consistent_branch(f(journey_stage="pre_start", outcome="unknown", kpi_branch="involuntary_payment_failure",
                                 stop_scenario_ids=[11])) == ("not_applicable", [])


def test_redo_test_reruns_same_posts_archives_old_and_needs_approval(es, monkeypatch):
    posts = [mk(i) for i in range(1, 7)]
    s = es(posts)
    s.enrich.prompt_file = "enrich_v1_20261027.txt"
    fp = FakeProvider([resp([item(p["id"]) for p in posts])])
    run(s, fp, limit=3)
    first = ids_in_prompt(fp.calls[0])
    # prompt changes; declining approval must leave everything untouched
    s.enrich.prompt_file = "enrich_v2_20261027.txt"
    before_rows = read_rows(s)
    assert E.run_enrich(s, limit=3, approve=lambda: False, provider=FakeProvider([]), redo_test=True) == 0
    assert read_rows(s) == before_rows
    fp2 = FakeProvider([resp([item(pid, outcome="continued", trigger_type="none",
                                   evidence_span="I started my first SIP in January") for pid in first])])
    assert E.run_enrich(s, limit=3, approve=lambda: True, provider=fp2, sleep=lambda _: None, redo_test=True) == 0
    assert ids_in_prompt(fp2.calls[0]) == first                      # the SAME posts
    rows = read_rows(s)
    assert len(rows) == 3 and {r["prompt_version"] for r in rows} == {"enrich_v2_20261027"}
    assert all(r["enrichment"]["kpi_branch"] == "continuing" for r in rows)
    arch = Path(s.paths.corpus_stats).parent / "enrich_tests" / "enrich_v1_20261027_test.jsonl"
    assert len(arch.read_text().splitlines()) == 3
    # a second redo with the same prompt is refused
    assert E.run_enrich(s, limit=3, approve=lambda: True, provider=FakeProvider([]), redo_test=True) == 2


def test_calibration_resets_when_prompt_changes(tmp_path):
    p = tmp_path / "c.json"
    c = E.Calibration(p, "v1"); c.add_main(10, Usage(1000, 2000)); c.d["posts_done"] = 10; c.save()
    c2 = E.Calibration(p, "v2")
    assert not c2.ready and c2.d["history"][0]["prompt_version"] == "v1"


def test_event_sample_picks_first_hand_events_and_skips_golden():
    posts = [mk(i, src) for src in E.TEST_SOURCE_WEIGHTS for i in range(6)]                       # "I started... I stopped"
    posts += [mk(100 + i, "reddit", text="Which flexi cap fund is best for 10 years? Please suggest.") for i in range(10)]
    golden = {posts[0]["id"]}
    s = E.select_event_sample(posts, golden, 20)
    assert len(s) == 20 and posts[0]["id"] not in {p["id"] for p in s}
    assert all("stopped" in p["clean_text"] for p in s)


def test_evidence_check_ignores_only_typography_and_edge_punctuation():
    from core.utils import verify_evidence_span as v
    text = "I’m stopping my SIP — returns were negative… “not worth it” anymore."
    assert v("I'm stopping my SIP - returns were negative", text)            # curly vs straight, dash style
    assert v('"not worth it"', text) and v("negative...", text)              # quote style, ellipsis char
    assert v("  stopping my SIP. ", text)                                   # edge punctuation/space
    assert not v("I am stopping my SIP", text)                              # different wording
    assert not v("stopping SIP my", text)                                   # different order
    assert not v("..", text)                                                # too short


def test_first_pass_evidence_failures_are_logged(es):
    s = es([mk(1)])
    fp = FakeProvider([resp([item("reddit:1", span="they stopped because of rent")]), resp([item("reddit:1")])])
    run(s, fp, limit=1)
    logs = list((Path(s.paths.outputs) / "enrich_tests").glob("evidence_failures_*.jsonl"))
    assert len(logs) == 1 and json.loads(logs[0].read_text().splitlines()[0])["span"] == "they stopped because of rent"
