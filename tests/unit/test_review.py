"""Review case state machine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from lab_result_integrity.domain.errors import ConflictError, InvalidStateError
from lab_result_integrity.domain.models import Priority
from lab_result_integrity.domain.review import (
    CaseStatus,
    Decision,
    PolicyRecord,
    Proposal,
    ReviewCase,
    ReviewOutcome,
)
from tests.conftest import NOW

POLICY = PolicyRecord(
    decision="allow", matched_rules=("r",), policy_version="1", policy_digest="d", source="local"
)


def case() -> ReviewCase:
    return ReviewCase(
        case_id="c" * 32,
        result_id="R-1",
        patient_id="P-1",
        analyte_code="2823-3",
        priority=Priority.URGENT,
        created_at=NOW,
        due_at=NOW + timedelta(hours=2),
        findings=(),
        evidence={},
        evidence_digest="sha256:x",
    )


def decision(
    by: str, outcome: ReviewOutcome = ReviewOutcome.CONFIRM, comment: str | None = None
) -> Decision:
    return Decision(outcome=outcome, comment=comment, decided_by=by, decided_at=NOW, policy=POLICY)


def test_claim_then_decide() -> None:
    claimed = case().claim("dr.a")
    assert claimed.status is CaseStatus.CLAIMED
    assert claimed.claim("dr.a").assigned_to == "dr.a"
    decided = claimed.decide(decision("dr.a"))
    assert decided.status is CaseStatus.DECIDED
    assert not decided.overdue(NOW + timedelta(days=1))


def test_claimed_case_is_protected_unless_override() -> None:
    claimed = case().claim("dr.a")
    with pytest.raises(ConflictError):
        claimed.claim("dr.b")
    with pytest.raises(ConflictError):
        claimed.decide(decision("dr.b"))
    assert claimed.claim("dr.b", override=True).assigned_to == "dr.b"
    assert claimed.decide(decision("dr.b"), override=True).decision is not None


def test_decided_case_is_final() -> None:
    decided = case().decide(decision("dr.a"))
    proposal = Proposal(
        proposed_by="agent",
        outcome=ReviewOutcome.CONFIRM,
        rationale="r",
        proposed_at=NOW,
        policy=POLICY,
    )
    actions: list[Callable[[], object]] = [
        lambda: decided.claim("dr.a"),
        lambda: decided.decide(decision("dr.a")),
        lambda: decided.propose(proposal),
    ]
    for action in actions:
        with pytest.raises(InvalidStateError):
            action()


def test_non_confirm_outcomes_need_comment() -> None:
    with pytest.raises(InvalidStateError, match="requires a comment"):
        case().decide(decision("dr.a", ReviewOutcome.REQUEST_RECOLLECTION, "  "))
    done = case().decide(decision("dr.a", ReviewOutcome.REQUEST_RECOLLECTION, "hemolysed; redraw"))
    assert done.decision is not None
    assert done.decision.comment == "hemolysed; redraw"


def test_overdue_and_escalation() -> None:
    c = case()
    assert not c.overdue(NOW)
    assert c.overdue(NOW + timedelta(hours=3))
    assert c.escalate().escalated
