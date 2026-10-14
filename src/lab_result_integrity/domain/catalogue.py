"""Analyte catalogue: the governed configuration every integrity rule reads.

Reference intervals, critical limits, delta-check limits, HIL thresholds,
implausible-combination rules and patient-based QC parameters are laboratory
decisions. They are loaded from a reviewed YAML file, never hard-coded, and
every numeric threshold names the citation(s) behind it. A catalogue whose
``governance.status`` is not ``approved`` is refused unless the operator
explicitly allows a starter catalogue outside production.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from agentic_core.domain.provenance import canonical_json, content_digest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lab_result_integrity.domain.errors import CatalogueError
from lab_result_integrity.domain.models import HilIndex, Priority


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Cited(_Frozen):
    cite: tuple[str, ...] = Field(min_length=1, description="Citation ids from `sources`")


class ReferenceInterval(Cited):
    sex: Literal["any", "male", "female"] = "any"
    age_min_days: int = Field(default=0, ge=0)
    age_max_days: int | None = Field(default=None, gt=0, description="Exclusive upper bound")
    low: float | None = None
    high: float | None = None

    @model_validator(mode="after")
    def _bounds(self) -> ReferenceInterval:
        if self.low is None and self.high is None:
            raise ValueError("reference interval needs low and/or high")
        if self.low is not None and self.high is not None and self.low >= self.high:
            raise ValueError("reference interval low must be below high")
        if self.age_max_days is not None and self.age_max_days <= self.age_min_days:
            raise ValueError("age_max_days must exceed age_min_days")
        return self


class CriticalLimits(Cited):
    low: float | None = None
    high: float | None = None

    @model_validator(mode="after")
    def _bounds(self) -> CriticalLimits:
        if self.low is None and self.high is None:
            raise ValueError("critical limits need low and/or high")
        if self.low is not None and self.high is not None and self.low >= self.high:
            raise ValueError("critical low must be below critical high")
        return self


class PlausibleRange(Cited):
    """Values outside this range are treated as analytical or pre-analytical error."""

    min: float
    max: float

    @model_validator(mode="after")
    def _bounds(self) -> PlausibleRange:
        if self.min >= self.max:
            raise ValueError("plausible min must be below max")
        return self


class DeltaRule(Cited):
    window_hours: float = Field(gt=0, le=24 * 365)
    absolute: float | None = Field(default=None, gt=0)
    percent: float | None = Field(default=None, gt=0)
    mode: Literal["any", "all"] = Field(
        default="any", description="any: either limit triggers; all: both must be exceeded"
    )

    @model_validator(mode="after")
    def _limits(self) -> DeltaRule:
        if self.absolute is None and self.percent is None:
            raise ValueError("delta rule needs an absolute and/or percent limit")
        if self.mode == "all" and (self.absolute is None or self.percent is None):
            raise ValueError("mode 'all' needs both absolute and percent limits")
        return self


class Bias(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    UNPREDICTABLE = "unpredictable"


class HilAction(StrEnum):
    FLAG = "flag"  # report with comment after review
    WITHHOLD = "withhold"  # result should not be reported; recollection likely


class HilRule(Cited):
    index: HilIndex
    threshold: float = Field(gt=0, description="Index value at or above which the rule fires")
    bias: Bias
    action: HilAction


class PbrtqcConfig(Cited):
    """Patient-based real-time QC (moving average) parameters per analyte and instrument."""

    method: Literal["sma", "ewma"]
    block_size: int = Field(ge=2, le=1000, description="SMA window / EWMA warm-up length")
    ewma_lambda: float | None = Field(default=None, gt=0, le=1)
    truncation_low: float
    truncation_high: float
    target: float
    control_limit: float = Field(gt=0, description="Alarm when |statistic - target| exceeds this")

    @model_validator(mode="after")
    def _consistent(self) -> PbrtqcConfig:
        if self.truncation_low >= self.truncation_high:
            raise ValueError("truncation_low must be below truncation_high")
        if not self.truncation_low <= self.target <= self.truncation_high:
            raise ValueError("target must lie within the truncation limits")
        if self.method == "ewma" and self.ewma_lambda is None:
            raise ValueError("ewma method needs ewma_lambda")
        return self


class AnalyteSpec(_Frozen):
    code: str = Field(pattern=r"^[0-9]{1,7}-[0-9]$")
    name: str
    unit: str
    reference_intervals: tuple[ReferenceInterval, ...] = Field(min_length=1)
    critical: CriticalLimits | None = None
    plausible: PlausibleRange
    delta: DeltaRule | None = None
    trend_window_hours: float = Field(default=168, gt=0)
    interference: tuple[HilRule, ...] = ()
    pbrtqc: PbrtqcConfig | None = None

    def citations(self) -> set[str]:
        cited: list[Cited] = [*self.reference_intervals, self.plausible, *self.interference]
        cited.extend(c for c in (self.critical, self.delta, self.pbrtqc) if c is not None)
        return {c for item in cited for c in item.cite}


class Operator(StrEnum):
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"

    def holds(self, actual: float, expected: float) -> bool:
        match self:
            case Operator.GT:
                return actual > expected
            case Operator.GTE:
                return actual >= expected
            case Operator.LT:
                return actual < expected
            case Operator.LTE:
                return actual <= expected
        return False  # pragma: no cover - exhaustive match


class Condition(_Frozen):
    """One clause of a combination rule: an analyte value or a serum index."""

    analyte: str | None = Field(default=None, pattern=r"^[0-9]{1,7}-[0-9]$")
    index: HilIndex | None = None
    op: Operator
    value: float

    @model_validator(mode="after")
    def _one_subject(self) -> Condition:
        if (self.analyte is None) == (self.index is None):
            raise ValueError("condition needs exactly one of analyte or index")
        return self

    def describe(self) -> str:
        subject = self.analyte if self.analyte is not None else f"{self.index} index"
        return f"{subject} {self.op.value} {self.value:g}"


class CombinationRule(Cited):
    """Physiologically implausible combination within one specimen."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,63}$")
    description: str
    suspected_cause: str
    severity: Literal["warning", "high", "critical"] = "high"
    conditions: tuple[Condition, ...] = Field(min_length=2)

    def analytes(self) -> set[str]:
        return {c.analyte for c in self.conditions if c.analyte is not None}


