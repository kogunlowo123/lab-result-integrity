"""Patient-based real-time quality control (moving average / EWMA).

Each (instrument, analyte) pair keeps a moving statistic over patient results
that fall inside the truncation limits. When the statistic moves further than
``control_limit`` from ``target`` the pair is out of control: every result
from that instrument and analyte is held for review until the statistic comes
back inside the limits, and the results in the window at the moment of the
alarm are listed as potentially affected (look-back).
"""

from __future__ import annotations

from datetime import datetime

from agentic_core.domain.provenance import canonical_json, content_digest
from pydantic import BaseModel, ConfigDict, Field

from lab_result_integrity.domain.catalogue import PbrtqcConfig
from lab_result_integrity.domain.models import Finding, FindingKind, Severity


class WindowEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    result_id: str
    value: float


class PbrtqcState(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: str
    analyte_code: str
    config_digest: str
    window: tuple[WindowEntry, ...] = ()
    ewma: float | None = None
    accepted: int = 0
    excluded: int = 0
    statistic: float | None = None
    in_control: bool = True
    alarms: int = 0
    last_alarm_at: datetime | None = None
    affected_result_ids: tuple[str, ...] = Field(
        default=(), description="Window contents when the current alarm started"
    )
    version: int = 0

    @classmethod
    def fresh(cls, instrument_id: str, analyte_code: str, config: PbrtqcConfig) -> PbrtqcState:
        return cls(
            instrument_id=instrument_id,
            analyte_code=analyte_code,
            config_digest=config_digest(config),
        )


class PbrtqcUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: PbrtqcState
    included: bool
    alarm_started: bool
    recovered: bool

    @property
    def out_of_control(self) -> bool:
        return not self.state.in_control


def config_digest(config: PbrtqcConfig) -> str:
    return content_digest(canonical_json(config.model_dump(mode="json")))


def update(
    state: PbrtqcState, config: PbrtqcConfig, *, result_id: str, value: float, at: datetime
) -> PbrtqcUpdate:
    """Fold one patient result into the moving statistic."""
    if state.config_digest != config_digest(config):
        state = PbrtqcState.fresh(state.instrument_id, state.analyte_code, config).model_copy(
            update={"version": state.version}
        )
    if any(e.result_id == result_id for e in state.window):
        # Replay of a result already folded in (a resumed screening): no double counting.
        return PbrtqcUpdate(state=state, included=True, alarm_started=False, recovered=False)
    if not config.truncation_low <= value <= config.truncation_high:
        excluded = state.model_copy(update={"excluded": state.excluded + 1})
        return PbrtqcUpdate(state=excluded, included=False, alarm_started=False, recovered=False)

    window = (*state.window, WindowEntry(result_id=result_id, value=value))[-config.block_size :]
    accepted = state.accepted + 1
    ewma = state.ewma
    statistic: float | None
    if config.method == "sma":
        statistic = (
            sum(e.value for e in window) / len(window) if len(window) >= config.block_size else None
        )
    else:
        lam = config.ewma_lambda if config.ewma_lambda is not None else 1.0
        ewma = value if ewma is None else lam * value + (1 - lam) * ewma
        statistic = ewma if accepted >= config.block_size else None

    in_control = state.in_control
    alarm_started = recovered = False
    alarms = state.alarms
    last_alarm_at = state.last_alarm_at
    affected = state.affected_result_ids
    if statistic is not None:
        out = abs(statistic - config.target) > config.control_limit
        if out and in_control:
            alarm_started, in_control = True, False
            alarms += 1
            last_alarm_at = at
            affected = tuple(e.result_id for e in window)
        elif not out and not in_control:
            recovered, in_control = True, True
            affected = ()
    new_state = state.model_copy(
        update={
            "window": window,
            "ewma": ewma,
            "accepted": accepted,
            "statistic": statistic,
            "in_control": in_control,
            "alarms": alarms,
            "last_alarm_at": last_alarm_at,
            "affected_result_ids": affected,
        }
    )
    return PbrtqcUpdate(
        state=new_state, included=True, alarm_started=alarm_started, recovered=recovered
    )


def alarm_finding(update_: PbrtqcUpdate, config: PbrtqcConfig, analyte_name: str) -> Finding:
    state = update_.state
    stat = state.statistic
    return Finding(
        kind=FindingKind.PBRTQC_ALARM,
        severity=Severity.HIGH,
        rule_id=f"{state.analyte_code}:pbrtqc:{state.instrument_id}",
        message=(
            f"patient-based QC for {analyte_name} on {state.instrument_id} is out of control "
            f"({config.method.upper()} {'n/a' if stat is None else f'{stat:.3g}'} vs target "
            f"{config.target:g} +/- {config.control_limit:g}); results from this instrument "
            "need review until the statistic recovers"
        ),
        evidence={
            "instrument_id": state.instrument_id,
            "method": config.method,
            "statistic": stat,
            "target": config.target,
            "control_limit": config.control_limit,
            "block_size": config.block_size,
            "alarm_started_at": (
                state.last_alarm_at.isoformat() if state.last_alarm_at is not None else None
            ),
            "affected_result_ids": list(state.affected_result_ids),
        },
        citations=config.cite,
    )
