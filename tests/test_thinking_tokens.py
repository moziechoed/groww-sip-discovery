"""AC15: thinking set to the lowest level, thinking tokens logged per call, warning when > 10% of output."""
import csv
import logging

from core.config import Settings, load_raw_config
from core.llm_client import LLMClient, thinking_kwargs
from core.schemas import SmokeTestResult
from tests.conftest import FakeProvider, ok_response


def _call(settings, tracker, response):
    provider = FakeProvider([response])
    client = LLMClient(settings, tracker, provider=provider, sleep=lambda s: None)
    client.generate_json(prompt="p", schema=SmokeTestResult, batch_id="b0", max_output_tokens=200)
    return provider


def test_zero_thinking_logged_and_no_warning(settings, tracker, caplog):
    with caplog.at_level(logging.WARNING, logger="llm_client"):
        _call(settings, tracker, ok_response(output_tokens=100, thinking_tokens=0))
    rows = list(csv.DictReader(open(settings.paths.resolve("spend_log"))))
    assert rows[0]["thinking_tokens"] == "0"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_high_thinking_warns(settings, tracker, caplog):
    with caplog.at_level(logging.WARNING, logger="llm_client"):
        _call(settings, tracker, ok_response(output_tokens=100, thinking_tokens=500))
    rows = list(csv.DictReader(open(settings.paths.resolve("spend_log"))))
    assert rows[0]["thinking_tokens"] == "500"
    assert any("thinking tokens" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_tagging_call_sends_lowest_level_only(settings, tracker):
    provider = _call(settings, tracker, ok_response())
    assert provider.calls[0]["thinking"] == {"thinking_level": "minimal"}


def test_budget_mode_sends_budget_only():
    raw = load_raw_config()
    raw["llm"]["thinking_param"] = "thinking_budget"
    s = Settings.model_validate(raw)
    assert thinking_kwargs(s) == {"thinking_budget": 0}


def test_never_both_controls(settings):
    for call_type in ("tagging", "clustering"):
        assert len(thinking_kwargs(settings, call_type)) == 1
