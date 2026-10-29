from core.schemas import EnrichmentFields, SegmentSignals
from scoring.segment_fit import segment_fit


def f(**kw):
    seg = kw.pop("seg", {})
    return EnrichmentFields(post_id="p", is_relevant=kw.pop("is_relevant", True), segment_signals=SegmentSignals(**seg), **kw)


def test_full_match_is_one():
    assert segment_fit(f(is_first_time_investor="yes", seg=dict(employment="salaried", age_band="22_28",
                         city_tier="tier_1", income_stability="stable_monthly"))) == 1.0


def test_all_unknown():
    assert segment_fit(f()) == 0.42   # .3*.4 + .25*.4 + .25*.4 + .1*.5 + .1*.5


def test_all_mismatch():
    assert segment_fit(f(is_first_time_investor="no", seg=dict(employment="self_employed", age_band="over_35",
                         city_tier="tier_3", income_stability="variable"))) == 0.04


def test_tenure_or_early_stage_count_as_match():
    assert segment_fit(f(journey_stage="month_2_3", seg=dict(years_of_work="0_1"))) == 0.72


def test_irrelevant_not_scored():
    assert segment_fit(f(is_relevant=False)) is None and segment_fit(None) is None
