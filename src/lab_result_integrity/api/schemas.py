"""Request and response bodies of the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lab_result_integrity.application.service import CaseSummary
from lab_result_integrity.domain.models import Disposition, LabResult, ScreeningOutcome
from lab_result_integrity.domain.pbrtqc import PbrtqcState
from lab_result_integrity.domain.review import ReviewOutcome


class ScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[LabResult] = Field(min_length=1, max_length=5000)


class ScreenResponse(BaseModel):
    outcomes: list[ScreeningOutcome]
    held_for_review: int
    screened_no_findings: int

    @classmethod
    def of(cls, outcomes: list[ScreeningOutcome]) -> ScreenResponse:
        held = sum(1 for o in outcomes if o.disposition is Disposition.HELD_FOR_REVIEW)
        return cls(
            outcomes=outcomes, held_for_review=held, screened_no_findings=len(outcomes) - held
        )


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: ReviewOutcome
    comment: str | None = Field(default=None, max_length=4000)


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: ReviewOutcome
    rationale: str = Field(min_length=1, max_length=4000)


class QueueResponse(BaseModel):
    items: list[CaseSummary]
    limit: int
    offset: int


class PbrtqcResponse(BaseModel):
    items: list[PbrtqcState]
