"""A2A protocol surface: Agent Card and JSON-RPC 2.0 ``message/send``.

Peers send a ``Message`` whose first ``data`` part names a skill and carries
its input. The request is routed to the same application service as REST and
MCP, so authorization, audit and the never-release invariant are identical on
every channel. Each call completes synchronously and returns a ``Task`` in a
terminal state with the result as a data artifact.

JSON-RPC error codes: -32700 parse error, -32600 invalid request, -32601
method not found, -32602 invalid params, -32603 internal error; server-defined
-32040 forbidden, -32041 not found, -32042 conflict, -32043 upstream
unavailable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from agentic_core.domain.identifiers import new_id
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from lab_result_integrity import __version__
from lab_result_integrity.api.schemas import QueueResponse, ScreenResponse
from lab_result_integrity.application.service import IntegrityService
from lab_result_integrity.domain.errors import (
    ConflictError,
    ForbiddenError,
    IntegrityError,
    NotFoundError,
    UpstreamError,
)
from lab_result_integrity.domain.models import LabResult
from lab_result_integrity.domain.review import ReviewOutcome
from lab_result_integrity.security.auth import Principal

PROTOCOL_VERSION = "0.3.0"


class Skill(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    examples: tuple[str, ...] = ()
    inputModes: tuple[str, ...] = ("application/json",)  # noqa: N815 - A2A wire name
    outputModes: tuple[str, ...] = ("application/json",)  # noqa: N815 - A2A wire name


SKILLS = (
    Skill(
        id="screen-results",
        name="Screen lab results",
        description=(
            "Delta checks, age/sex reference and critical ranges, HIL interference, implausible "
            "combinations and patient-based QC. Input {results: [LabResult]}. Never releases."
        ),
        tags=("laboratory", "integrity", "delta-check", "pbrtqc"),
    ),
    Skill(
        id="review-queue",
        name="Read the pathologist review queue",
        description="Open and claimed cases by priority and due time. Input {limit?: int}.",
        tags=("laboratory", "review"),
    ),
    Skill(
        id="get-case",
        name="Get a review case",
        description="A case with its evidence packet. Input {case_id: str}.",
        tags=("laboratory", "review", "evidence"),
    ),
    Skill(
        id="propose-disposition",
        name="Propose a disposition",
        description=(
            "Record a proposed outcome for a case, pending pathologist approval. "
            "Input {case_id, outcome, rationale}."
        ),
        tags=("laboratory", "review", "human-in-the-loop"),
    ),
)


def agent_card(public_base: str) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "lab-result-integrity",
        "description": (
            "Lab result integrity agents: trend analysis, interference detection, patient-based "
            "QC and evidence collection feeding a pathologist review queue. Results are never "
            "released or autoverified by an agent."
        ),
        "url": f"{public_base}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "documentationUrl": "https://github.com/kogunlowo123/lab-result-integrity",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [s.model_dump() for s in SKILLS],
        "securitySchemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Enterprise OIDC access token or agent-identity-fabric credential",
            }
        },
        "security": [{"bearer": []}],
    }


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str = Field(min_length=1, max_length=100)
    params: dict[str, Any] | None = None


class Part(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["text", "data", "file"]
    data: dict[str, Any] | None = None
    text: str | None = None


class A2AMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["user", "agent"]
    parts: list[Part] = Field(min_length=1, max_length=20)
    messageId: str = Field(min_length=1, max_length=128)  # noqa: N815 - A2A wire name
    contextId: str | None = Field(default=None, max_length=128)  # noqa: N815
    kind: Literal["message"] = "message"


class SendParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: A2AMessage


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _rpc_error(exc: IntegrityError) -> RpcError:
    if isinstance(exc, ForbiddenError):
        return RpcError(-32040, exc.message)
    if isinstance(exc, NotFoundError):
        return RpcError(-32041, exc.message)
    if isinstance(exc, ConflictError):
        return RpcError(-32042, exc.message)
    if isinstance(exc, UpstreamError):
        return RpcError(-32043, exc.message)
    return RpcError(-32602, exc.message)


_RESULTS = TypeAdapter(list[LabResult])


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    results: list[dict[str, Any]] | None = Field(default=None, max_length=500)
    case_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    outcome: ReviewOutcome | None = None
    rationale: str | None = Field(default=None, max_length=4000)
    limit: int = Field(default=20, ge=1, le=200)


class A2AHandler:
    def __init__(self, service: IntegrityService, *, now: Callable[[], datetime]) -> None:
        self._service = service
        self._now = now
        self._skills: dict[str, Callable[[Principal, SkillInput], Awaitable[dict[str, Any]]]] = {
            "screen-results": self._screen,
            "review-queue": self._queue,
            "get-case": self._case,
            "propose-disposition": self._propose,
        }

    async def handle(self, who: Principal, body: Any) -> dict[str, Any]:
        """Return a JSON-RPC response object (success or error)."""
        request_id: Any = body.get("id") if isinstance(body, dict) else None
        try:
            try:
                request = JsonRpcRequest.model_validate(body)
            except ValidationError as exc:
                raise RpcError(-32600, "invalid JSON-RPC request", _errors(exc)) from exc
            if request.method != "message/send":
                raise RpcError(-32601, f"method {request.method!r} not found")
            result = await self._send(who, request.params or {})
        except RpcError as failure:
            error: dict[str, Any] = {"code": failure.code, "message": failure.message}
            if failure.data is not None:
                error["data"] = failure.data
            return {"jsonrpc": "2.0", "id": request_id, "error": error}
        return {"jsonrpc": "2.0", "id": request.id, "result": result}

    async def _send(self, who: Principal, params: dict[str, Any]) -> dict[str, Any]:
        try:
            message = SendParams.model_validate(params).message
            data = next((p.data for p in message.parts if p.kind == "data" and p.data), None)
            if data is None:
                raise RpcError(-32602, "message needs a data part naming a skill")
            skill_input = SkillInput.model_validate(data)
        except ValidationError as exc:
            raise RpcError(-32602, "invalid params", _errors(exc)) from exc
        skill = self._skills.get(skill_input.skill)
        if skill is None:
            raise RpcError(-32602, f"unknown skill {skill_input.skill!r}")
        try:
            output = await skill(who, skill_input)
        except IntegrityError as exc:
            await self._service.record_channel_call(who, "a2a", skill_input.skill, exc.code)
            raise _rpc_error(exc) from exc
        await self._service.record_channel_call(who, "a2a", skill_input.skill, "ok")
        task_id = new_id()
        return {
            "kind": "task",
            "id": task_id,
            "contextId": message.contextId or new_id(),
            "status": {"state": "completed", "timestamp": self._now().isoformat()},
            "artifacts": [
                {
                    "artifactId": new_id(),
                    "name": skill_input.skill,
                    "parts": [{"kind": "data", "data": output}],
                }
            ],
        }

    async def _screen(self, who: Principal, data: SkillInput) -> dict[str, Any]:
        if not data.results:
            raise RpcError(-32602, "screen-results needs a non-empty results list")
        try:
            results = _RESULTS.validate_python(data.results)
        except ValidationError as exc:
            raise RpcError(-32602, "invalid results", _errors(exc)) from exc
        outcomes = await self._service.screen(who, results)
        return ScreenResponse.of(outcomes).model_dump(mode="json")

    async def _queue(self, who: Principal, data: SkillInput) -> dict[str, Any]:
        items = await self._service.queue(who, limit=data.limit)
        return QueueResponse(items=items, limit=data.limit, offset=0).model_dump(mode="json")

    async def _case(self, who: Principal, data: SkillInput) -> dict[str, Any]:
        if data.case_id is None:
            raise RpcError(-32602, "get-case needs case_id")
        return (await self._service.case(who, data.case_id)).model_dump(mode="json")

    async def _propose(self, who: Principal, data: SkillInput) -> dict[str, Any]:
        if data.case_id is None or data.outcome is None or not (data.rationale or "").strip():
            raise RpcError(-32602, "propose-disposition needs case_id, outcome and rationale")
        case = await self._service.propose(who, data.case_id, data.outcome, data.rationale or "")
        latest = case.proposals[-1]
        return {
            "case_id": case.case_id,
            "proposal_status": latest.status,
            "policy_decision": latest.policy.decision,
            "matched_rules": list(latest.policy.matched_rules),
            "case_status": case.status.value,
        }


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))} for e in exc.errors()]
