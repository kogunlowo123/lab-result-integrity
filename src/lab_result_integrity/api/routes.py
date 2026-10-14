"""HTTP routes. Every /v1 route authenticates the bearer token and the service authorizes."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from lab_result_integrity.a2a_server.protocol import A2AHandler, agent_card
from lab_result_integrity.api.schemas import (
    DecisionRequest,
    PbrtqcResponse,
    ProposalRequest,
    QueueResponse,
    ScreenRequest,
    ScreenResponse,
)
from lab_result_integrity.application.service import AuditVerification, PatientTrend
from lab_result_integrity.domain.errors import IntegrityError
from lab_result_integrity.domain.models import IDENTIFIER, StoredResult
from lab_result_integrity.domain.review import CaseStatus, ReviewCase
from lab_result_integrity.runtime.container import Container
from lab_result_integrity.security.auth import Principal

router = APIRouter()

LOINC = r"^[0-9]{1,7}-[0-9]$"


def container(request: Request) -> Container:
    value: Container = request.app.state.container
    return value


async def principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    return await container(request).authenticator.authenticate(authorization)


Deps = Annotated[Container, Depends(container)]
Caller = Annotated[Principal, Depends(principal)]
Ident = Annotated[str, Path(pattern=IDENTIFIER)]
CaseId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


# ---------------------------------------------------------------- screening


@router.post("/v1/screenings", tags=["screening"])
async def screen(body: ScreenRequest, c: Deps, who: Caller) -> ScreenResponse:
    """Screen a batch of results. Never releases or autoverifies; returns holds and cases."""
    if len(body.results) > c.settings.max_batch:
        raise IntegrityError(f"batch exceeds {c.settings.max_batch} results")
    return ScreenResponse.of(await c.service.screen(who, body.results))


@router.get("/v1/results/{result_id}", tags=["screening"])
async def get_result(result_id: Ident, c: Deps, who: Caller) -> StoredResult:
    return await c.service.result(who, result_id)


@router.get("/v1/patients/{patient_id}/analytes/{analyte_code}/trend", tags=["screening"])
async def patient_trend(
    patient_id: Ident,
    analyte_code: Annotated[str, Path(pattern=LOINC)],
    c: Deps,
    who: Caller,
) -> PatientTrend:
    return await c.service.patient_trend(who, patient_id, analyte_code)


# ------------------------------------------------------------------- review


@router.get("/v1/review-queue", tags=["review"])
async def review_queue(
    c: Deps,
    who: Caller,
    status: Annotated[list[CaseStatus] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> QueueResponse:
    statuses = status or [CaseStatus.OPEN, CaseStatus.CLAIMED]
    items = await c.service.queue(who, statuses=statuses, limit=limit, offset=offset)
    return QueueResponse(items=items, limit=limit, offset=offset)


@router.get("/v1/cases/{case_id}", tags=["review"])
async def get_case(case_id: CaseId, c: Deps, who: Caller) -> ReviewCase:
    return await c.service.case(who, case_id)


@router.post("/v1/cases/{case_id}/claim", tags=["review"])
async def claim_case(case_id: CaseId, c: Deps, who: Caller) -> ReviewCase:
    return await c.service.claim(who, case_id)


@router.post("/v1/cases/{case_id}/decision", tags=["review"])
async def decide_case(case_id: CaseId, body: DecisionRequest, c: Deps, who: Caller) -> ReviewCase:
    """Pathologist decision. Humans only; the decision is bound to the evidence digest."""
    return await c.service.decide(who, case_id, body.outcome, body.comment)


@router.post("/v1/cases/{case_id}/proposals", tags=["review"], status_code=202)
async def propose(case_id: CaseId, body: ProposalRequest, c: Deps, who: Caller) -> ReviewCase:
    """Agent proposal; recorded as pending human approval and never changes the case state."""
    return await c.service.propose(who, case_id, body.outcome, body.rationale)


# -------------------------------------------------------------- QC and audit


@router.get("/v1/qc/pbrtqc", tags=["quality-control"])
async def pbrtqc(c: Deps, who: Caller) -> PbrtqcResponse:
    return PbrtqcResponse(items=await c.service.pbrtqc_status(who))


@router.get("/v1/catalogue", tags=["quality-control"])
async def catalogue(c: Deps, who: Caller) -> dict[str, object]:
    return await c.service.catalogue(who)


@router.post("/v1/audit/verify", tags=["audit"])
async def verify_audit(c: Deps, who: Caller) -> AuditVerification:
    return await c.service.verify_audit(who)


# ---------------------------------------------------------------------- A2A


@router.get("/.well-known/agent-card.json", tags=["a2a"])
async def card(c: Deps) -> dict[str, Any]:
    return agent_card(c.settings.public_base)


@router.post("/a2a", tags=["a2a"])
async def a2a(request: Request, c: Deps, who: Caller) -> JSONResponse:
    """A2A JSON-RPC 2.0 endpoint (``message/send``)."""
    raw = await request.body()
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        c.metrics.a2a_requests.labels("unknown", "parse_error").inc()
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        )
    handler = A2AHandler(c.service, now=c.service.now)
    reply = await handler.handle(who, body)
    method = body.get("method") if isinstance(body, dict) else None
    c.metrics.a2a_requests.labels(
        method if method == "message/send" else "other",
        "error" if "error" in reply else "ok",
    ).inc()
    return JSONResponse(reply)


# --------------------------------------------------------------- operations


@router.get("/healthz", tags=["operations"], include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["operations"], include_in_schema=False)
async def readyz(c: Deps, response: Response) -> dict[str, str]:
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {"status": "database unavailable"}
    return {"status": "ready"}


@router.get("/metrics", tags=["operations"], include_in_schema=False)
async def metrics(c: Deps) -> Response:
    return Response(generate_latest(c.metrics.registry), media_type=CONTENT_TYPE_LATEST)
