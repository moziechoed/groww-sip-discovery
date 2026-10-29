"""AC14: no Gemini call without a printed estimate and a typed 'yes'. Anything else exits cleanly."""
import pytest

import run
from core.config import Settings, load_raw_config
from tests.conftest import FakeProvider, ok_response


@pytest.mark.parametrize("answer", ["no", "", "y", "YES please", "ok"])
def test_anything_but_yes_makes_no_call(settings, answer, capsys):
    provider = FakeProvider([ok_response()])
    code = run.cmd_test_call(settings, input_fn=lambda _: answer, provider=provider)
    out = capsys.readouterr().out
    assert code == 0
    assert provider.calls == []
    assert "Pre-run Estimate" in out and "Est. cost" in out and "Budget remaining" in out
    assert "no Gemini calls made" in out


@pytest.mark.parametrize("answer", ["yes", "YES", "  Yes  "])
def test_yes_proceeds_with_exactly_one_call(settings, answer):
    provider = FakeProvider([ok_response()])
    code = run.cmd_test_call(settings, input_fn=lambda _: answer, provider=provider)
    assert code == 0
    assert len(provider.calls) == 1


def test_eof_counts_as_no(settings):
    def eof(_):
        raise EOFError

    provider = FakeProvider([ok_response()])
    run.cmd_test_call(settings, input_fn=eof, provider=provider)
    assert provider.calls == []


def test_estimate_printed_before_prompt(settings, capsys):
    seen = {}

    def answer(_):
        seen["out"] = capsys.readouterr().out
        return "no"

    run.cmd_test_call(settings, input_fn=answer, provider=FakeProvider([ok_response()]))
    assert "Pre-run Estimate" in seen["out"]


def test_approval_cannot_be_disabled_in_config():
    raw = load_raw_config()
    raw["budget"]["require_approval"] = False
    with pytest.raises(Exception):
        Settings.model_validate(raw)


def test_cli_has_no_bypass_flag():
    with pytest.raises(SystemExit):
        run.main(["--stage", "test-call", "--yes"])
