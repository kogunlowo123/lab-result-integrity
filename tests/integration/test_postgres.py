"""Repository behaviour against a real PostgreSQL (CI service container).

Runs when LRI_TEST_DATABASE_URL points at PostgreSQL, e.g.
postgresql+asyncpg://lri:ci-only@localhost:5432/lri
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

from lab_result_integrity.domain.catalogue import AnalyteCatalogue
from lab_result_integrity.domain.errors import ConflictError
from lab_result_integrity.domain.models import Priority
from lab_result_integrity.domain.pbrtqc import PbrtqcState
from lab_result_integrity.domain.review import CaseStatus, ReviewCase
from lab_result_integrity.infrastructure.database import (
    SqlCaseRepository,
    SqlPbrtqcStore,
    SqlResultRepository,
    create_schema,
    make_engine,
)
from tests.conftest import NOW, POTASSIUM, ROOT, make_result

URL = os.environ.get("LRI_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL.startswith("postgresql"), reason="PostgreSQL not configured"),
]


async def test_repositories_round_trip() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    await create_schema(engine)  # idempotent
    results = SqlResultRepository(engine)
    cases = SqlCaseRepository(engine)
    run = uuid.uuid4().hex[:8]
    patient = f"P-PG-{run}"

    older = make_result(
        POTASSIUM,
        4.0,
        patient_id=patient,
        collected_at=NOW - timedelta(hours=10),
        result_id=f"R-PG-{run}-1",
    )
    newer = make_result(POTASSIUM, 5.4, patient_id=patient, result_id=f"R-PG-{run}-2")
    await results.add_many([(older, "sha256:a"), (newer, "sha256:b")])
    with pytest.raises(ConflictError):
        await results.add_many([(older, "sha256:a")])
    assert set(await results.get_many([older.result_id, "R-none"])) == {older.result_id}
    history = await results.history(
        patient,
        POTASSIUM,
        before=newer.collected_at,
        since=newer.collected_at - timedelta(hours=48),
        exclude_specimen=newer.specimen_id,
        limit=10,
    )
    assert [h.result_id for h in history] == [older.result_id]
    assert history[0].collected_at.tzinfo is not None
    stored = await results.get(newer.result_id)
    assert stored is not None
    assert stored.result == newer

    case = ReviewCase(
        case_id=uuid.uuid4().hex,
        result_id=newer.result_id,
        patient_id=patient,
        analyte_code=POTASSIUM,
        priority=Priority.STAT,
        created_at=NOW,
        due_at=NOW + timedelta(minutes=30),
        findings=(),
        evidence={},
        evidence_digest="sha256:e",
    )
    await cases.add(case)
    claimed = await cases.update(case.claim("dr.pg"))
    with pytest.raises(ConflictError):
        await cases.update(case.claim("dr.other"))  # stale version
    assert claimed.status is CaseStatus.CLAIMED
    assert (await cases.for_result(newer.result_id)) == claimed
    assert any(
        c.case_id == case.case_id for c in await cases.overdue(NOW + timedelta(hours=1), limit=1000)
    )
    await engine.dispose()


async def test_pbrtqc_optimistic_concurrency() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    store = SqlPbrtqcStore(engine)
    catalogue = AnalyteCatalogue.load(ROOT / "configs" / "analytes.yaml", allow_starter=True)
    spec = catalogue.analyte(POTASSIUM)
    assert spec is not None
    assert spec.pbrtqc is not None
    instrument = f"PG-{uuid.uuid4().hex[:8]}"
    fresh = PbrtqcState.fresh(instrument, POTASSIUM, spec.pbrtqc)
    saved = await store.save(fresh)
    with pytest.raises(ConflictError):
        await store.save(fresh)  # second creator loses
    await store.save(saved)
    with pytest.raises(ConflictError):
        await store.save(saved)  # stale update loses
    assert (await store.get(instrument, POTASSIUM)) is not None
    await engine.dispose()
