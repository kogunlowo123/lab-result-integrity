"""Screening pipeline: Results -> Trend Analysis -> Interference Detection ->
Patient-based QC -> Evidence Collection -> Pathologist Review queue.

The pipeline never releases, verifies or autoverifies a result. Its only two
outcomes are ``screened_no_findings`` (no integrity rule fired; release stays
with the LIS autoverification rules or a human) and ``held_for_review`` (a
review case with an evidence packet is queued for a pathologist).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from agentic_core.audit import AuditLog
from agentic_core.domain.identifiers import new_id
from agentic_core.domain.provenance import canonical_json, content_digest
from agentic_core.observability import correlation_id

from lab_result_integrity.agents.analysis import (
    AGENT_PREFIX,
    InterferenceDetectionAgent,
    PatientBasedQcAgent,
    TrendAnalysisAgent,
)
from lab_result_integrity.agents.evidence import EvidenceCollectionAgent
from lab_result_integrity.domain.catalogue import AnalyteCatalogue
from lab_result_integrity.domain.errors import ConflictError
from lab_result_integrity.domain.models import (
    Disposition,
    Finding,
    FindingKind,
    Flag,
    LabResult,
    Priority,
    PriorResult,
    ScreeningOutcome,
    Severity,
    TrendSummary,
)
from lab_result_integrity.domain.ports import CaseRepository, Clock, ResultRepository
from lab_result_integrity.domain.review import ReviewCase
from lab_result_integrity.domain.rules import evaluate_ranges, unit_mismatch
from lab_result_integrity.observability.metrics import IntegrityMetrics

HISTORY_LIMIT = 500


def result_digest(result: LabResult) -> str:
    return content_digest(canonical_json(result.model_dump(mode="json")))


def priority_for(findings: Sequence[Finding]) -> Priority | None:
    """Hold on any finding of warning severity or above; INFO findings never hold."""
    worst = max((f.severity.rank for f in findings), default=-1)
    if worst >= Severity.CRITICAL.rank:
        return Priority.STAT
    if worst >= Severity.HIGH.rank:
        return Priority.URGENT
    if worst >= Severity.WARNING.rank:
        return Priority.ROUTINE
    return None


class ScreeningPipeline:
    name = f"{AGENT_PREFIX}/screening-orchestrator"

    def __init__(
        self,
        *,
        catalogue: AnalyteCatalogue,
        results: ResultRepository,
        cases: CaseRepository,
        trend: TrendAnalysisAgent,
        interference: InterferenceDetectionAgent,
        qc: PatientBasedQcAgent,
        evidence: EvidenceCollectionAgent,
        audit: AuditLog,
        clock: Clock,
        metrics: IntegrityMetrics,
    ) -> None:
        self._catalogue = catalogue
        self._results = results
        self._cases = cases
        self._trend = trend
        self._interference = interference
        self._qc = qc
        self._evidence = evidence
        self._audit = audit
        self._clock = clock
        self._metrics = metrics

    @property
    def catalogue(self) -> AnalyteCatalogue:
        return self._catalogue

    async def screen(
        self, batch: Sequence[LabResult], *, submitted_by: str
    ) -> list[ScreeningOutcome]:
        ids = [r.result_id for r in batch]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ConflictError(f"duplicate result ids in batch: {duplicates}")

        done: dict[str, ScreeningOutcome] = {}
        pending: list[LabResult] = []
        fresh: list[tuple[LabResult, str]] = []
        conflicts: list[str] = []
        existing = await self._results.get_many(ids)
        for result in batch:
            digest = result_digest(result)
            stored = existing.get(result.result_id)
            if stored is None:
                fresh.append((result, digest))
                pending.append(result)
            elif stored.content_digest != digest:
                conflicts.append(result.result_id)
            elif stored.outcome is not None:
                done[result.result_id] = stored.outcome  # idempotent replay
            else:
                pending.append(result)  # resume an interrupted screening
        if conflicts:
            raise ConflictError(
                f"results already submitted with different content: {sorted(conflicts)}; "
                "submit corrections under a new result_id"
            )
        await self._results.add_many(fresh)

        await self._audit.record(
            actor=submitted_by,
            action="results.submitted",
            resource=f"batch:{len(batch)}",
            outcome="accepted",
            correlation_id=correlation_id(),
            details={"result_ids": ids, "new": [r.result_id for r in pending]},
        )
        for result in sorted(pending, key=lambda r: (r.collected_at, ids.index(r.result_id))):
            done[result.result_id] = await self._screen_one(result, submitted_by)
        return [done[i] for i in ids]

    async def _screen_one(self, result: LabResult, submitted_by: str) -> ScreeningOutcome:
        now = self._clock.now()
        spec = self._catalogue.analyte(result.analyte_code)
        findings: list[Finding] = []
        flag: Flag | None = None
        history: Sequence[PriorResult] = ()
        trend_summary: TrendSummary | None = None
        qc_snapshot = None
        specimen = await self._results.specimen(result.specimen_id)

        if spec is None:
            findings.append(
                Finding(
                    kind=FindingKind.NOT_IN_CATALOGUE,
                    severity=Severity.HIGH,
                    rule_id="catalogue:unknown-analyte",
                    message=(
                        f"analyte {result.analyte_code} is not in catalogue "
                        f"{self._catalogue.governance.version}; no integrity rule can be applied"
                    ),
                    evidence={"analyte_code": result.analyte_code},
                )
            )
        elif (mismatch := unit_mismatch(result, spec)) is not None:
            findings.append(mismatch)
        else:
            flag, range_findings = evaluate_ranges(result, spec)
            findings.extend(range_findings)
            lookback = max(spec.delta.window_hours if spec.delta else 0.0, spec.trend_window_hours)
            history = await self._results.history(
                result.patient.patient_id,
                spec.code,
                before=result.collected_at,
                since=result.collected_at - timedelta(hours=lookback),
                exclude_specimen=result.specimen_id,
                limit=HISTORY_LIMIT,
            )
            delta_findings, trend_summary = self._trend.analyze(result, spec, history)
            findings.extend(delta_findings)
            findings.extend(self._interference.analyze(result, spec, specimen))
            observation = await self._qc.observe(result, spec)
            qc_snapshot = observation.snapshot
            if observation.alarm_started:
                self._metrics.pbrtqc_alarms.labels(spec.code).inc()
            if observation.finding is not None:
                findings.append(observation.finding)

        priority = priority_for(findings)
        case_id: str | None = None
        existing = await self._cases.for_result(result.result_id) if priority else None
        if existing is not None:
            case_id = existing.case_id  # resumed screening: the case was already opened
        elif priority is not None:
            packet = await self._evidence.collect(
                result=result,
                spec=spec,
                flag=flag,
                findings=findings,
                history=history,
                trend=trend_summary,
                specimen=specimen,
                qc_snapshot=qc_snapshot,
                now=now,
            )
            self._metrics.narratives.labels(packet.narrative.status).inc()
            case = ReviewCase(
                case_id=new_id(),
                result_id=result.result_id,
                patient_id=result.patient.patient_id,
                analyte_code=result.analyte_code,
                priority=priority,
                created_at=now,
                due_at=now + timedelta(minutes=self._catalogue.review_policy.sla_minutes[priority]),
                findings=tuple(findings),
                evidence=packet.model_dump(mode="json"),
                evidence_digest=packet.digest(),
            )
            await self._cases.add(case)
            case_id = case.case_id
            self._metrics.cases_opened.labels(priority.value).inc()
            await self._audit.record(
                actor=self._evidence.name,
                action="case.opened",
                resource=f"case:{case.case_id}",
                outcome=priority.value,
                correlation_id=correlation_id(),
                details={
                    "result_id": result.result_id,
                    "evidence_digest": case.evidence_digest,
                    "due_at": case.due_at.isoformat(),
                    "narrative": packet.narrative.status,
                },
            )

        outcome = ScreeningOutcome(
            result_id=result.result_id,
            analyte_code=result.analyte_code,
            disposition=(
                Disposition.SCREENED_NO_FINDINGS
                if priority is None
                else Disposition.HELD_FOR_REVIEW
            ),
            flag=flag,
            findings=tuple(findings),
            priority=priority,
            case_id=case_id,
            catalogue_version=self._catalogue.governance.version,
            catalogue_digest=self._catalogue.digest,
            screened_at=now,
        )
        await self._results.record_outcome(outcome)
        self._metrics.results_screened.labels(outcome.disposition.value).inc()
        for finding in findings:
            self._metrics.findings.labels(finding.kind.value, finding.severity.value).inc()
        await self._audit.record(
            actor=self.name,
            action="result.screened",
            resource=f"result:{result.result_id}",
            outcome=outcome.disposition.value,
            correlation_id=correlation_id(),
            details={
                "submitted_by": submitted_by,
                "rules": [f.rule_id for f in findings],
                "case_id": case_id,
                "catalogue_digest": outcome.catalogue_digest,
                "released_by_agent": False,
            },
        )
        return outcome
