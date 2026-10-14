"""Fault injection: the service fails closed and degrades only where it is safe to."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_result_integrity.api.app import sweep_review_sla_forever
from lab_result_integrity.application.service import IntegrityService
from lab_result_integrity.domain.errors import ConflictError, UnauthorizedError, UpstreamError
from lab_result_integrity.domain.pbrtqc import PbrtqcState
from lab_result_integrity.infrastructure.database import make_engine
from lab_result_integrity.integrations.identity_fabric import IdentityFabricClient
from lab_result_integrity.runtime.container import Container
from lab_result_integrity.security.auth import Authenticator
from lab_result_integrity.security.jwks import RemoteJwks, StaticJwks
from tests.conftest import (
    AUDIENCE,
    OIDC_ISSUER,
    PATHOLOGIST,
    POTASSIUM,
    TECH,
    FakeIdp,
    make_result,
)

pytestmark = pytest.mark.chaos


def test_database_outage_flips_readiness(client: TestClient) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    assert client.get("/readyz").status_code == 200
    healthy = container.engine
    container.engine = make_engine("sqlite+aiosqlite:////nonexistent/dir/db.sqlite")
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200
    finally:
        container.engine = healthy


async def test_identity_fabric_outage_fails_closed(idp: FakeIdp) -> None:
    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        auth = Authenticator(
            StaticJwks(idp.jwks()),
            issuer=OIDC_ISSUER,
            audience=AUDIENCE,
            fabric=IdentityFabricClient("https://fabric.test", http=http),
            fabric_issuer="https://fabric.test",
        )
        token = FakeIdp(issuer="https://fabric.test").mint(sub="agent")
        with pytest.raises(UpstreamError):
            await auth.authenticate(f"Bearer {token}")


async def test_jwks_outage_fails_closed(idp: FakeIdp) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
        auth = Authenticator(
            RemoteJwks("https://login.lab.test/jwks", client=http),
            issuer=OIDC_ISSUER,
            audience=AUDIENCE,
        )
        with pytest.raises(UnauthorizedError):
            await auth.authenticate(f"Bearer {idp.mint(sub='x')}")


async def test_audit_failure_blocks_screening(service: IntegrityService) -> None:
    """If evidence cannot be written, the screening must not complete silently."""

    async def broken_append(_: object) -> None:
        raise OSError("disk full")

    service._audit._sink.append = broken_append  # type: ignore[attr-defined]
    with pytest.raises(OSError, match="disk full"):
        await service.screen(TECH, [make_result(POTASSIUM, 6.9)])


async def test_interrupted_screening_resumes(service: IntegrityService) -> None:
    result = make_result(POTASSIUM, 6.9)
    original = service.pipeline._results.record_outcome  # type: ignore[attr-defined]
    calls = {"n": 0}

    async def flaky(outcome: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database connection reset")
        await original(outcome)

    service.pipeline._results.record_outcome = flaky  # type: ignore[attr-defined]
    with pytest.raises(ConnectionError):
        await service.screen(TECH, [result])
    service.pipeline._results.record_outcome = original  # type: ignore[attr-defined]
    [resumed] = await service.screen(TECH, [result])
    assert resumed.case_id is not None
    queue = await service.queue(PATHOLOGIST)
    assert [c.case_id for c in queue] == [resumed.case_id]  # no duplicate case


async def test_pbrtqc_contention_retries_then_gives_up(container: Container) -> None:
    qc = container.service.pipeline._qc  # type: ignore[attr-defined]
    store = qc._store
    real_save = store.save
    attempts = {"n": 0}

    async def contended(state: PbrtqcState) -> PbrtqcState:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConflictError("modified concurrently")
        return await real_save(state)

    store.save = contended
    [outcome] = await container.service.screen(TECH, [make_result(POTASSIUM, 4.2)])
    assert attempts["n"] == 3
    assert outcome.disposition.value == "screened_no_findings"

    async def always(_: PbrtqcState) -> PbrtqcState:
        raise ConflictError("modified concurrently")

    store.save = always
    with pytest.raises(ConflictError, match="contention"):
        await container.service.screen(TECH, [make_result(POTASSIUM, 4.2)])


async def test_concurrent_reviewers_cannot_both_decide(container: Container) -> None:
    from lab_result_integrity.domain.review import ReviewOutcome
    from tests.conftest import OTHER_PATHOLOGIST, PATHOLOGIST

    [outcome] = await container.service.screen(TECH, [make_result(POTASSIUM, 7.0)])
    assert outcome.case_id is not None
    results: Sequence[object] = await asyncio.gather(
        container.service.decide(PATHOLOGIST, outcome.case_id, ReviewOutcome.CONFIRM, None),
        container.service.decide(OTHER_PATHOLOGIST, outcome.case_id, ReviewOutcome.CONFIRM, None),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], ConflictError)


async def test_sla_sweeper_survives_failures(container: Container) -> None:
    calls = {"n": 0}

    async def failing() -> int:
        calls["n"] += 1
        raise RuntimeError("transient")

    container.service.escalate_overdue = failing  # type: ignore[method-assign]

    async def stop_after_two(_: float) -> None:
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await sweep_review_sla_forever(container, 0.01, sleep=stop_after_two)
    assert calls["n"] == 2