class ReviewPolicy(Cited):
    sla_minutes: dict[Priority, int] = Field(description="Time to pathologist decision")
    prior_results_in_evidence: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def _all_priorities(self) -> ReviewPolicy:
        missing = set(Priority) - set(self.sla_minutes)
        if missing:
            raise ValueError(f"sla_minutes missing priorities: {sorted(missing)}")
        if any(v <= 0 for v in self.sla_minutes.values()):
            raise ValueError("sla_minutes must be positive")
        return self


class Governance(_Frozen):
    catalogue_id: str
    version: str
    status: Literal["starter", "approved", "retired"]
    approved_by: str | None = None
    approved_on: date | None = None
    statement: str

    @model_validator(mode="after")
    def _approval(self) -> Governance:
        if self.status == "approved" and (not self.approved_by or self.approved_on is None):
            raise ValueError("an approved catalogue must name approved_by and approved_on")
        return self


class AnalyteCatalogue(_Frozen):
    governance: Governance
    sources: dict[str, str] = Field(min_length=1, description="Citation id -> full reference")
    review_policy: ReviewPolicy
    analytes: tuple[AnalyteSpec, ...] = Field(min_length=1)
    combination_rules: tuple[CombinationRule, ...] = ()

    @model_validator(mode="after")
    def _references(self) -> AnalyteCatalogue:
        codes = [a.code for a in self.analytes]
        duplicates = sorted({c for c in codes if codes.count(c) > 1})
        if duplicates:
            raise ValueError(f"duplicate analyte codes: {duplicates}")
        rule_ids = [r.id for r in self.combination_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("duplicate combination rule ids")
        cited = set(self.review_policy.cite)
        for analyte in self.analytes:
            cited |= analyte.citations()
        for rule in self.combination_rules:
            cited |= set(rule.cite)
            unknown = rule.analytes() - set(codes)
            if unknown:
                raise ValueError(f"rule {rule.id} references unknown analytes {sorted(unknown)}")
        dangling = sorted(cited - set(self.sources))
        if dangling:
            raise ValueError(f"citations without a source entry: {dangling}")
        return self

    @property
    def digest(self) -> str:
        return content_digest(canonical_json(self.model_dump(mode="json")))

    def analyte(self, code: str) -> AnalyteSpec | None:
        return next((a for a in self.analytes if a.code == code), None)

    def rules_for(self, code: str) -> tuple[CombinationRule, ...]:
        return tuple(r for r in self.combination_rules if code in r.analytes())

    def summary(self) -> dict[str, Any]:
        return {
            "catalogue_id": self.governance.catalogue_id,
            "version": self.governance.version,
            "status": self.governance.status,
            "approved_by": self.governance.approved_by,
            "approved_on": (
                self.governance.approved_on.isoformat() if self.governance.approved_on else None
            ),
            "statement": self.governance.statement,
            "digest": self.digest,
            "analytes": [{"code": a.code, "name": a.name, "unit": a.unit} for a in self.analytes],
            "combination_rules": [r.id for r in self.combination_rules],
            "sources": dict(self.sources),
        }

    @classmethod
    def from_yaml(cls, text: str) -> AnalyteCatalogue:
        try:
            return cls.model_validate(yaml.safe_load(text))
        except (ValidationError, yaml.YAMLError) as exc:
            raise CatalogueError(f"invalid analyte catalogue: {exc}") from exc

    @classmethod
    def load(cls, path: Path, *, allow_starter: bool) -> AnalyteCatalogue:
        catalogue = cls.from_yaml(path.read_text(encoding="utf-8"))
        status = catalogue.governance.status
        if status == "retired":
            raise CatalogueError(f"catalogue {catalogue.governance.version} is retired")
        if status != "approved" and not allow_starter:
            raise CatalogueError(
                f"catalogue status is {status!r}; a laboratory-director approved catalogue is "
                "required (set governance.status: approved with approved_by/approved_on)"
            )
        return catalogue
