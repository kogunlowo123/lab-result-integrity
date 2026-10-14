"""Patient-based real-time QC: moving average, EWMA, truncation, alarm and recovery."""

from __future__ import annotations

import pytest

from lab_result_integrity.domain.catalogue import PbrtqcConfig
from lab_result_integrity.domain.models import FindingKind
from lab_result_integrity.domain.pbrtqc import PbrtqcState, PbrtqcUpdate, alarm_finding, update
from tests.conftest import NOW

SMA = PbrtqcConfig(
    method="sma",
    block_size=5,
    truncation_low=3.0,
    truncation_high=5.5,
    target=4.2,
    control_limit=0.25,
    cite=("badrick-2019",),
)
EWMA = PbrtqcConfig(
    method="ewma",
    block_size=3,
    ewma_lambda=0.5,
    truncation_low=130,
    truncation_high=150,
    target=140,
    control_limit=2.0,
    cite=("badrick-2019",),
)


def feed(
    state: PbrtqcState, config: PbrtqcConfig, values: list[float], start: int = 0
) -> tuple[PbrtqcState, list[PbrtqcUpdate]]:
    updates = []
    for i, value in enumerate(values, start=start):
        result = update(state, config, result_id=f"R-{i}", value=value, at=NOW)
        state = result.state
        updates.append(result)
    return state, updates


def test_sma_warms_up_then_alarms_on_shift() -> None:
    state = PbrtqcState.fresh("CHEM-01", "2823-3", SMA)
    state, _ = feed(state, SMA, [4.2, 4.1, 4.3, 4.2])
    assert state.statistic is None
    assert state.in_control
    state, updates = feed(state, SMA, [4.2, 4.6, 4.8, 4.9, 4.9], start=4)
    started = [u for u in updates if u.alarm_started]
    assert len(started) == 1
    assert not state.in_control
    assert state.alarms == 1
    assert len(state.affected_result_ids) == SMA.block_size
    finding = alarm_finding(updates[-1], SMA, "Potassium")
    assert finding.kind is FindingKind.PBRTQC_ALARM
    assert finding.evidence["affected_result_ids"]


def test_recovery_clears_alarm() -> None:
    state = PbrtqcState.fresh("CHEM-01", "2823-3", SMA)
    state, _ = feed(state, SMA, [4.9] * 5)
    assert not state.in_control
    state, updates = feed(state, SMA, [4.0] * 5, start=5)
    assert any(u.recovered for u in updates)
    assert state.in_control
    assert state.affected_result_ids == ()


def test_truncation_excludes_values() -> None:
    state = PbrtqcState.fresh("CHEM-01", "2823-3", SMA)
    result = update(state, SMA, result_id="R-x", value=7.5, at=NOW)
    assert not result.included
    assert result.state.excluded == 1
    assert result.state.accepted == 0


def test_ewma_detects_shift() -> None:
    state = PbrtqcState.fresh("CHEM-02", "2951-2", EWMA)
    state, _ = feed(state, EWMA, [140, 141, 139])
    assert state.in_control
    assert state.statistic == pytest.approx(139.75)
    state, updates = feed(state, EWMA, [146, 147], start=3)
    assert updates[-1].out_of_control


def test_replayed_result_is_not_counted_twice() -> None:
    state = PbrtqcState.fresh("CHEM-01", "2823-3", SMA)
    first = update(state, SMA, result_id="R-1", value=4.2, at=NOW)
    again = update(first.state, SMA, result_id="R-1", value=4.2, at=NOW)
    assert again.state == first.state


def test_config_change_resets_state_but_keeps_version() -> None:
    state = PbrtqcState.fresh("CHEM-01", "2823-3", SMA).model_copy(update={"version": 7})
    state, _ = feed(state, SMA, [4.9] * 5)
    changed = SMA.model_copy(update={"control_limit": 1.0})
    result = update(state, changed, result_id="R-new", value=4.2, at=NOW)
    assert result.state.accepted == 1
    assert result.state.version == 7
    assert result.state.in_control
