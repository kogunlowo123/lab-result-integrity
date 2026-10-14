"""Typed client for agent-identity-fabric.

Wire formats follow the fabric's published contracts:

* ``POST /oauth2/token``      RFC 7523 jwt-bearer grant (form encoded)
* ``POST /oauth2/introspect`` RFC 7662 token introspection (form encoded)
* ``POST /v1/authorize``      policy decision for an agent action (JSON, bearer)

Every failure fails closed: transport errors and 5xx responses raise
``UpstreamError``; rejected credentials raise ``UnauthorizedError``.
"""

from __future__ import annotations

from typing import Any

import httpx
from agentic_core.domain.classification import DataClassification, Decision
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lab_result_integrity.domain.errors import ForbiddenError, UnauthorizedError, UpstreamError

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class FabricToken(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    access_token: str
    # RFC 6750 token type name, not a credential.
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int
    scope: str


class FabricIntrospection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    active: bool
    sub: str | None = None
    scope: str | None = None
    agent_status: str | None = None
    iss: str | None = None
    exp: int | None = None

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset((self.scope or "").split())


class FabricDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    decision: Decision
    agent: str
    action: str
    resource: str
    matched_rules: tuple[str, ...] = ()
    policy_version: str
    policy_digest: str
    reason: str
    delegation_chain: tuple[str, ...] = Field(default=())


class IdentityFabricClient:
    def __init__(self, base_url: str, *, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, f"{self._base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"identity fabric unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise UpstreamError(f"identity fabric returned {response.status_code}")
        return response

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError("identity fabric returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise UpstreamError("identity fabric returned an unexpected body")
        return body

    @staticmethod
    def _rejected(response: httpx.Response, body: dict[str, Any]) -> None:
        if response.status_code in (400, 401):
            raise UnauthorizedError(
                f"identity fabric rejected the credential: {body.get('error', 'invalid')}"
            )
        if response.status_code == 403:
            raise ForbiddenError(str(body.get("error_description", "forbidden by fabric")))
        if response.status_code >= 400:
            raise UpstreamError(f"identity fabric returned {response.status_code}")

    async def token(
        self, *, client_id: str, assertion: str, scope: str | None = None
    ) -> FabricToken:
        """Exchange a workload token for a short-lived agent credential."""
        form = {"grant_type": JWT_BEARER, "client_id": client_id, "assertion": assertion}
        if scope:
            form["scope"] = scope
        response = await self._send("POST", "/oauth2/token", data=form)
        body = self._body(response)
        self._rejected(response, body)
        return _parse(FabricToken, body)

    async def introspect(self, token: str) -> FabricIntrospection:
        response = await self._send("POST", "/oauth2/introspect", data={"token": token})
        body = self._body(response)
        self._rejected(response, body)
        return _parse(FabricIntrospection, body)

    async def authorize(
        self,
        token: str,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        classification: DataClassification,
        attributes: dict[str, Any] | None = None,
        environment: dict[str, Any] | None = None,
    ) -> FabricDecision:
        payload = {
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "resource_classification": int(classification),
            "resource_attributes": attributes or {},
            "environment": environment or {},
        }
        response = await self._send(
            "POST", "/v1/authorize", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
        body = self._body(response)
        self._rejected(response, body)
        return _parse(FabricDecision, body)


def _parse[M: BaseModel](model: type[M], body: dict[str, Any]) -> M:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise UpstreamError(
            f"identity fabric response failed validation: {model.__name__}"
        ) from exc
