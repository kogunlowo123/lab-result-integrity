"""Latency budgets for hot paths. Loose enough for shared CI runners."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import coverage
import pytest

from lab_result_integrity.application.service import IntegrityService
from lab_result_integrity.domain.catalogue import AnalyteCatalogue
from lab_result_integrity.domain.models import PriorResult
from lab_result_integrity.domain.rules import delta_check, evaluate_ranges, hil_interference, trend
from tests.conftest import NOW, POTASSIUM, ROOT, TECH, make_result

pytestmark = pytest.mark.performance

CATALOGUE = AnalyteCatalogue.load(ROOT / "configs" / "analytes.yaml", allow_starter=True)


@contextmanager
def untraced() -> Iterator[None]:
    """Pause coverage tracing so budgets measure the code, not the tracer."""
    active = coverage.Coverage.current()
    if active is None:
        yield
        return
    active.stop()
    try:
        yield
    finally:
        active.start()


def test_rule_evaluation_budget() -> None:
    spec = CATALOGUE.analyte(POTASSIUM)
    assert spec is not None
    history = [
        PriorResult(
            result_id=f"R-{i}",
            value=4.0 + (i % 5) / 10,
            collected_at=NOW - timedelta(hours=2 + i),
            specimen_id=f"S-{i}",
        )
        for i in range(100)
    ]
    result = make_result(POTASSIUM, 5.6, hemolysis=120)
    with untraced():
        started = time.perf_counter()
        for _ in range(1000):
            evaluate_ranges(result, spec)
            delta_check(result, spec, history)
            trend(result, spec, history)
            hil_interference(result, spec)
        elapsed = time.perf_counter() - started
    per_result_ms = elapsed * 1000 / 1000
    assert per_result_ms < 2


async def test_batch_screening_budget(service: IntegrityService) -> None:
    batch = [
        make_result(
            POTASSIUM,
            4.0 + (i % 7) / 10,
            patient_id=f"P-{i:05d}",
            collected_at=NOW - timedelta(minutes=300 - i),
        )
        for i in range(200)
    ]
    with untraced():
        started = time.perf_counter()
        outcomes = await service.screen(TECH, batch)
        per_result_ms = (time.perf_counter() - started) * 1000 / len(batch)
    assert len(outcomes) == 200
    assert per_result_ms < 60
