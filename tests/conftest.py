"""Shared fixtures. All tests use a fake provider — the real Gemini API is never called (Guardrails §9)."""
from __future__ import annotations

from typing import Any, List

import pytest

from core.budget import BudgetTracker, Usage
from core.config import Settings, load_raw_config


class FakeProvider:
    """Returns queued (text, Usage) tuples or raises queued exceptions."""

    name = "fake"

    def __init__(self, responses: List[Any] = None):
        self.responses = list(responses or [])
        self.calls: List[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def list_models(self):
        return ["models/gemini-3.6-flash", "models/gemini-3.6-flash-lite"]


@pytest.fixture(autouse=True)
def _default_test_salt(monkeypatch):
    monkeypatch.setenv("AUTHOR_HASH_SALT", "pytest-valid-author-salt-98765")


@pytest.fixture
def make_settings(tmp_path):
    def _make(**budget_overrides) -> Settings:
        raw = load_raw_config()
        raw["paths"] = {
            "outputs": str(tmp_path / "outputs"),
            "prompts": raw.get("paths", {}).get("prompts", "prompts"),
            "checkpoints": str(tmp_path / "checkpoints"),
            "raw_dir": str(tmp_path / "raw"),
            "clean_dir": str(tmp_path / "clean"),
            "spend_log": str(tmp_path / "outputs" / "spend_log.csv"),
            "corpus_stats": str(tmp_path / "outputs" / "corpus_stats.json"),
        }
        raw["budget"].update(budget_overrides)
        return Settings.model_validate(raw)

    return _make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def tracker(settings) -> BudgetTracker:
    return BudgetTracker(settings.budget, settings.paths.resolve("spend_log"), stage="test",
                         model=settings.llm.model, prompt_version="v1")


SMOKE_OK = '{"post_id": "smoke-1", "is_relevant": true, "outcome": "restarted", "evidence_span": "restarted in January"}'


def ok_response(input_tokens=300, output_tokens=40, thinking_tokens=0, cached_tokens=0, text=SMOKE_OK):
    return text, Usage(input_tokens, output_tokens, thinking_tokens, cached_tokens)
