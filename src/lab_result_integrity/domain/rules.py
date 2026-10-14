"""Integrity rules as pure functions over results and the analyte catalogue.

Nothing here performs I/O or reads a clock; callers pass history and time in.
That keeps every rule deterministic, replayable from the evidence packet, and
testable with property-based tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from lab_result_integrity.domain.catalogue import (
    AnalyteSpec,
    CombinationRule,
    Condition,
    HilAction,
    ReferenceInterval,
)
from lab_result_integrity.domain.models import (
    Finding,
    FindingKind,
    Flag,
    HilIndex,
    LabResult,
    PriorResult,
    Severity,
    Sex,
    TrendSummary,
)

# ----------------------------------------------------------- reference ranges


def select_interval(
    spec: AnalyteSpec, sex: Sex, age_days: int
) -> tuple[float | None, float | None, str]:
    """Pick the reference interval for a patient.

    The most specific interval wins: a sex-specific band beats ``any``. When
    sex is unknown or other and only sex-specific bands exist for the age, the
    narrowest combined interval (highest low, lowest high) is used so that an
    abnormal result is not missed; the basis is reported in the evidence.
    """
    candidates = [i for i in spec.reference_intervals if _age_covers(i, age_days)]
    if sex in (Sex.MALE, Sex.FEMALE):
        specific = [i for i in candidates if i.sex == sex.value]
        if specific:
            return specific[0].low, specific[0].high, f"{sex.value}-specific"
    generic = [i for i in candidates if i.sex == "any"]
    if generic:
        return generic[0].low, generic[0].high, "sex-independent"
    if not candidates:
        return None, None, "no interval for age"
    lows = [i.low for i in candidates if i.low is not None]
    highs = [i.high for i in candidates if i.high is not None]
    return (
        max(lows) if lows else None,
        min(highs) if highs else None,
        "narrowest combined (sex unknown)",
    )


def _age_covers(interval: ReferenceInterval, age_days: int) -> bool:
    return age_days >= interval.age_min_days and (
        interval.age_max_days is None or age_days < interval.age_max_days
    )


def evaluate_ranges(result: LabResult, spec: AnalyteSpec) -> tuple[Flag, list[Finding]]:
    """Plausibility, critical and reference-interval evaluation."""
    findings: list[Finding] = []
    value = result.value
    plausible = spec.plausible
    if not plausible.min <= value <= plausible.max:
        findings.append(
            Finding(
                kind=FindingKind.PLAUSIBILITY_LIMIT,
                severity=Severity.HIGH,
                rule_id=f"{spec.code}:plausible",
                message=(
                    f"{spec.name} {value:g} {spec.unit} is outside the plausible range "
                    f"{plausible.min:g}-{plausible.max:g}; suspect analytical or "
                    "pre-analytical error"
                ),
                evidence={"value": value, "min": plausible.min, "max": plausible.max},
                citations=plausible.cite,
            )
        )

    critical = spec.critical
    if critical is not None:
        if critical.low is not None and value <= critical.low:
            findings.append(_critical(spec, value, "low", critical.low, critical.cite))
            return Flag.CRITICAL_LOW, findings
        if critical.high is not None and value >= critical.high:
            findings.append(_critical(spec, value, "high", critical.high, critical.cite))
            return Flag.CRITICAL_HIGH, findings

    age = result.patient.age_days(result.collected_at)
    low, high, basis = select_interval(spec, result.patient.sex, age)
    flag = Flag.NORMAL
    if low is not None and value < low:
        flag = Flag.LOW
    elif high is not None and value > high:
        flag = Flag.HIGH
    if flag is not Flag.NORMAL or basis.startswith(("narrowest", "no interval")):
        cites = tuple(
            sorted({c for i in spec.reference_intervals if _age_covers(i, age) for c in i.cite})
        )
        findings.append(
            Finding(
                kind=FindingKind.REFERENCE_RANGE,
                severity=Severity.INFO,
                rule_id=f"{spec.code}:reference",
                message=(
                    f"{spec.name} {value:g} {spec.unit} flagged {flag.value} against "
                    f"{_fmt_range(low, high)} ({basis})"
                ),
                evidence={
                    "value": value,
                    "low": low,
                    "high": high,
                    "basis": basis,
                    "age_days": age,
                    "sex": result.patient.sex.value,
                },
                citations=cites or spec.reference_intervals[0].cite,
            )
        )
    return flag, findings


def _critical(
    spec: AnalyteSpec, value: float, side: str, limit: float, cite: tuple[str, ...]
) -> Finding:
    return Finding(
        kind=FindingKind.CRITICAL_VALUE,
        severity=Severity.CRITICAL,
        rule_id=f"{spec.code}:critical-{side}",
        message=(
            f"{spec.name} {value:g} {spec.unit} meets the critical {side} limit {limit:g}; "
            "verify specimen integrity and notify per the critical-result procedure"
        ),
        evidence={"value": value, "limit": limit, "side": side},
        citations=cite,
    )


def _fmt_range(low: float | None, high: float | None) -> str:
    return f"{'' if low is None else f'{low:g}'}-{'' if high is None else f'{high:g}'}"


# ------------------------------------------------------------ delta and trend


def delta_check(
    result: LabResult, spec: AnalyteSpec, history: Sequence[PriorResult]
) -> list[Finding]:
    """Compare against the most recent prior result inside the delta window.

    ``history`` holds the patient's earlier results for the analyte, any order.
    """
    rule = spec.delta
    if rule is None:
        return []
    window_start = result.collected_at - timedelta(hours=rule.window_hours)
    eligible = [p for p in history if window_start <= p.collected_at < result.collected_at]
    if not eligible:
        return []
    prior = max(eligible, key=lambda p: p.collected_at)
    absolute = result.value - prior.value
    percent = None if prior.value == 0 else abs(absolute) / abs(prior.value) * 100
    abs_hit = rule.absolute is not None and abs(absolute) >= rule.absolute
    pct_hit = rule.percent is not None and percent is not None and percent >= rule.percent
    fired = (abs_hit and pct_hit) if rule.mode == "all" else (abs_hit or pct_hit)
    if not fired:
        return []
    hours = (result.collected_at - prior.collected_at).total_seconds() / 3600
    return [
        Finding(
            kind=FindingKind.DELTA_CHECK,
            severity=Severity.HIGH,
            rule_id=f"{spec.code}:delta",
            message=(
                f"{spec.name} changed {absolute:+g} {spec.unit}"
                + (f" ({percent:.1f}%)" if percent is not None else "")
                + f" from {prior.value:g} in {hours:.1f} h; confirm patient identity and "
                "specimen before reporting"
            ),
            evidence={
                "prior_result_id": prior.result_id,
                "prior_value": prior.value,
                "prior_collected_at": prior.collected_at.isoformat(),
                "absolute_change": absolute,
                "percent_change": percent,
                "hours_between": hours,
                "absolute_limit": rule.absolute,
                "percent_limit": rule.percent,
                "mode": rule.mode,
            },
            citations=rule.cite,
        )
    ]


def trend(result: LabResult, spec: AnalyteSpec, history: Sequence[PriorResult]) -> TrendSummary:
    """Ordinary least-squares slope (units per day) over the trend window."""
    window = spec.trend_window_hours
    start = result.collected_at - timedelta(hours=window)
    points = [
        (p.collected_at, p.value) for p in history if start <= p.collected_at < result.collected_at
    ]
    points.append((result.collected_at, result.value))
    return trend_of(points, window)


def trend_of(series: Sequence[tuple[datetime, float]], window: float) -> TrendSummary:
    """Least-squares slope of ``(time, value)`` points, in value units per day."""
    points = sorted(series)
    if not points:
        return TrendSummary(
            points=0,
            window_hours=window,
            slope_per_day=None,
            first_value=None,
            last_value=None,
            direction="insufficient data",
        )
    if len(points) < 3:
        return TrendSummary(
            points=len(points),
            window_hours=window,
            slope_per_day=None,
            first_value=points[0][1],
            last_value=points[-1][1],
            direction="insufficient data",
        )
    origin = points[0][0]
    xs = [(t - origin).total_seconds() / 86400 for t, _ in points]
    ys = [v for _, v in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        slope = 0.0
    else:
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    scale = max(abs(mean_y), 1e-9)
    direction = "stable" if abs(slope) / scale < 0.01 else ("rising" if slope > 0 else "falling")
    return TrendSummary(
        points=len(points),
        window_hours=window,
        slope_per_day=slope,
        first_value=ys[0],
        last_value=ys[-1],
        direction=direction,
    )


# --------------------------------------------------------------- interference


def hil_interference(result: LabResult, spec: AnalyteSpec) -> list[Finding]:
    findings: list[Finding] = []
    for rule in spec.interference:
        observed = result.indices.value(rule.index)
        if observed is None or observed < rule.threshold:
            continue
        withhold = rule.action is HilAction.WITHHOLD
        findings.append(
            Finding(
                kind=FindingKind.HIL_INTERFERENCE,
                severity=Severity.HIGH if withhold else Severity.WARNING,
                rule_id=f"{spec.code}:{rule.index.value}-{rule.threshold:g}",
                message=(
                    f"{rule.index.value} index {observed:g} >= {rule.threshold:g}: "
                    f"{spec.name} subject to {rule.bias.value} bias"
                    + ("; result should be withheld pending recollection" if withhold else "")
                ),
                evidence={
                    "index": rule.index.value,
                    "observed": observed,
                    "threshold": rule.threshold,
                    "bias": rule.bias.value,
                    "action": rule.action.value,
                },
                citations=rule.cite,
            )
        )
    return findings


def combination_findings(
    result: LabResult,
    rules: Sequence[CombinationRule],
    specimen_values: Mapping[str, float],
) -> list[Finding]:
    """Evaluate cross-analyte rules against all results of the same specimen.

    ``specimen_values`` maps LOINC code to value for every result of the
    specimen, including ``result`` itself. A rule fires only when every
    condition can be evaluated and holds.
    """
    findings: list[Finding] = []
    for rule in rules:
        observed: dict[str, float] = {}
        holds = True
        for condition in rule.conditions:
            actual, key = _condition_input(condition, result, specimen_values)
            if actual is None or not condition.op.holds(actual, condition.value):
                holds = False
                break
            observed[key] = actual
        if not holds:
            continue
        findings.append(
            Finding(
                kind=FindingKind.IMPLAUSIBLE_COMBINATION,
                severity=Severity(rule.severity),
                rule_id=rule.id,
                message=f"{rule.description}; pattern consistent with {rule.suspected_cause}",
                evidence={
                    "specimen_id": result.specimen_id,
                    "observed": observed,
                    "conditions": [c.describe() for c in rule.conditions],
                    "suspected_cause": rule.suspected_cause,
                },
                citations=rule.cite,
            )
        )
    return findings


def _condition_input(
    condition: Condition, result: LabResult, specimen_values: Mapping[str, float]
) -> tuple[float | None, str]:
    if condition.analyte is not None:
        return specimen_values.get(condition.analyte), condition.analyte
    index: HilIndex | None = condition.index
    if index is None:  # pragma: no cover - rejected by Condition validation
        return None, "index"
    return result.indices.value(index), f"{index.value}_index"


def unit_mismatch(result: LabResult, spec: AnalyteSpec) -> Finding | None:
    if result.unit.strip().lower() == spec.unit.lower():
        return None
    return Finding(
        kind=FindingKind.UNIT_MISMATCH,
        severity=Severity.HIGH,
        rule_id=f"{spec.code}:unit",
        message=(
            f"reported unit {result.unit!r} differs from catalogue unit {spec.unit!r}; "
            "no range or delta rule can be applied safely"
        ),
        evidence={"reported_unit": result.unit, "catalogue_unit": spec.unit},
        citations=spec.plausible.cite,
    )
