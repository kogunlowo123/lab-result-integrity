"""MCP server (official ``mcp`` SDK, FastMCP, streamable HTTP) mounted at ``/mcp``.

The endpoint is wrapped by ``AuthenticatedMcpEndpoint``: a request without a
valid bearer token is rejected with HTTP 401 before it reaches the MCP
session manager. The authenticated principal travels in the ASGI scope; each
tool reads it, the application service authorizes the specific action, and
the tool call itself is written to the audit chain. Tools never release or
verify results; ``propose_disposition`` records a proposal that a pathologist
must accept.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from agentic_core.observability import bind_correlation_id
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from lab_result_integrity.api.schemas import PbrtqcResponse, QueueResponse, ScreenResponse
from lab_result_integrity.application.service import PatientTrend
from lab_result_integrity.domain.errors import IntegrityError
from lab_result_integrity.domain.models import IDENTIFIER, LabResult, StoredResult
from lab_result_integrity.domain.review import ReviewCase, ReviewOutcome
from lab_result_integrity.runtime.container import Container
from lab_result_integrity.security.auth import Principal

PRINCIPAL_KEY = "lri.principal"
ContainerGetter = Callable[[], Container]


class ProposalReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    proposal_status: str
    policy_decision: str
    matched_rules: tuple[str, ...]
    case_status: str


def _principal(ctx: Context[Any, Any, Any]) -> Principal:
    request = ctx.request_context.request
    scope = getattr(request, "scope", None)
    principal = scope.get(PRINCIPAL_KEY) if isinstance(scope, dict) else None
    if not isinstance(principal, Principal):
        raise ToolError("unauthenticated")
    return principal


def build_mcp(get_container: ContainerGetter, *, allowed_host: str) -> FastMCP:
    mcp = FastMCP(
        name="lab-result-integrity",
        instructions=(
            "Screen laboratory results for integrity problems (delta checks, critical values, "
            "HIL interference, implausible combinations, patient-based QC) and read the "
            "pathologist review queue. These tools never release, verify or autoverify a "
            "result; dispositions are decided by a pathologist."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[allowed_host, f"{allowed_host}:*"],
            allowed_origins=[f"https://{allowed_host}", f"http://{allowed_host}"],
        ),
    )

    async def call[T](
        ctx: Context[Any, Any, Any], tool: str, run: Callable[[Principal], Awaitable[T]]
    ) -> T:
        container = get_container()
        who = _principal(ctx)
        try:
            value = await run(who)
        except IntegrityError as exc:
            container.metrics.mcp_calls.labels(tool, exc.code).inc()
            await container.service.record_channel_call(who, "mcp", tool, exc.code)
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        container.metrics.mcp_calls.labels(tool, "ok").inc()
        await container.service.record_channel_call(who, "mcp", tool, "ok")
        return value

    @mcp.tool(description="Screen up to 500 lab results; returns holds, findings and case ids.")
    async def screen_results(
        results: Annotated[list[LabResult], Field(min_length=1, max_length=500)],
        ctx: Context[Any, Any, Any],
    ) -> ScreenResponse:
        async def run(who: Principal) -> ScreenResponse:
            return ScreenResponse.of(await get_container().service.screen(who, results))

        return await call(ctx, "screen_results", run)

    @mcp.tool(description="Get a result with its screening outcome and review state.")
    async def get_result(
        result_id: Annotated[str, Field(pattern=IDENTIFIER)], ctx: Context[Any, Any, Any]
    ) -> StoredResult:
        async def run(who: Principal) -> StoredResult:
            return await get_container().service.result(who, result_id)

        return await call(ctx, "get_result", run)

    @mcp.tool(description="Prior results and least-squares trend for a patient and analyte.")
    async def patient_trend(
        patient_id: Annotated[str, Field(pattern=IDENTIFIER)],
        analyte_code: Annotated[str, Field(pattern=r"^[0-9]{1,7}-[0-9]$")],
        ctx: Context[Any, Any, Any],
    ) -> PatientTrend:
        async def run(who: Principal) -> PatientTrend:
            return await get_container().service.patient_trend(who, patient_id, analyte_code)

        return await call(ctx, "patient_trend", run)

    @mcp.tool(description="Open and claimed review cases, highest priority and earliest due first.")
    async def review_queue(
        ctx: Context[Any, Any, Any],
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> QueueResponse:
        async def run(who: Principal) -> QueueResponse:
            items = await get_container().service.queue(who, limit=limit)
            return QueueResponse(items=items, limit=limit, offset=0)

        return await call(ctx, "review_queue", run)

    @mcp.tool(description="A review case with its evidence packet.")
    async def get_case(
        case_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")], ctx: Context[Any, Any, Any]
    ) -> ReviewCase:
        async def run(who: Principal) -> ReviewCase:
            return await get_container().service.case(who, case_id)

        return await call(ctx, "get_case", run)

    @mcp.tool(
        description=(
            "Propose a disposition for a review case. Recorded as pending pathologist "
            "approval; it never changes the case or releases the result."
        )
    )
    async def propose_disposition(
        case_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
        outcome: ReviewOutcome,
        rationale: Annotated[str, Field(min_length=1, max_length=4000)],
        ctx: Context[Any, Any, Any],
    ) -> ProposalReceipt:
        async def run(who: Principal) -> ProposalReceipt:
            case = await get_container().service.propose(who, case_id, outcome, rationale)
            latest = case.proposals[-1]
            return ProposalReceipt(
                case_id=case.case_id,
                proposal_status=latest.status,
                policy_decision=latest.policy.decision,
                matched_rules=latest.policy.matched_rules,
                case_status=case.status.value,
            )

        return await call(ctx, "propose_disposition", run)

    @mcp.tool(description="Patient-based real-time QC status per instrument and analyte.")
    async def pbrtqc_status(ctx: Context[Any, Any, Any]) -> PbrtqcResponse:
        async def run(who: Principal) -> PbrtqcResponse:
            return PbrtqcResponse(items=await get_container().service.pbrtqc_status(who))

        return await call(ctx, "pbrtqc_status", run)

    return mcp


class AuthenticatedMcpEndpoint:
    """ASGI endpoint: authenticate, then hand the request to the MCP session manager."""

    def __init__(self, mcp: FastMCP, get_container: ContainerGetter) -> None:
        mcp.streamable_http_app()  # creates the session manager
        self._inner = StreamableHTTPASGIApp(mcp.session_manager)
        self._get_container = get_container

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        incoming = headers.get("x-correlation-id")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else None
        with bind_correlation_id(cid):
            try:
                who = await self._get_container().authenticator.authenticate(
                    headers.get("authorization")
                )
            except IntegrityError as exc:
                response = JSONResponse(
                    {"error": exc.code, "error_description": exc.message},
                    status_code=exc.status,
                    headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
                )
                await response(scope, receive, send)
                return
            scope[PRINCIPAL_KEY] = who
            await self._inner(scope, receive, send)
