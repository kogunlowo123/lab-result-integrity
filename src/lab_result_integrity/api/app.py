"""FastAPI application factory: REST API, MCP endpoint, A2A Agent Card and JSON-RPC."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from agentic_core.observability import bind_correlation_id, get_logger
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from lab_result_integrity import __version__
from lab_result_integrity.api.routes import router
from lab_result_integrity.domain.errors import IntegrityError, UpstreamError
from lab_result_integrity.mcp_server.server import AuthenticatedMcpEndpoint, build_mcp
from lab_result_integrity.runtime.container import Container

ContainerFactory = Callable[[], Awaitable[Container]]

log = get_logger("lab_result_integrity.api")


async def sweep_review_sla_forever(
    container: Container,
    interval_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Escalate review cases that have passed their SLA."""
    while True:
        try:
            await container.service.escalate_overdue()
        except Exception:
            log.exception("review_sla_sweep_failed")
        await sleep(interval_s)


def create_app(factory: ContainerFactory, *, public_host: str = "localhost") -> FastAPI:
    holder: dict[str, Container] = {}

    def current() -> Container:
        found = holder.get("container")
        if found is None:
            raise UpstreamError("service is starting")
        return found

    mcp = build_mcp(current, allowed_host=public_host)
    mcp_endpoint = AuthenticatedMcpEndpoint(mcp, current)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await factory()
        holder["container"] = container
        app.state.container = container
        sweeper = asyncio.create_task(
            sweep_review_sla_forever(container, container.settings.sla_sweep_interval_s)
        )
        log.info("startup", version=__version__)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            await container.aclose()

    app = FastAPI(
        title="Lab Result Integrity",
        version=__version__,
        description=(
            "Lab result integrity agents: delta checks, reference and critical ranges by age and "
            "sex, HIL interference, implausible combinations, patient-based real-time QC, "
            "evidence packets and a pathologist review queue. Agents never release or "
            "autoverify results."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    endpoint: Any = mcp_endpoint  # an ASGI app; Starlette routes it without wrapping
    app.add_route("/mcp", endpoint, methods=["GET", "POST", "DELETE"], include_in_schema=False)
    _install_middleware_and_handlers(app)
    return app


def _install_middleware_and_handlers(app: FastAPI) -> None:

    @app.middleware("http")
    async def correlation_and_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else None
        started = time.perf_counter()
        with bind_correlation_id(cid) as bound:
            response = await call_next(request)
        response.headers["X-Correlation-ID"] = bound
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        container: Container | None = getattr(request.app.state, "container", None)
        if container is not None:
            container.metrics.requests.labels(
                request.method, path, str(response.status_code)
            ).observe(time.perf_counter() - started)
        return response

    @app.exception_handler(IntegrityError)
    async def integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "error_description": exc.message},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "error_description": "request failed validation",
                "details": [
                    {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))}
                    for e in exc.errors()
                ],
            },
        )
