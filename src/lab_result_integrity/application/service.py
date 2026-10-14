"""Application service: the single enforcement point for REST, MCP and A2A callers.

Every method takes the authenticated ``Principal`` and authorizes the action
before touching data, so a new channel cannot bypass policy by calling the
pipeline or repositories directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from agentic_core.audit import AuditEvent, AuditLog, AuditSink, verify_chain
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.domain.errors import AuditIntegrityError
from agentic_core.observability import correlation_id
from agentic_core.policy import Resource
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict

from lab_result_integrity.agents.pipeline import ScreeningPipeline
from lab_result_integrity.domain.errors import NotFoundError
from lab_result_integrity.domain.models import (
    LabResult,
    Priority,
    PriorResult,
    ScreeningOutcome,
    StoredResult,
    TrendSummary,
)
from lab_result_integrity.domain.pbrtqc import PbrtqcState
from lab_result_integrity.domain.ports import CaseRepository, Clock, PbrtqcStore, ResultRepository
from lab_result_integrity.domain.review import (
    CaseStatus,
    Proposal,
    ReviewCase,
    ReviewOutcome,
)
from lab_result_integrity.domain.review import (
    Decision as ReviewDecision,
)
from lab_result_integrity.domain.rules import trend_of
from lab_result_integrity.observability.metrics import IntegrityMetrics
from lab_result_integrity.security.auth import Authorizer, AuthzOutcome, Principal

PURPOSE = {"purpose_of_use": "laboratory-quality-assurance"}


class CaseSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    result_id: str
    analyte_code: str
    priority: Priority
    status: CaseStatus
    created_at: str
    due_at: str
    overdue: bool
    escalated: bool
    assigned_to: str | None
    finding_kinds: tuple[str, ...]
    pending_proposals: int

    @classmethod
    def of(cls, case: ReviewCase, *, overdue: bool) -> CaseSummary:
        return cls(
            case_id=case.case_id,
            result_id=case.result_id,
            analyte_code=case.analyte_code,
            priority=case.priority,
            status=case.status,
            created_at=case.created_at.isoformat(),
            due_at=case.due_at.isoformat(),
            overdue=overdue,
            escalated=case.escalated,
            assigned_to=case.assigned_to,
            finding_kinds=tuple(sorted({f.kind.value for f in case.findings})),
            pending_proposals=sum(1 for p in case.proposals if p.status.startswith("pending")),
        )


class PatientTrend(BaseModel):
    model_config = ConfigDict(frozen=True)

    patient_id: str
    analyte_code: str
    unit: str
    results: tuple[PriorResult, ...]
    trend: TrendSummary


class AuditVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    verified: bool
    events: int
    failed_sequence: int | None = None
    reason: str | None = None


class IntegrityService:
    def __init__(
        self,
        *,
        pipeline: ScreeningPipeline,
        results: ResultRepository,
        cases: CaseRepository,
        pbrtqc: PbrtqcStore,
        authorizer: Authorizer,
        audit: AuditLog,
        audit_sink: AuditSink,
        audit_keys: dict[str, Ed25519PublicKey],
        clock: Clock,
        metrics: IntegrityMetrics,
    ) -> None:
        self.pipeline = pipeline
        self._results = results
        self._cases = cases
        self._pbrtqc = pbrtqc
        self._authz = authorizer
        self._audit = audit
        self._audit_sink = audit_sink
        self._audit_keys = audit_keys
        self._clock = clock
        self._metrics = metrics

    def now(self) -> datetime:
        return self._clock.now()

    # ----------------------------------------------------------- authorization

    async def _authorize(
        self, who: Principal, action: str, resource: Resource, *, allow_only: bool = True
    ) -> AuthzOutcome:
        try:
            if allow_only:
                outcome = await self._authz.require_allow(
                    who, action, resource, environment=PURPOSE
                )
            else:
                outcome = await self._authz.require(who, action, resource, environment=PURPOSE)
        except Exception:
            self._metrics.authz_decisions.labels(action, "deny").inc()
            raise
        self._metrics.authz_decisions.labels(action, outcome.decision.value).inc()
        return outcome

    @staticmethod
    def _restricted(kind: str, ident: str) -> Resource:
        return Resource(type=kind, id=ident, classification=DataClassification.RESTRICTED)

    # --------------------------------------------------------------- screening

    async def screen(self, who: Principal, batch: Sequence[LabResult]) -> list[ScreeningOutcome]:
        await self._authorize(who, "results:screen", self._restricted("lab-result-batch", "*"))
        return await self.pipeline.screen(batch, submitted_by=who.subject)

    async def result(self, who: Principal, result_id: str) -> StoredResult:
        await self._authorize(who, "results:read", self._restricted("lab-result", result_id))
        stored = await self._results.get(result_id)
        if stored is None:
            raise NotFoundError(f"result {result_id} not found")
        return stored

    async def patient_trend(
        self, who: Principal, patient_id: str, analyte_code: str
    ) -> PatientTrend:
        await self._authorize(who, "results:read", self._restricted("patient", patient_id))
        spec = self.pipeline.catalogue.analyte(analyte_code)
        if spec is None:
            raise NotFoundError(f"analyte {analyte_code} is not in the catalogue")
        now = self._clock.now()
        points = await self._results.history(
            patient_id,
            analyte_code,
            before=now + timedelta(seconds=1),
            since=now - timedelta(hours=spec.trend_window_hours),
            exclude_specimen="",
            limit=500,
        )
        ordered = tuple(sorted(points, key=lambda p: p.collected_at))
        return PatientTrend(
            patient_id=patient_id,
            analyte_code=analyte_code,
            unit=spec.unit,
            results=ordered,
            trend=trend_of([(p.collected_at, p.value) for p in ordered], spec.trend_window_hours),
        )

    # ------------------------------------------------------------ review queue

    async def queue(
        self,
        who: Principal,
        *,
        statuses: Sequence[CaseStatus] = (CaseStatus.OPEN, CaseStatus.CLAIMED),
        limit: int = 50,
        offset: int = 0,
    ) -> list[CaseSummary]:
        await self._authorize(who, "cases:read", self._restricted("review-queue", "*"))
        now = self._clock.now()
        found = await self._cases.queue(statuses=statuses, limit=limit, offset=offset)
        return [CaseSummary.of(c, overdue=c.overdue(now)) for c in found]

    async def _load(self, case_id: str) -> ReviewCase:
        case = await self._cases.get(case_id)
        if case is None:
            raise NotFoundError(f"case {case_id} not found")
        return case

    async def case(self, who: Principal, case_id: str) -> ReviewCase:
        await self._authorize(who, "cases:read", self._restricted("review-case", case_id))
        return await self._load(case_id)

    async def _may_override(self, who: Principal, case: ReviewCase) -> bool:
        if case.status is not CaseStatus.CLAIMED or case.assigned_to == who.subject:
            return False
        outcome = await self._authz.decide(
            who, "review:override", self._restricted("review-case", case.case_id)
        )
        return outcome.decision is Decision.ALLOW

    async def claim(self, who: Principal, case_id: str) -> ReviewCase:
        await self._authorize(who, "review:claim", self._restricted("review-case", case_id))
        case = await self._load(case_id)
        claimed = case.claim(who.subject, override=await self._may_override(who, case))
        saved = await self._cases.update(claimed)
        await self._audit.record(
            actor=who.subject,
            action="case.claimed",
            resource=f"case:{case_id}",
            outcome="claimed",
            correlation_id=correlation_id(),
            details={"previous_assignee": case.assigned_to},
        )
        return saved

    async def decide(
        self, who: Principal, case_id: str, outcome: ReviewOutcome, comment: str | None
    ) -> ReviewCase:
        policy = await self._authorize(
            who, "review:decide", self._restricted("review-case", case_id)
        )
        case = await self._load(case_id)
        decision = ReviewDecision(
            outcome=outcome,
            comment=comment.strip() if comment else None,
            decided_by=who.subject,
            decided_at=self._clock.now(),
            policy=policy.record(),
        )
        decided = case.decide(decision, override=await self._may_override(who, case))
        saved = await self._cases.update(decided)
        await self._results.record_review(case.result_id, outcome.value)
        self._metrics.review_decisions.labels(outcome.value).inc()
        await self._audit.record(
            actor=who.subject,
            action="case.decided",
            resource=f"case:{case_id}",
            outcome=outcome.value,
            correlation_id=correlation_id(),
            details={
                "result_id": case.result_id,
                "evidence_digest": case.evidence_digest,
                "policy_rules": list(policy.matched_rules),
                "policy_digest": policy.policy_digest,
            },
        )
        return saved

    async def propose(
        self, who: Principal, case_id: str, outcome: ReviewOutcome, rationale: str
    ) -> ReviewCase:
        """Agents may suggest a disposition; it is stored for a pathologist to accept or not."""
        policy = await self._authorize(
            who, "review:propose", self._restricted("review-case", case_id), allow_only=False
        )
        case = await self._load(case_id)
        proposal = Proposal(
            proposed_by=who.subject,
            outcome=outcome,
            rationale=rationale.strip(),
            proposed_at=self._clock.now(),
            policy=policy.record(),
        )
        saved = await self._cases.update(case.propose(proposal))
        self._metrics.proposals.labels(outcome.value).inc()
        await self._audit.record(
            actor=who.subject,
            action="case.proposal",
            resource=f"case:{case_id}",
            outcome=policy.decision.value,
            correlation_id=correlation_id(),
            details={"proposed_outcome": outcome.value, "policy_rules": list(policy.matched_rules)},
        )
        return saved

    async def escalate_overdue(self) -> int:
        """Mark undecided cases past their SLA as escalated; returns how many are overdue."""
        now = self._clock.now()
        overdue = await self._cases.overdue(now, limit=1000)
        for case in overdue:
            if case.escalated:
                continue
            await self._cases.update(case.escalate())
            self._metrics.escalations.inc()
            await self._audit.record(
                actor="lab-result-integrity/review-sla-monitor",
                action="case.escalated",
                resource=f"case:{case.case_id}",
                outcome="overdue",
                details={"due_at": case.due_at.isoformat(), "priority": case.priority.value},
            )
        self._metrics.cases_overdue.set(len(overdue))
        return len(overdue)

    async def record_channel_call(
        self, who: Principal, channel: str, operation: str, outcome: str
    ) -> None:
        """Audit an MCP tool call or A2A skill invocation (authorization is audited separately)."""
        await self._audit.record(
            actor=who.subject,
            action=f"{channel}.{operation}",
            resource=f"{channel}:{operation}",
            outcome=outcome,
            correlation_id=correlation_id(),
            details={"principal_kind": who.kind.value, "via": who.via},
        )

    # ------------------------------------------------------------ QC and audit

    async def pbrtqc_status(self, who: Principal) -> list[PbrtqcState]:
        await self._authorize(
            who, "qc:read", Resource(type="pbrtqc", classification=DataClassification.INTERNAL)
        )
        return list(await self._pbrtqc.all())

    async def catalogue(self, who: Principal) -> dict[str, object]:
        await self._authorize(
            who,
            "catalogue:read",
            Resource(type="analyte-catalogue", classification=DataClassification.INTERNAL),
        )
        return self.pipeline.catalogue.summary()

    async def verify_audit(self, who: Principal) -> AuditVerification:
        await self._authorize(
            who,
            "audit:verify",
            Resource(type="audit-chain", classification=DataClassification.CONFIDENTIAL),
        )
        events: list[AuditEvent] = [event async for event in self._audit_sink.read()]
        try:
            count = verify_chain(events, self._audit_keys)
        except AuditIntegrityError as exc:
            return AuditVerification(
                verified=False, events=len(events), failed_sequence=exc.sequence, reason=str(exc)
            )
        return AuditVerification(verified=True, events=count)
