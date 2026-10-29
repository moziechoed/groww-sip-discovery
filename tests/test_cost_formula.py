"""PRD AC18: cost = input × input_price + (output + thinking) × output_price + cached × cached_price, × usd_inr_rate."""
import pytest

from core.budget import Usage, compute_cost_inr


def test_config_prices_and_rate(settings):
    b = settings.budget
    assert (b.input_price_per_m_usd, b.output_price_per_m_usd, b.cached_input_price_per_m_usd) == (0.75, 3.75, 0.075)
    assert b.usd_inr_rate == 96


def test_each_component_alone(settings):
    b = settings.budget
    assert compute_cost_inr(Usage(input_tokens=1_000_000), b) == pytest.approx(0.75 * 96)       # ₹72.00
    assert compute_cost_inr(Usage(output_tokens=1_000_000), b) == pytest.approx(3.75 * 96)      # ₹360.00
    assert compute_cost_inr(Usage(thinking_tokens=1_000_000), b) == pytest.approx(3.75 * 96)    # thinking = output price
    assert compute_cost_inr(Usage(cached_tokens=1_000_000), b) == pytest.approx(0.075 * 96)     # ₹7.20


def test_combined_formula(settings):
    # (10,000×0.75 + (2,000+500)×3.75 + 4,000×0.075) / 1e6 = 0.017175 USD → ₹1.6488
    u = Usage(input_tokens=10_000, output_tokens=2_000, thinking_tokens=500, cached_tokens=4_000)
    assert compute_cost_inr(u, settings.budget) == pytest.approx(1.6488)


def test_thinking_billed_exactly_like_output(settings):
    b = settings.budget
    assert compute_cost_inr(Usage(output_tokens=700), b) == compute_cost_inr(Usage(thinking_tokens=700), b)


def test_zero_usage_costs_nothing(settings):
    assert compute_cost_inr(Usage(), settings.budget) == 0.0


def test_usage_metadata_mapping_subtracts_cached():
    from types import SimpleNamespace

    from core.llm_client import usage_from_metadata

    md = SimpleNamespace(prompt_token_count=1200, cached_content_token_count=1000,
                         candidates_token_count=90, thoughts_token_count=None)
    assert usage_from_metadata(md) == Usage(input_tokens=200, output_tokens=90, thinking_tokens=0, cached_tokens=1000)
