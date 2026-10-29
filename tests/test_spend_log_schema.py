"""AC16: every call (including failures and repair retries) produces a valid spend_log row."""
import csv

import pytest

from core.budget import SPEND_LOG_COLUMNS, Usage
from core.llm_client import LLMClient, LLMUnavailable, ProviderError
from core.schemas import SmokeTestResult
from tests.conftest import FakeProvider, ok_response

APPROVED = ["timestamp", "stage", "batch_id", "model", "prompt_version", "input_tokens", "output_tokens",
            "thinking_tokens", "cached_tokens", "cost_inr", "cumulative_cost_inr", "retry_count", "call_status"]


def _rows(settings):
    with open(settings.paths.resolve("spend_log"), newline="") as f:
        return list(csv.DictReader(f))


def test_columns_match_approved_schema(settings, tracker):
    assert SPEND_LOG_COLUMNS == APPROVED
    LLMClient(settings, tracker, provider=FakeProvider([ok_response()])).generate_json(
        prompt="p", schema=SmokeTestResult, batch_id="b0", max_output_tokens=200)
    with open(settings.paths.resolve("spend_log"), newline="") as f:
        assert next(csv.reader(f)) == APPROVED


def test_retries_and_repair_are_all_logged(settings, tracker):
    provider = FakeProvider([
        ProviderError("rate limited", code=429, retryable=True),
        ("not json", Usage(300, 20)),
        ok_response(),
    ])
    r = LLMClient(settings, tracker, provider=provider, sleep=lambda s: None).generate_json(
        prompt="p", schema=SmokeTestResult, batch_id="b0", max_output_tokens=200)
    rows = _rows(settings)
    assert r.status == "ok"
    assert [row["call_status"] for row in rows] == ["error_429", "invalid_json", "ok"]
    assert [row["retry_count"] for row in rows] == ["0", "1", "2"]
    assert float(rows[-1]["cumulative_cost_inr"]) == pytest.approx(sum(float(x["cost_inr"]) for x in rows), abs=1e-5)


def test_invalid_json_twice_marks_enrichment_failed(settings, tracker):
    provider = FakeProvider([("nope", Usage(300, 20)), ("still nope", Usage(300, 20))])
    r = LLMClient(settings, tracker, provider=provider).generate_json(
        prompt="p", schema=SmokeTestResult, batch_id="b0", max_output_tokens=200)
    assert r.status == "enrichment_failed"
    assert len(provider.calls) == 2  # exactly one repair retry, no loop


def test_retries_capped_at_three(settings, tracker):
    provider = FakeProvider([ProviderError("503", code=503, retryable=True)] * 10)
    with pytest.raises(LLMUnavailable):
        LLMClient(settings, tracker, provider=provider, sleep=lambda s: None).generate_json(
            prompt="p", schema=SmokeTestResult, batch_id="b0", max_output_tokens=200)
    assert len(provider.calls) == 4  # 1 attempt + 3 retries
    assert len(_rows(settings)) == 4


def test_non_retryable_error_stops_immediately(settings, tracker):
    provider = FakeProvider([ProviderError("bad thinking level", code=400)])
    with pytest.raises(LLMUnavailable) as e:
        LLMClient(settings, tracker, provider=provider).generate_json(
            prompt="p", schema=SmokeTestResult, batch_id="b0", max_output_tokens=200)
    assert e.value.code == 400
    assert len(provider.calls) == 1
