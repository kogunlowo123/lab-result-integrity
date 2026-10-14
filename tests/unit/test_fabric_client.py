"""agent-identity-fabric client: recorded wire shapes, fail-closed error mapping."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
from agentic_core.domain.classification import DataClassification, Decision

from lab_result_integrity.domain.errors import ForbiddenError, UnauthorizedError, UpstreamError
from lab_result_integrity.integrations.identity_fabric import JWT_BEARER, IdentityFabricClient

BASE = "https://fabric.agents.test/"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> IdentityFabricClient:
    return IdentityFabricClient(
        BASE, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_token_uses_jwt_bearer_grant() -> None:
    seen: dict[str, list[str]] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/token"
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "access_token": "eyJ.x.y",
                "token_type": "Bearer",
                "expires_in": 600,
                "scope": "results:screen",
                "issued_token_type": "urn:ietf:params:oauth:token-type:jwt",
            },
        )

    issued = await client(handle).token(
        client_id="spiffe://agents.test/agent/lab/screener", assertion="wl", scope="results:screen"
    )
    assert issued.expires_in == 600
    assert seen["grant_type"] == [JWT_BEARER]
    assert seen["scope"] == ["results:screen"]


async def test_introspect_and_authorize() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/introspect":
            return httpx.Response(200, json={"active": True, "sub": "a", "scope": "x y"})
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "decision": "require_approval",
                "agent": "a",
                "action": "review:propose",
                "resource": "review-case:1",
                "matched_rules": ["r"],
                "policy_version": "1",
                "policy_digest": "sha256:p",
                "reason": "human approval required",
                "delegation_chain": [],
            },
        )

    fabric = client(handle)
    info = await fabric.introspect("tok")
    assert info.scopes == frozenset({"x", "y"})
    decision = await fabric.authorize(
        "tok",
        action="review:propose",
        resource_type="review-case",
        resource_id="1",
        classification=DataClassification.RESTRICTED,
    )
    assert decision.decision is Decision.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), UnauthorizedError),
        (httpx.Response(401, json={"error": "unauthorized"}), UnauthorizedError),
        (httpx.Response(403, json={"error_description": "no"}), ForbiddenError),
        (httpx.Response(404, json={"error": "not_found"}), UpstreamError),
        (httpx.Response(503, json={}), UpstreamError),
        (httpx.Response(200, text="<html>"), UpstreamError),
        (httpx.Response(200, json=["not", "an", "object"]), UpstreamError),
        (httpx.Response(200, json={"active": "maybe"}), UpstreamError),
    ],
)
async def test_errors_fail_closed(response: httpx.Response, error: type[Exception]) -> None:
    with pytest.raises(error):
        await client(lambda _: response).introspect("tok")


async def test_transport_failure_is_upstream_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(UpstreamError, match="unreachable"):
        await client(down).token(client_id="c", assertion="a")
