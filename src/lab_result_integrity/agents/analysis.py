"""Trend analysis, interference detection and patient-based QC agents.

Each agent owns one stage of the integrity flow and is deterministic: given
the same inputs and catalogue it produces the same findings, which is what
makes the evidence packet replayable during an inspection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agentic_core.audit import AuditLog
from agentic_core.observability import correlation_id

from lab_result_integrity.domain.catalogue import AnalyteCatalogue, AnalyteSpec
from lab_result_integrity.domain.errors import ConflictError
from lab_result_integrity.domain.models import Finding, LabResult, PriorResult, TrendSummary
from lab_result_integrity.domain.pbrtqc import PbrtqcState, alarm_finding, update
from lab_result_integrity.domain.ports import PbrtqcStore
from lab_result_integrity.domain.rules import (
    combination_findings,
    delta_check,
    hil_interference,
    trend,
)

AGENT_PREFIX = "lab-result-integrity"


class TrendAnalysisAgent:
    """Delta checks against the patient's previous result and a trend over the window."""

    name = f"{AGENT_PREFIX}/trend-analysis"

    def analyze(
        self, result: LabResult, spec: AnalyteSpec, history: Sequence[PriorResult]
    ) -> tuple[list[Finding], TrendSummary]:
        return delta_check(result, spec, history), trend(result, spec, history)


class InterferenceDetectionAgent:
    """HIL index interference and physiologically implausible combinations."""

    name = f"{AGENT_PREFIX}/interference-detection"

    def __init__(self, catalogue: AnalyteCatalogue) -> None:
        self._catalogue = catalogue

    def analyze(
        self, result: LabResult, spec: AnalyteSpec, specimen: Sequence[LabResult]
    ) -> list[Finding]:
        values: dict[str, float] = {}
        for other in specimen:
            other_spec = self._catalogue.analyte(other.analyte_code)
            # Only results in catalogue units take part in combination rules.
            if other_spec is not None and other.unit.strip().lower() == other_spec.unit.lower():
                values[other.analyte_code] = other.value
        values[result.analyte_code] = result.value
        return [
            *hil_interference(result, spec),
            *combination_findings(result, self._catalogue.rules_for(spec.code), values),
        ]


@dataclass(frozen=True)
class QcObservation:
    finding: Finding | None = None
    snapshot: dict[str, Any] | None = None
    alarm_started: bool = False


class PatientBasedQcAgent:
    """Moving-average QC per instrument and analyte, shared across replicas via the store."""

    name = f"{AGENT_PREFIX}/patient-based-qc"

    def __init__(self, store: PbrtqcStore, audit: AuditLog, *, max_attempts: int = 8) -> None:
        self._store = store
        self._audit = audit
        self._max_attempts = max_attempts

    async def observe(self, result: LabResult, spec: AnalyteSpec) -> QcObservation:
        config = spec.pbrtqc
        if config is None:
            return QcObservation()
        for _ in range(self._max_attempts):
            state = await self._store.get(result.instrument_id, spec.code) or PbrtqcState.fresh(
                result.instrument_id, spec.code, config
            )
            outcome = update(
                state, config, result_id=result.result_id, value=result.value, at=result.resulted_at
            )
            try:
                saved = await self._store.save(outcome.state)
            except ConflictError:
                continue
            break
        else:
            raise ConflictError("patient-based QC state is under contention; retry the batch")

        if outcome.alarm_started or outcome.recovered:
            await self._audit.record(
                actor=self.name,
                action="pbrtqc.alarm" if outcome.alarm_started else "pbrtqc.recovered",
                resource=f"instrument:{result.instrument_id}/analyte:{spec.code}",
                outcome="out_of_control" if outcome.alarm_started else "in_control",
                correlation_id=correlation_id(),
                details={
                    "statistic": saved.statistic,
                    "target": config.target,
                    "control_limit": config.control_limit,
                    "affected_result_ids": list(saved.affected_result_ids),
                },
            )
        snapshot = {
            "instrument_id": saved.instrument_id,
            "method": config.method,
            "statistic": saved.statistic,
            "target": config.target,
            "control_limit": config.control_limit,
            "in_control": saved.in_control,
            "accepted": saved.accepted,
            "excluded": saved.excluded,
            "result_included": outcome.included,
        }
        return QcObservation(
            finding=alarm_finding(outcome, config, spec.name) if outcome.out_of_control else None,
            snapshot=snapshot,
            alarm_started=outcome.alarm_started,
        )
