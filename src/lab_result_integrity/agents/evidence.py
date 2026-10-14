"""Evidence collection: the packet a pathologist reviews, and the optional draft narrative.

The packet is the authoritative record. It carries the result, the patient's
prior results, every finding with the citation behind its rule, the specimen's
other results, the patient-based QC snapshot, the catalogue version and digest,
and a provenance record. It is hashed so the review decision can be tied to
exactly the evidence that was shown.

A model-drafted narrative may be attached when a model gateway is configured.
It is advisory, built from de-identified content only (no patient, specimen or
accession identifiers, no birth date), and passes the agentic-core
ContentGuard on the way in and out. Any guard block or provider failure leaves
the narrative empty; screening never depends on the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from agentic_core.domain.classification import DataClassification, TrustLevel
from agentic_core.domain.errors import AgenticCoreError
from agentic_core.domain.provenance import ProvenanceRecord, canonical_json, content_digest
from agentic_core.models import Message, ModelRequest, Role
from agentic_core.models.router import ModelRouter
from agentic_core.security import ContentGuard
from pydantic import BaseModel, ConfigDict

from lab_result_integrity.agents.analysis import AGENT_PREFIX
from lab_result_integrity.domain.catalogue import AnalyteCatalogue, AnalyteSpec
from lab_result_integrity.domain.models import (
    Finding,
    Flag,
    LabResult,
    PriorResult,
    Severity,
    TrendSummary,
)

NARRATIVE_SYSTEM_PROMPT = (
    "You draft a short note for a pathologist reviewing a held laboratory result. "
    "Restate only the evidence provided: the result, the rules that fired and their "
    "evidence, the trend and the quality-control status. Do not add clinical facts, "
    "diagnoses or treatment advice. Never state or imply that the result may be "
    "released or verified; that decision belongs to the pathologist. At most 120 words."
)


class Narrative(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["drafted", "blocked", "unavailable", "disabled"]
    text: str | None = None
    provider: str | None = None
    model: str | None = None
    notes: tuple[str, ...] = ()
    advisory: bool = True


class NarrativeDrafter:
    def __init__(self, router: ModelRouter, guard: ContentGuard, *, max_tokens: int = 400) -> None:
        self._router = router
        self._guard = guard
        self._max_tokens = max_tokens

    async def draft(self, facts: str) -> Narrative:
        request = ModelRequest(
            messages=(
                Message(role=Role.SYSTEM, content=NARRATIVE_SYSTEM_PROMPT, trust=TrustLevel.SYSTEM),
                Message(role=Role.USER, content=facts, trust=TrustLevel.VERIFIED),
            ),
            max_tokens=self._max_tokens,
            temperature=0.0,
            classification=DataClassification.CONFIDENTIAL,
            task="summarize",
        )
        inbound = self._guard.inspect_request(request)
        if inbound.blocked or inbound.request is None:
            return Narrative(status="blocked", notes=inbound.reasons)
        try:
            routed = await self._router.generate(inbound.request)
        except AgenticCoreError as exc:
            return Narrative(status="unavailable", notes=(f"{exc.code}: {exc}",))
        outbound = self._guard.inspect_output(routed.response.text)
        if outbound.blocked or outbound.text is None:
            return Narrative(status="blocked", notes=outbound.reasons)
        return Narrative(
            status="drafted",
            text=outbound.text.strip(),
            provider=routed.response.provider,
            model=routed.response.model,
            notes=outbound.reasons,
        )


def deidentified_facts(
    *,
    result: LabResult,
    spec: AnalyteSpec,
    flag: Flag | None,
    findings: Sequence[Finding],
    trend: TrendSummary | None,
    age_days: int,
) -> str:
    """Evidence restated without identifiers; ages are reported in years only."""
    lines = [
        f"Analyte: {spec.name} ({spec.code}); value {result.value:g} {result.unit}; "
        f"flag {flag.value if flag else 'n/a'}.",
        f"Patient: {result.patient.sex.value}, age {age_days // 365} years.",
    ]
    if trend is not None and trend.slope_per_day is not None:
        lines.append(
            f"Trend over {trend.window_hours:g} h: {trend.direction}, "
            f"{trend.slope_per_day:+.3g} {spec.unit}/day across {trend.points} results."
        )
    lines.extend(f"Finding [{f.severity.value}] {f.kind.value}: {f.message}" for f in findings)
    return "\n".join(lines)


class EvidencePacket(BaseModel):
    model_config = ConfigDict(frozen=True)

    packet_version: Literal["1"] = "1"
    assembled_by: str
    assembled_at: datetime
    result: LabResult
    flag: Flag | None
    findings: tuple[Finding, ...]
    highest_severity: Severity
    prior_results: tuple[PriorResult, ...]
    trend: TrendSummary | None
    specimen_results: tuple[dict[str, Any], ...]
    patient_based_qc: dict[str, Any] | None
    catalogue: dict[str, Any]
    citations: dict[str, str]
    provenance: ProvenanceRecord
    narrative: Narrative

    def digest(self) -> str:
        return content_digest(canonical_json(self.model_dump(mode="json")))


class EvidenceCollectionAgent:
    name = f"{AGENT_PREFIX}/evidence-collection"

    def __init__(
        self,
        catalogue: AnalyteCatalogue,
        *,
        drafter: NarrativeDrafter | None = None,
        prior_limit: int = 10,
    ) -> None:
        self._catalogue = catalogue
        self._drafter = drafter
        self._prior_limit = prior_limit

    async def collect(
        self,
        *,
        result: LabResult,
        spec: AnalyteSpec | None,
        flag: Flag | None,
        findings: Sequence[Finding],
        history: Sequence[PriorResult],
        trend: TrendSummary | None,
        specimen: Sequence[LabResult],
        qc_snapshot: dict[str, Any] | None,
        now: datetime,
    ) -> EvidencePacket:
        cited = sorted({c for f in findings for c in f.citations})
        narrative = Narrative(status="disabled")
        if self._drafter is not None and spec is not None:
            narrative = await self._drafter.draft(
                deidentified_facts(
                    result=result,
                    spec=spec,
                    flag=flag,
                    findings=findings,
                    trend=trend,
                    age_days=result.patient.age_days(result.collected_at),
                )
            )
        governance = self._catalogue.governance
        return EvidencePacket(
            assembled_by=self.name,
            assembled_at=now,
            result=result,
            flag=flag,
            findings=tuple(findings),
            highest_severity=max(
                (f.severity for f in findings), key=lambda s: s.rank, default=Severity.INFO
            ),
            prior_results=tuple(
                sorted(history, key=lambda p: p.collected_at, reverse=True)[: self._prior_limit]
            ),
            trend=trend,
            specimen_results=tuple(
                {
                    "result_id": r.result_id,
                    "analyte_code": r.analyte_code,
                    "value": r.value,
                    "unit": r.unit,
                }
                for r in specimen
                if r.result_id != result.result_id
            ),
            patient_based_qc=qc_snapshot,
            catalogue={
                "catalogue_id": governance.catalogue_id,
                "version": governance.version,
                "status": governance.status,
                "digest": self._catalogue.digest,
            },
            citations={
                c: self._catalogue.sources[c] for c in cited if c in self._catalogue.sources
            },
            provenance=ProvenanceRecord.for_content(
                canonical_json(result.model_dump(mode="json")),
                source_uri=f"lis://results/{result.result_id}",
                source_type="lis-result",
                trust=TrustLevel.VERIFIED,
                classification=DataClassification.RESTRICTED,
                retrieved_by=self.name,
            ),
            narrative=narrative,
        )
