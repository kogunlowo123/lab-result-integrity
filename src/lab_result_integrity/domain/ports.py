"""Ports the agents and pipeline depend on. Adapters live in infrastructure."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from lab_result_integrity.domain.models import (
    LabResult,
    PriorResult,
    ScreeningOutcome,
    StoredResult,
)
from lab_result_integrity.domain.pbrtqc import PbrtqcState
from lab_result_integrity.domain.review import CaseStatus, ReviewCase


class ResultRepository(Protocol):
    async def add_many(self, items: Sequence[tuple[LabResult, str]]) -> None: ...

    async def get_many(self, result_ids: Sequence[str]) -> dict[str, StoredResult]: ...

    async def get(self, result_id: str) -> StoredResult | None: ...

    async def record_outcome(self, outcome: ScreeningOutcome) -> None: ...

    async def record_review(self, result_id: str, review_outcome: str) -> None: ...

    async def history(
        self,
        patient_id: str,
        analyte_code: str,
        *,
        before: datetime,
        since: datetime,
        exclude_specimen: str,
        limit: int,
    ) -> Sequence[PriorResult]: ...

    async def specimen(self, specimen_id: str) -> Sequence[LabResult]: ...


class CaseRepository(Protocol):
    async def add(self, case: ReviewCase) -> None: ...

    async def get(self, case_id: str) -> ReviewCase | None: ...

    async def for_result(self, result_id: str) -> ReviewCase | None: ...

    async def update(self, case: ReviewCase) -> ReviewCase: ...

    async def queue(
        self, *, statuses: Sequence[CaseStatus], limit: int, offset: int
    ) -> Sequence[ReviewCase]: ...

    async def overdue(self, now: datetime, *, limit: int) -> Sequence[ReviewCase]: ...


class PbrtqcStore(Protocol):
    async def get(self, instrument_id: str, analyte_code: str) -> PbrtqcState | None: ...

    async def save(self, state: PbrtqcState) -> PbrtqcState: ...

    async def all(self) -> Sequence[PbrtqcState]: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
