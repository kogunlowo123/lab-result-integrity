"""SQLAlchemy Core persistence. PostgreSQL in production; SQLite in tests.

Documents are stored as validated JSON alongside the indexed columns that
queries need. Cases and patient-based QC state use optimistic concurrency
(a ``version`` column) so several replicas can work the same queue safely.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lab_result_integrity.domain.errors import ConflictError, NotFoundError
from lab_result_integrity.domain.models import (
    LabResult,
    PriorResult,
    ScreeningOutcome,
    StoredResult,
)
from lab_result_integrity.domain.pbrtqc import PbrtqcState
from lab_result_integrity.domain.review import CaseStatus, ReviewCase

metadata = MetaData()

results = Table(
    "results",
    metadata,
    Column("result_id", String(128), primary_key=True),
    Column("patient_id", String(128), nullable=False),
    Column("analyte_code", String(16), nullable=False),
    Column("specimen_id", String(128), nullable=False),
    Column("instrument_id", String(128), nullable=False),
    Column("collected_at", DateTime(timezone=True), nullable=False),
    Column("value", Float, nullable=False),
    Column("content_digest", String(80), nullable=False),
    Column("document", Text, nullable=False),
    Column("outcome", Text, nullable=True),
    Column("review_outcome", String(32), nullable=True),
    Index("ix_results_history", "patient_id", "analyte_code", "collected_at"),
    Index("ix_results_specimen", "specimen_id"),
)

cases = Table(
    "review_cases",
    metadata,
    Column("case_id", String(64), primary_key=True),
    Column("result_id", String(128), nullable=False, unique=True),
    Column("status", String(16), nullable=False),
    Column("priority_rank", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Index("ix_cases_queue", "status", "priority_rank", "due_at"),
)

pbrtqc_states = Table(
    "pbrtqc_states",
    metadata,
    Column("instrument_id", String(128), primary_key=True),
    Column("analyte_code", String(16), primary_key=True),
    Column("document", Text, nullable=False),
    Column("version", Integer, nullable=False),
)

schema_version = Table(
    "schema_version",
    metadata,
    Column("version", Integer, primary_key=True),
)

SCHEMA_VERSION = 1


def make_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        # SQLite serves tests, local runs and the CI smoke test. WAL with
        # synchronous=NORMAL keeps commits durable across process crashes
        # without an fsync per transaction; busy_timeout absorbs writer overlap.
        event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    return engine


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        current = (await conn.execute(select(schema_version.c.version))).scalar()
        if current is None:
            await conn.execute(insert(schema_version).values(version=SCHEMA_VERSION))


def utc(value: datetime) -> datetime:
    """Normalise to UTC so every dialect compares instants, not wall-clock strings."""
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _stored(row: Any) -> StoredResult:
    return StoredResult(
        result=LabResult.model_validate_json(row[0]),
        content_digest=row[1],
        outcome=ScreeningOutcome.model_validate_json(row[2]) if row[2] else None,
        review_outcome=row[3],
    )


class SqlResultRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_many(self, items: Sequence[tuple[LabResult, str]]) -> None:
        """Insert a batch atomically; any existing result_id rejects the whole batch."""
        if not items:
            return
        rows = [
            {
                "result_id": result.result_id,
                "patient_id": result.patient.patient_id,
                "analyte_code": result.analyte_code,
                "specimen_id": result.specimen_id,
                "instrument_id": result.instrument_id,
                "collected_at": utc(result.collected_at),
                "value": result.value,
                "content_digest": digest,
                "document": result.model_dump_json(),
            }
            for result, digest in items
        ]
        try:
            async with self._engine.begin() as conn:
                await conn.execute(insert(results), rows)
        except SqlIntegrityError as exc:
            raise ConflictError("one or more results were submitted concurrently") from exc

    async def get_many(self, result_ids: Sequence[str]) -> dict[str, StoredResult]:
        if not result_ids:
            return {}
        query = select(
            results.c.document,
            results.c.content_digest,
            results.c.outcome,
            results.c.review_outcome,
        ).where(results.c.result_id.in_(list(result_ids)))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        found = [_stored(row) for row in rows]
        return {s.result.result_id: s for s in found}

    async def get(self, result_id: str) -> StoredResult | None:
        query = select(
            results.c.document,
            results.c.content_digest,
            results.c.outcome,
            results.c.review_outcome,
        ).where(results.c.result_id == result_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return _stored(row) if row is not None else None

    async def record_outcome(self, outcome: ScreeningOutcome) -> None:
        async with self._engine.begin() as conn:
            done = await conn.execute(
                update(results)
                .where(results.c.result_id == outcome.result_id)
                .values(outcome=outcome.model_dump_json())
            )
        if done.rowcount == 0:
            raise NotFoundError(f"result {outcome.result_id} not found")

    async def record_review(self, result_id: str, review_outcome: str) -> None:
        async with self._engine.begin() as conn:
            done = await conn.execute(
                update(results)
                .where(results.c.result_id == result_id)
                .values(review_outcome=review_outcome)
            )
        if done.rowcount == 0:
            raise NotFoundError(f"result {result_id} not found")

    async def history(
        self,
        patient_id: str,
        analyte_code: str,
        *,
        before: datetime,
        since: datetime,
        exclude_specimen: str,
        limit: int,
    ) -> Sequence[PriorResult]:
        query = (
            select(
                results.c.result_id,
                results.c.value,
                results.c.collected_at,
                results.c.specimen_id,
            )
            .where(
                and_(
                    results.c.patient_id == patient_id,
                    results.c.analyte_code == analyte_code,
                    results.c.collected_at < utc(before),
                    results.c.collected_at >= utc(since),
                    results.c.specimen_id != exclude_specimen,
                )
            )
            .order_by(results.c.collected_at.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [
            PriorResult(result_id=r[0], value=r[1], collected_at=utc(r[2]), specimen_id=r[3])
            for r in rows
        ]

    async def specimen(self, specimen_id: str) -> Sequence[LabResult]:
        query = (
            select(results.c.document)
            .where(results.c.specimen_id == specimen_id)
            .order_by(results.c.result_id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [LabResult.model_validate_json(r[0]) for r in rows]


class SqlCaseRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, case: ReviewCase) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(cases).values(
                        case_id=case.case_id,
                        result_id=case.result_id,
                        status=case.status.value,
                        priority_rank=case.priority.rank,
                        created_at=utc(case.created_at),
                        due_at=utc(case.due_at),
                        document=case.model_dump_json(),
                        version=case.version,
                    )
                )
        except SqlIntegrityError as exc:
            raise ConflictError(f"a review case for {case.result_id} already exists") from exc

    async def get(self, case_id: str) -> ReviewCase | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(cases.c.document).where(cases.c.case_id == case_id))
            ).first()
        return ReviewCase.model_validate_json(row[0]) if row else None

    async def for_result(self, result_id: str) -> ReviewCase | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(cases.c.document).where(cases.c.result_id == result_id))
            ).first()
        return ReviewCase.model_validate_json(row[0]) if row else None

    async def update(self, case: ReviewCase) -> ReviewCase:
        """Persist ``case`` if nobody changed it since it was read; returns the new version."""
        saved = case.model_copy(update={"version": case.version + 1})
        async with self._engine.begin() as conn:
            done = await conn.execute(
                update(cases)
                .where(and_(cases.c.case_id == case.case_id, cases.c.version == case.version))
                .values(
                    status=saved.status.value,
                    document=saved.model_dump_json(),
                    version=saved.version,
                )
            )
        if done.rowcount == 0:
            raise ConflictError(f"case {case.case_id} was modified concurrently; reload and retry")
        return saved

    async def queue(
        self, *, statuses: Sequence[CaseStatus], limit: int, offset: int
    ) -> Sequence[ReviewCase]:
        query = (
            select(cases.c.document)
            .where(cases.c.status.in_([s.value for s in statuses]))
            .order_by(cases.c.priority_rank, cases.c.due_at, cases.c.case_id)
            .limit(limit)
            .offset(offset)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [ReviewCase.model_validate_json(r[0]) for r in rows]

    async def overdue(self, now: datetime, *, limit: int) -> Sequence[ReviewCase]:
        query = (
            select(cases.c.document)
            .where(
                and_(
                    cases.c.status != CaseStatus.DECIDED.value,
                    cases.c.due_at < utc(now),
                )
            )
            .order_by(cases.c.due_at)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [ReviewCase.model_validate_json(r[0]) for r in rows]


class SqlPbrtqcStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, instrument_id: str, analyte_code: str) -> PbrtqcState | None:
        query = select(pbrtqc_states.c.document).where(
            and_(
                pbrtqc_states.c.instrument_id == instrument_id,
                pbrtqc_states.c.analyte_code == analyte_code,
            )
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return PbrtqcState.model_validate_json(row[0]) if row else None

    async def save(self, state: PbrtqcState) -> PbrtqcState:
        saved = state.model_copy(update={"version": state.version + 1})
        try:
            async with self._engine.begin() as conn:
                if state.version == 0:
                    await conn.execute(
                        insert(pbrtqc_states).values(
                            instrument_id=state.instrument_id,
                            analyte_code=state.analyte_code,
                            document=saved.model_dump_json(),
                            version=saved.version,
                        )
                    )
                    return saved
                done = await conn.execute(
                    update(pbrtqc_states)
                    .where(
                        and_(
                            pbrtqc_states.c.instrument_id == state.instrument_id,
                            pbrtqc_states.c.analyte_code == state.analyte_code,
                            pbrtqc_states.c.version == state.version,
                        )
                    )
                    .values(document=saved.model_dump_json(), version=saved.version)
                )
        except SqlIntegrityError as exc:
            raise ConflictError("patient-based QC state was created concurrently") from exc
        if done.rowcount == 0:
            raise ConflictError("patient-based QC state was modified concurrently")
        return saved

    async def all(self) -> Sequence[PbrtqcState]:
        query = select(pbrtqc_states.c.document).order_by(
            pbrtqc_states.c.instrument_id, pbrtqc_states.c.analyte_code
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [PbrtqcState.model_validate_json(r[0]) for r in rows]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
