"""AC13: the pipeline halts and checkpoints at the per-run cap (mock costs). Project cap refuses to start."""
import csv

import pytest

from core.budget import BudgetExceeded, BudgetTracker, CallCostAnomaly, ProjectCapReached, Usage
from core.checkpoint import Checkpoint
from core.llm_client import LLMClient, LLMUnavailable, PREPAY_EXHAUSTED_MESSAGE, ProviderError
from core.schemas import SmokeTestResult
from tests.conftest import SMOKE_OK, FakeProvider

# 333,334 output tokens × $3.75/M × 96 ≈ ₹120.00 per call
EXPENSIVE = Usage(input_tokens=0, output_tokens=333_334)


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_halts_at_per_run_cap_and_checkpoints(settings, tracker):
    provider = FakeProvider([(SMOKE_OK, EXPENSIVE)] * 5)
    client = LLMClient(settings, tracker, provider=provider, sleep=lambda s: None)
    ckpt = Checkpoint("enrich", settings.paths.resolve("checkpoints"))
    batches = [f"b{i}" for i in range(5)]

    with pytest.raises(BudgetExceeded):
        for b in ckpt.pending(batches):
            # worst case per call ≈ ₹120 (200 max output tokens are negligible next to the mocked usage)
            client.worst_case_cost = lambda *a, **k: 120.0
            client.generate_json(prompt="p", schema=SmokeTestResult, batch_id=b, max_output_tokens=200)
            ckpt.mark_done([b])

    # ₹120 + ₹120 = ₹240 spent; a third call could reach ₹360 > ₹300 → stopped *before* calling
    assert len(provider.calls) == 2
    assert tracker.run_spend == pytest.approx(240, rel=1e-3)
    assert Checkpoint("enrich", settings.paths.resolve("checkpoints")).pending(batches) == ["b2", "b3", "b4"]
    assert len(_rows(settings.paths.resolve("spend_log"))) == 2


def test_project_cap_refuses_to_start(settings, tracker):
    tracker.record(Usage(output_tokens=2_800_000), "seed")  # ≈ ₹1,008 logged
    fresh = BudgetTracker(settings.budget, settings.paths.resolve("spend_log"), "enrich", "m", "v1")
    with pytest.raises(ProjectCapReached):
        fresh.check_can_start()


def test_project_cap_blocks_call_that_would_cross_it(settings, tracker):
    tracker.record(Usage(output_tokens=2_700_000), "seed")  # ≈ ₹972
    fresh = BudgetTracker(settings.budget, settings.paths.resolve("spend_log"), "enrich", "m", "v1")
    with pytest.raises(ProjectCapReached):
        fresh.preflight(worst_case_cost_inr=50)


def test_cost_anomaly_stops_next_call_but_keeps_current_result(settings, tracker):
    provider = FakeProvider([(SMOKE_OK, Usage(output_tokens=10_000)), (SMOKE_OK, Usage(output_tokens=10))])
    client = LLMClient(settings, tracker, provider=provider, sleep=lambda s: None)
    r = client.generate_json(prompt="p", schema=SmokeTestResult, batch_id="b0", max_output_tokens=200,
                             expected_cost_inr=0.01)  # actual ≈ ₹3.6 → > 3× estimate
    assert r.status == "ok"  # paid result is not thrown away
    with pytest.raises(CallCostAnomaly):
        client.generate_json(prompt="p", schema=SmokeTestResult, batch_id="b1", max_output_tokens=200)
    assert len(provider.calls) == 1


def test_http_402_prepay_exhausted_checkpoints_and_stops_without_retry(settings, tracker):
    provider = FakeProvider([
        (SMOKE_OK, Usage(input_tokens=100, output_tokens=50)),
        ProviderError("Payment Required", code=402, retryable=True),
        (SMOKE_OK, Usage(input_tokens=100, output_tokens=50)),
    ])
    client = LLMClient(settings, tracker, provider=provider, sleep=lambda s: None)
    ckpt = Checkpoint("enrich", settings.paths.resolve("checkpoints"))
    batches = ["b0", "b1", "b2"]

    with pytest.raises(LLMUnavailable) as exc_info:
        for b in ckpt.pending(batches):
            client.generate_json(prompt="p", schema=SmokeTestResult, batch_id=b, max_output_tokens=200)
            ckpt.mark_done([b])

    assert exc_info.value.code == 402
    assert PREPAY_EXHAUSTED_MESSAGE in str(exc_info.value)
    assert exc_info.value.error_message == PREPAY_EXHAUSTED_MESSAGE
    # b0 ran once, b1 hit 402 and stopped immediately with zero retries
    assert len(provider.calls) == 2
    assert Checkpoint("enrich", settings.paths.resolve("checkpoints")).pending(batches) == ["b1", "b2"]
    rows = _rows(settings.paths.resolve("spend_log"))
    assert [r["call_status"] for r in rows] == ["ok", "error_402"]
    assert rows[-1]["retry_count"] == "0"
