"""Pathologist review cases: lifecycle, claims, agent proposals and human decisions.

State machine::

    open ──claim──▶ claimed ──decide──▶ decided
      └──────────────decide────────────▶ decided

Only a human principal holding a reviewer role may claim or decide. Agents may
attach proposals, which never change the case state.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lab_result_integrity.domain.errors import ConflictError, InvalidStateError
from lab_result_integrity.domain.models import Finding, Priority


class CaseStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    DECIDED = "decided"


class ReviewOutcome(StrEnum):
    CONFIRM = "confirm"
    CONFIRM_WITH_COMMENT = "confirm_with_comment"
    REQUEST_RECOLLECTION = "request_recollection"
    CANCEL_RESULT = "cancel_result"

    @property
    def requires_comment(self) -> bool:
        return self is not ReviewOutcome.CONFIRM


class PolicyRecord(BaseModel):
    """The policy decision that permitted a state change, kept with the record."""

    model_config = ConfigDict(frozen=True)

    decision: str
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    source: str


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposed_by: str
    outcome: ReviewOutcome
    rationale: str
    proposed_at: datetime
    status: str = "pending_human_approval"
    policy: PolicyRecord


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: ReviewOutcome
    comment: str | None
    decided_by: str
    decided_at: datetime
    policy: PolicyRecord


class ReviewCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    result_id: str
    patient_id: str
    analyte_code: str
    priority: Priority
    status: CaseStatus = CaseStatus.OPEN
    created_at: datetime
    due_at: datetime
    escalated: bool = False
    assigned_to: str | None = None
    findings: tuple[Finding, ...]
    evidence: dict[str, Any] = Field(description="Evidence packet assembled at screening")
    evidence_digest: str
    proposals: tuple[Proposal, ...] = ()
    decision: Decision | None = None
    version: int = 0

    def overdue(self, now: datetime) -> bool:
        return self.status is not CaseStatus.DECIDED and now > self.due_at

    def claim(self, reviewer: str, *, override: bool = False) -> ReviewCase:
        if self.status is CaseStatus.DECIDED:
            raise InvalidStateError(f"case {self.case_id} is already decided")
        if self.status is CaseStatus.CLAIMED and self.assigned_to != reviewer and not override:
            raise ConflictError(f"case {self.case_id} is claimed by another reviewer")
        return self.model_copy(update={"status": CaseStatus.CLAIMED, "assigned_to": reviewer})

    def propose(self, proposal: Proposal) -> ReviewCase:
        if self.status is CaseStatus.DECIDED:
            raise InvalidStateError(f"case {self.case_id} is already decided")
        return self.model_copy(update={"proposals": (*self.proposals, proposal)})

    def decide(self, decision: Decision, *, override: bool = False) -> ReviewCase:
        if self.status is CaseStatus.DECIDED:
            raise InvalidStateError(f"case {self.case_id} is already decided")
        if (
            self.status is CaseStatus.CLAIMED
            and self.assigned_to != decision.decided_by
            and not override
        ):
            raise ConflictError(f"case {self.case_id} is claimed by another reviewer")
        if decision.outcome.requires_comment and not (decision.comment or "").strip():
            raise InvalidStateError(f"outcome {decision.outcome.value} requires a comment")
        return self.model_copy(
            update={
                "status": CaseStatus.DECIDED,
                "assigned_to": decision.decided_by,
                "decision": decision,
            }
        )

    def escalate(self) -> ReviewCase:
        return self.model_copy(update={"escalated": True})
