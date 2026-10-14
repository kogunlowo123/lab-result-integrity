"""Integrity rules: ranges by age and sex, delta checks, trends, HIL, combinations, units."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lab_result_integrity.domain.catalogue import AnalyteCatalogue, AnalyteSpec
from lab_result_integrity.domain.models import FindingKind, Flag, PriorResult, Severity, Sex
from lab_result_integrity.domain.rules import (
    combination_findings,
    delta_check,
    evaluate_ranges,
    hil_interference,
    select_interval,
    trend,
    trend_of,
    unit_mismatch,
)
from tests.conftest import (
    CALCIUM,
    CREATININE,
    GLUCOSE,
    HEMOGLOBIN,
    NOW,
    PHOSPHATE,
    POTASSIUM,
    ROOT,
    SODIUM,
    make_result,
)

CATALOGUE = AnalyteCatalogue.load(ROOT / "configs" / "analytes.yaml", allow_starter=True)


def spec(code: str) -> AnalyteSpec:
    found = CATALOGUE.analyte(code)
    assert found is not None
    return found


def prior(value: float, hours_ago: float, rid: str = "R-PRIOR") -> PriorResult:
    return PriorResult(
        result_id=rid,
        value=value,
        collected_at=NOW - timedelta(hours=1 + hours_ago),
        specimen_id=f"S-{rid}",
    )


# ------------------------------------------------------------------- ranges


def test_sex_specific_interval_is_selected() -> None:
    assert select_interval(spec(HEMOGLOBIN), Sex.MALE, 40 * 365)[:2] == (13.2, 16.6)
    assert select_interval(spec(HEMOGLOBIN), Sex.FEMALE, 40 * 365)[:2] == (11.6, 15.0)


def test_unknown_sex_uses_narrowest_combined_interval() -> None:
    low, high, basis = select_interval(spec(HEMOGLOBIN), Sex.UNKNOWN, 40 * 365)
    assert (low, high) == (13.2, 15.0)
    assert basis.startswith("narrowest")


def test_neonatal_band_selected_by_age() -> None:
    assert select_interval(spec(POTASSIUM), Sex.FEMALE, 10)[:2] == (3.7, 5.9)
    assert select_interval(spec(POTASSIUM), Sex.FEMALE, 31)[:2] == (3.5, 5.1)


def test_no_interval_for_age_is_reported() -> None:
    low, high, basis = select_interval(spec(CREATININE), Sex.MALE, 365)
    assert (low, high, basis) == (None, None, "no interval for age")
    result = make_result(CREATININE, 0.4, birth_date=date(2025, 9, 1))
    flag, findings = evaluate_ranges(result, spec(CREATININE))
    assert flag is Flag.NORMAL
    assert findings[0].evidence["basis"] == "no interval for age"


@pytest.mark.parametrize(
    ("value", "flag"),
    [
        (4.2, Flag.NORMAL),
        (3.2, Flag.LOW),
        (5.6, Flag.HIGH),
        (2.8, Flag.CRITICAL_LOW),
        (6.4, Flag.CRITICAL_HIGH),
    ],
)
def test_potassium_flags(value: float, flag: Flag) -> None:
    got, findings = evaluate_ranges(make_result(POTASSIUM, value), spec(POTASSIUM))
    assert got is flag
    if flag in (Flag.CRITICAL_LOW, Flag.CRITICAL_HIGH):
        assert findings[-1].kind is FindingKind.CRITICAL_VALUE
        assert findings[-1].severity is Severity.CRITICAL
        assert "clsi-gp47" in findings[-1].citations
    elif flag is Flag.NORMAL:
        assert findings == []
    else:
        assert findings[0].severity is Severity.INFO


def test_implausible_value_is_flagged() -> None:
    _, findings = evaluate_ranges(make_result(SODIUM, 250), spec(SODIUM))
    kinds = [f.kind for f in findings]
    assert FindingKind.PLAUSIBILITY_LIMIT in kinds
    assert FindingKind.CRITICAL_VALUE in kinds


@settings(max_examples=200, deadline=None)
@given(st.floats(min_value=1.0, max_value=12.0, allow_nan=False))
def test_flag_is_consistent_with_limits(value: float) -> None:
    flag, _ = evaluate_ranges(make_result(POTASSIUM, value), spec(POTASSIUM))
    if value <= 2.8:
        assert flag is Flag.CRITICAL_LOW
    elif value >= 6.2:
        assert flag is Flag.CRITICAL_HIGH
    elif value < 3.5:
        assert flag is Flag.LOW
    elif value > 5.1:
        assert flag is Flag.HIGH
    else:
        assert flag is Flag.NORMAL


# -------------------------------------------------------------------- delta


def test_absolute_delta_fires_against_most_recent_prior() -> None:
    history = [prior(3.9, 30, "R-OLD"), prior(4.0, 6, "R-RECENT")]
    findings = delta_check(make_result(POTASSIUM, 5.3), spec(POTASSIUM), history)
    assert len(findings) == 1
    assert findings[0].evidence["prior_result_id"] == "R-RECENT"
    assert findings[0].evidence["absolute_change"] == pytest.approx(1.3)


def test_delta_outside_window_is_ignored() -> None:
    assert delta_check(make_result(POTASSIUM, 5.3), spec(POTASSIUM), [prior(4.0, 60)]) == []


def test_delta_below_limit_is_quiet() -> None:
    assert delta_check(make_result(POTASSIUM, 4.6), spec(POTASSIUM), [prior(4.0, 6)]) == []


def test_percent_delta() -> None:
    findings = delta_check(make_result("777-3", 90), spec("777-3"), [prior(250, 24)])
    assert findings[0].evidence["percent_change"] == pytest.approx(64.0)


def test_mode_all_requires_both_limits() -> None:
    glucose = spec(GLUCOSE)
    assert delta_check(make_result(GLUCOSE, 160), glucose, [prior(90, 4)]) == []  # 70 abs, 78%
    assert delta_check(make_result(GLUCOSE, 300), glucose, [prior(90, 4)])  # 210 abs, 233%


def test_zero_prior_uses_absolute_limit_only() -> None:
    findings = delta_check(make_result(CREATININE, 0.5), spec(CREATININE), [prior(0.0, 4)])
    assert findings[0].evidence["percent_change"] is None


def test_no_delta_rule_means_no_findings() -> None:
    assert delta_check(make_result(PHOSPHATE, 9), spec(PHOSPHATE), [prior(3, 4)]) == []


@settings(max_examples=100, deadline=None)
@given(
    st.floats(min_value=1.5, max_value=11.0, allow_nan=False),
    st.floats(min_value=1.5, max_value=11.0, allow_nan=False),
)
def test_delta_fires_iff_absolute_change_reaches_limit(old: float, new: float) -> None:
    fired = bool(delta_check(make_result(POTASSIUM, new), spec(POTASSIUM), [prior(old, 2)]))
    assert fired == (abs(new - old) >= 1.0)


# -------------------------------------------------------------------- trend


def test_trend_needs_three_points() -> None:
    summary = trend(make_result(POTASSIUM, 4.5), spec(POTASSIUM), [prior(4.0, 10)])
    assert summary.slope_per_day is None
    assert summary.direction == "insufficient data"


def test_rising_and_falling_trends() -> None:
    history = [prior(1.0, 48), prior(1.4, 24)]
    rising = trend(make_result(CREATININE, 1.8), spec(CREATININE), history)
    assert rising.direction == "rising"
    assert rising.slope_per_day == pytest.approx(0.4, rel=1e-6)
    falling = trend(make_result(CREATININE, 0.6), spec(CREATININE), history)
    assert falling.direction == "falling"


def test_stable_trend_and_degenerate_series() -> None:
    history = [prior(4.0, 48), prior(4.0, 24)]
    assert trend(make_result(POTASSIUM, 4.0), spec(POTASSIUM), history).direction == "stable"
    same = [(NOW, 1.0), (NOW, 2.0), (NOW, 3.0)]
    assert trend_of(same, 24).slope_per_day == 0.0
    assert trend_of([], 24).points == 0


# ------------------------------------------------------------- interference


def test_hil_flag_and_withhold() -> None:
    potassium = spec(POTASSIUM)
    assert hil_interference(make_result(POTASSIUM, 4.2, hemolysis=20), potassium) == []
    flagged = hil_interference(make_result(POTASSIUM, 4.2, hemolysis=80), potassium)
    assert [f.severity for f in flagged] == [Severity.WARNING]
    withheld = hil_interference(make_result(POTASSIUM, 4.2, hemolysis=250), potassium)
    assert {f.evidence["action"] for f in withheld} == {"flag", "withhold"}
    assert max(f.severity.rank for f in withheld) == Severity.HIGH.rank


def test_icterus_and_lipemia_rules() -> None:
    assert hil_interference(make_result(CREATININE, 1.0, icterus=12), spec(CREATININE))
    assert hil_interference(make_result(SODIUM, 140, lipemia=1500), spec(SODIUM))
    assert hil_interference(make_result(SODIUM, 140, hemolysis=900), spec(SODIUM)) == []


def test_hemolysis_pseudohyperkalemia_combination() -> None:
    result = make_result(POTASSIUM, 5.8, hemolysis=120)
    findings = combination_findings(
        result, CATALOGUE.rules_for(POTASSIUM), {POTASSIUM: result.value}
    )
    assert [f.rule_id for f in findings] == ["hemolysis-pseudohyperkalemia"]
    assert findings[0].evidence["observed"]["hemolysis_index"] == 120


def test_edta_combination_needs_both_analytes() -> None:
    result = make_result(POTASSIUM, 7.1)
    rules = CATALOGUE.rules_for(POTASSIUM)
    assert combination_findings(result, rules, {POTASSIUM: 7.1}) == []
    findings = combination_findings(result, rules, {POTASSIUM: 7.1, CALCIUM: 5.2})
    assert findings[0].rule_id == "k2edta-contamination"
    assert findings[0].severity is Severity.CRITICAL


def test_delayed_separation_combination() -> None:
    result = make_result(GLUCOSE, 41)
    values = {POTASSIUM: 5.9, PHOSPHATE: 5.6, GLUCOSE: 41}
    ids = [f.rule_id for f in combination_findings(result, CATALOGUE.rules_for(GLUCOSE), values)]
    assert "delayed-separation" in ids


def test_unit_mismatch() -> None:
    assert unit_mismatch(make_result(GLUCOSE, 5.5, unit="mmol/L"), spec(GLUCOSE)) is not None
    assert unit_mismatch(make_result(GLUCOSE, 95, unit="MG/DL"), spec(GLUCOSE)) is None
