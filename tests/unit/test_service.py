"""Screening pipeline and review desk through the application service (SQLite)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lab_result_integrity.application.service import IntegrityService
from lab_result_integrity.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
)
from lab_result_integrity.domain.models import Disposition, FindingKind, Flag, Priority
from lab_result_integrity.domain.review import CaseStatus, ReviewOutcome
from tests.conftest import (
    AUDITOR,
    CALCIUM,
    DIRECTOR,
    GLUCOSE,
    NOW,
    OTHER_PATHOLOGIST,
    PATHOLOGIST,
    POTASSIUM,
    SCREENER,
    TECH,
    FakeClock,
    agent,
    make_result,
)


async def test_normal_result_is_screened_not_released(service: IntegrityService) -> None:
    [outcome] = await service.screen(TECH, [make_result(POTASSIUM, 4.2)])
    assert outcome.disposition is Disposition.SCREENED_NO_FINDINGS
    assert outcome.flag is Flag.NORMAL
    assert outcome.case_id is None
    assert outcome.released_by_agent is False
    assert outcome.catalogue_digest.startswith("sha256:")


async def test_abnormal_but_clean_result_is_not_held(service: IntegrityService) -> None:
    [outcome] = await service.screen(TECH, [make_result(POTASSIUM, 5.4)])
    assert outcome.flag is Flag.HIGH
    assert outcome.disposition is Disposition.SCREENED_NO_FINDINGS
    assert [f.kind for f in outcome.findings] == [FindingKind.REFERENCE_RANGE]


async def test_hemolysed_potassium_is_held_with_evidence(service: IntegrityService) -> None:
    [outcome] = await service.screen(SCREENER, [make_result(POTASSIUM, 5.8, hemolysis=230)])
    assert outcome.disposition is Disposition.HELD_FOR_REVIEW
    assert outcome.priority is Priority.URGENT
    kinds = {f.kind for f in outcome.findings}
    assert {FindingKind.HIL_INTERFERENCE, FindingKind.IMPLAUSIBLE_COMBINATION} <= kinds
    assert outcome.case_id is not None
    case = await service.case(PATHOLOGIST, outcome.case_id)
    packet = case.evidence
    assert packet["provenance"]["digest"].startswith("sha256:")
    assert "clsi-c56" in packet["citations"]
    assert packet["narrative"]["status"] == "disabled"
    assert case.due_at == NOW + timedelta(minutes=120)


async def test_critical_value_is_stat(service: IntegrityService) -> None:
    [outcome] = await service.screen(TECH, [make_result(POTASSIUM, 6.9)])
    assert outcome.flag is Flag.CRITICAL_HIGH
    assert outcome.priority is Priority.STAT


async def test_delta_check_uses_stored_history(service: IntegrityService) -> None:
    earlier = make_result(POTASSIUM, 4.0, collected_at=NOW - timedelta(hours=20))
    later = make_result(POTASSIUM, 5.3, collected_at=NOW - timedelta(hours=1))
    await service.screen(TECH, [earlier])
    [outcome] = await service.screen(TECH, [later])
    delta = [f for f in outcome.findings if f.kind is FindingKind.DELTA_CHECK]
    assert delta[0].evidence["prior_result_id"] == earlier.result_id
    assert outcome.case_id is not None
    case = await service.case(PATHOLOGIST, outcome.case_id)
    assert case.evidence["prior_results"][0]["result_id"] == earlier.result_id


async def test_edta_rule_sees_whole_specimen_in_one_batch(service: IntegrityService) -> None:
    k = make_result(POTASSIUM, 7.4, specimen_id="S-EDTA")
    ca = make_result(CALCIUM, 4.9, specimen_id="S-EDTA")
    outcomes = await service.screen(TECH, [k, ca])
    for outcome in outcomes:
        assert "k2edta-contamination" in {f.rule_id for f in outcome.findings}
        assert outcome.priority is Priority.STAT


async def test_unknown_analyte_and_unit_mismatch_are_held(service: IntegrityService) -> None:
    unknown = make_result(POTASSIUM, 1.0).model_copy(update={"analyte_code": "1234-5"})
    wrong_unit = make_result(GLUCOSE, 5.4, unit="mmol/L")
    outcomes = await service.screen(TECH, [unknown, wrong_unit])
    assert outcomes[0].findings[0].kind is FindingKind.NOT_IN_CATALOGUE
    assert outcomes[1].findings[0].kind is FindingKind.UNIT_MISMATCH
    assert all(o.disposition is Disposition.HELD_FOR_REVIEW for o in outcomes)


async def test_resubmission_is_idempotent_and_changes_conflict(service: IntegrityService) -> None:
    result = make_result(POTASSIUM, 4.4)
    first = await service.screen(TECH, [result])
    assert await service.screen(TECH, [result]) == first
    with pytest.raises(ConflictError, match="different content"):
        await service.screen(TECH, [result.model_copy(update={"value": 4.9})])
    with pytest.raises(ConflictError, match="duplicate result ids"):
        await service.screen(TECH, [result, result])


async def test_pbrtqc_drift_holds_results(service: IntegrityService) -> None:
    batch = [
        make_result(
            POTASSIUM,
            4.9,
            patient_id=f"P-{i:04d}",
            collected_at=NOW - timedelta(minutes=60 - i),
            instrument_id="CHEM-07",
        )
        for i in range(25)
    ]
    outcomes = await service.screen(TECH, batch)
    held = [o for o in outcomes if any(f.kind is FindingKind.PBRTQC_ALARM for f in o.findings)]
    assert held
    assert outcomes[0].disposition is Disposition.SCREENED_NO_FINDINGS
    [state] = await service.pbrtqc_status(TECH)
    assert state.instrument_id == "CHEM-07"
    assert not state.in_control
    assert state.alarms == 1


async def test_review_flow_claim_decide(service: IntegrityService) -> None:
    [outcome] = await service.screen(TECH, [make_result(POTASSIUM, 6.8)])
    assert outcome.case_id is not None
    queue = await service.queue(PATHOLOGIST)
    assert [c.case_id for c in queue] == [outcome.case_id]
    claimed = await service.claim(PATHOLOGIST, outcome.case_id)
    assert claimed.assigned_to == PATHOLOGIST.subject
    with pytest.raises(ConflictError):
        await service.claim(OTHER_PATHOLOGIST, outcome.case_id)
    decided = await service.decide(
        PATHOLOGIST, outcome.case_id, ReviewOutcome.CONFIRM_WITH_COMMENT, "called to ward 4"
    )
    assert decided.status is CaseStatus.DECIDED
    assert decided.decision is not None
    assert decided.decision.policy.matched_rules == ("allow-pathologist-review",)
    stored = await service.result(TECH, outcome.result_id)
    assert stored.review_outcome == "confirm_with_comment"
    assert await service.queue(PATHOLOGIST) == []


async def test_director_override(service: IntegrityService) -> None:
    [outcome] = await service.screen(TECH, [make_result(POTASSIUM, 6.8)])
    assert outcome.case_id is not None
    await service.claim(PATHOLOGIST, outcome.case_id)
    taken = await service.claim(DIRECTOR, outcome.case_id)
    assert taken.assigned_to == DIRECTOR.subject


async def test_agents_can_only_propose(service: IntegrityService) -> None:
    [outcome] = await service.screen(SCREENER, [make_result(POTASSIUM, 6.8)])
    assert outcome.case_id is not None
    case = await service.propose(
        SCREENER, outcome.case_id, ReviewOutcome.REQUEST_RECOLLECTION, "hemolysis suspected"
    )
    assert case.status is CaseStatus.OPEN
    assert case.proposals[0].policy.decision == "require_approval"
    with pytest.raises(ForbiddenError):
        await service.decide(SCREENER, outcome.case_id, ReviewOutcome.CONFIRM, None)
    with pytest.raises(ForbiddenError):
        await service.claim(SCREENER, outcome.case_id)
    with pytest.raises(ForbiddenError):
        await service.propose(PATHOLOGIST, outcome.case_id, ReviewOutcome.CONFIRM, "x")


async def test_scopes_and_roles_are_enforced(service: IntegrityService) -> None:
    with pytest.raises(ForbiddenError):
        await service.screen(agent("cases:read"), [make_result()])
    with pytest.raises(ForbiddenError):
        await service.screen(AUDITOR, [make_result()])
    with pytest.raises(ForbiddenError):
        await service.decide(TECH, "a" * 32, ReviewOutcome.CONFIRM, None)
    with pytest.raises(ForbiddenError):
        await service.verify_audit(TECH)
    assert await service.screen(agent("results:*"), [make_result()])


async def test_not_found(service: IntegrityService) -> None:
    with pytest.raises(NotFoundError):
        await service.case(PATHOLOGIST, "f" * 32)
    with pytest.raises(NotFoundError):
        await service.result(PATHOLOGIST, "R-missing")
    with pytest.raises(NotFoundError):
        await service.patient_trend(PATHOLOGIST, "P-1", "9999-9")


async def test_decided_case_cannot_be_decided_again(service: IntegrityService) -> None:
    [outcome] = await service.screen(TECH, [make_result(POTASSIUM, 6.8)])
    assert outcome.case_id is not None
    await service.decide(PATHOLOGIST, outcome.case_id, ReviewOutcome.CONFIRM, None)
    with pytest.raises(InvalidStateError):
        await service.decide(DIRECTOR, outcome.case_id, ReviewOutcome.CANCEL_RESULT, "dup")


async def test_overdue_cases_escalate_once(service: IntegrityService, clock: FakeClock) -> None:
    await service.screen(TECH, [make_result(POTASSIUM, 6.8)])
    assert await service.escalate_overdue() == 0
    clock.advance(minutes=31)
    assert await service.escalate_overdue() == 1
    assert await service.escalate_overdue() == 1
    [summary] = await service.queue(PATHOLOGIST)
    assert summary.overdue
    assert summary.escalated


async def test_patient_trend(service: IntegrityService) -> None:
    for hours, value in ((50, 1.0), (26, 1.3), (2, 1.7)):
        await service.screen(
            TECH,
            [make_result("2160-0", value, collected_at=NOW - timedelta(hours=hours))],
        )
    trend = await service.patient_trend(PATHOLOGIST, "P-1001", "2160-0")
    assert [r.value for r in trend.results] == [1.0, 1.3, 1.7]
    assert trend.trend.direction == "rising"


async def test_catalogue_and_audit_chain(service: IntegrityService) -> None:
    summary = await service.catalogue(TECH)
    assert summary["status"] == "starter"
    await service.screen(TECH, [make_result(POTASSIUM, 6.8)])
    verification = await service.verify_audit(AUDITOR)
    assert verification.verified
    assert verification.events >= 4
