"""Domain entities and value objects for result screening and pathologist review."""

from __future__ import annotations

import math
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$"


class Sex(StrEnum):
    """Administrative sex used to select reference intervals (FHIR AdministrativeGender)."""

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNKNOWN = "unknown"


class HilIndex(StrEnum):
    """Serum indices reported by the analyzer (CLSI C56)."""

    HEMOLYSIS = "hemolysis"
    ICTERUS = "icterus"
    LIPEMIA = "lipemia"


class Flag(StrEnum):
    """Abnormal flags in HL7 v2 OBX-8 vocabulary."""

    NORMAL = "N"
    LOW = "L"
    HIGH = "H"
    CRITICAL_LOW = "LL"
    CRITICAL_HIGH = "HH"


class FindingKind(StrEnum):
    NOT_IN_CATALOGUE = "not_in_catalogue"
    UNIT_MISMATCH = "unit_mismatch"
    REFERENCE_RANGE = "reference_range"
    CRITICAL_VALUE = "critical_value"
    PLAUSIBILITY_LIMIT = "plausibility_limit"
    DELTA_CHECK = "delta_check"
    HIL_INTERFERENCE = "hil_interference"
    IMPLAUSIBLE_COMBINATION = "implausible_combination"
    PBRTQC_ALARM = "pbrtqc_alarm"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


class Priority(StrEnum):
    """Review priority; ``rank`` orders the pathologist queue."""

    STAT = "stat"
    URGENT = "urgent"
    ROUTINE = "routine"

    @property
    def rank(self) -> int:
        return {Priority.STAT: 0, Priority.URGENT: 1, Priority.ROUTINE: 2}[self]


class Disposition(StrEnum):
    """What screening concluded. Neither value releases the result.

    ``screened_no_findings`` tells the LIS that no integrity rule fired; release
    stays with the laboratory's own validated autoverification rules or a
    human. ``held_for_review`` places the result in the pathologist queue.
    """

    SCREENED_NO_FINDINGS = "screened_no_findings"
    HELD_FOR_REVIEW = "held_for_review"


class SerumIndices(BaseModel):
    """HIL indices as reported by the analyzer, in the analyzer's index units."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hemolysis: float | None = Field(default=None, ge=0, le=100_000)
    icterus: float | None = Field(default=None, ge=0, le=100_000)
    lipemia: float | None = Field(default=None, ge=0, le=100_000)

    def value(self, index: HilIndex) -> float | None:
        result: float | None = getattr(self, index.value)
        return result


class Patient(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    patient_id: str = Field(pattern=IDENTIFIER, description="Pseudonymous patient key")
    sex: Sex = Sex.UNKNOWN
    birth_date: date

    def age_days(self, at: datetime) -> int:
        return (at.date() - self.birth_date).days


class LabResult(BaseModel):
    """One numeric result as delivered by the LIS or instrument middleware."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: str = Field(pattern=IDENTIFIER)
    accession: str = Field(pattern=IDENTIFIER)
    specimen_id: str = Field(pattern=IDENTIFIER)
    patient: Patient
    analyte_code: str = Field(pattern=r"^[0-9]{1,7}-[0-9]$", description="LOINC code")
    value: float
    unit: str = Field(min_length=1, max_length=32)
    collected_at: datetime
    resulted_at: datetime
    instrument_id: str = Field(pattern=IDENTIFIER)
    indices: SerumIndices = Field(default_factory=SerumIndices)

    @field_validator("value")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be a finite number")
        return value

    @field_validator("collected_at", "resulted_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must carry a UTC offset")
        return value

    @model_validator(mode="after")
    def _chronology(self) -> LabResult:
        if self.resulted_at < self.collected_at:
            raise ValueError("resulted_at precedes collected_at")
        if self.collected_at.date() < self.patient.birth_date:
            raise ValueError("collected_at precedes the patient's birth date")
        return self


class Finding(BaseModel):
    """A rule outcome with the evidence that produced it and the citation behind the rule."""

    model_config = ConfigDict(frozen=True)

    kind: FindingKind
    severity: Severity
    rule_id: str
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    citations: tuple[str, ...] = ()


class PriorResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    result_id: str
    value: float
    collected_at: datetime
    specimen_id: str


class TrendSummary(BaseModel):
    """Least-squares trend over the patient's results inside the trend window."""

    model_config = ConfigDict(frozen=True)

    points: int
    window_hours: float
    slope_per_day: float | None
    first_value: float | None
    last_value: float | None
    direction: str


class ScreeningOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    result_id: str
    analyte_code: str
    disposition: Disposition
    flag: Flag | None
    findings: tuple[Finding, ...]
    priority: Priority | None
    case_id: str | None
    released_by_agent: bool = Field(
        default=False, description="Always false: agents never release or autoverify results"
    )
    catalogue_version: str
    catalogue_digest: str
    screened_at: datetime


class StoredResult(BaseModel):
    """A result together with its screening outcome and review state."""

    model_config = ConfigDict(frozen=True)

    result: LabResult
    content_digest: str
    outcome: ScreeningOutcome | None = None
    review_outcome: str | None = None
