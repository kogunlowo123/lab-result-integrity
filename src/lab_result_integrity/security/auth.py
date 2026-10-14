"""Caller authentication and authorization.

Two kinds of bearer token are accepted:

* **Enterprise OIDC tokens** (pathologists, technologists, auditors, and
  agents registered as OIDC confidential clients). Verified locally against
  the issuer JWKS. A token whose ``agent_claim`` equals ``agent_claim_value``
  (Entra ID app-only tokens carry ``idtyp: app``) is an agent; others are users.
* **agent-identity-fabric credentials** when a fabric is configured. They are
  introspected at the fabric (RFC 7662), and every action is also authorized
  by the fabric's policy decision point.

Every action is evaluated by the local policy engine as well. When both the
fabric and the local policy decide, the most restrictive outcome wins, so the
domain invariants in ``policies/lab-result-integrity.yaml`` (agents never
release or decide results) hold whatever the fabric policy says.
"""

from __future__ import annotations

import base64
import binascii
import json
from enum import StrEnum
from typing import Any, Literal

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import Decision
from agentic_core.observability import correlation_id
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject
from pydantic import BaseModel, ConfigDict, Field

from lab_result_integrity.domain.errors import ForbiddenError, UnauthorizedError
from lab_result_integrity.domain.review import PolicyRecord
from lab_result_integrity.integrations.identity_fabric import IdentityFabricClient
from lab_result_integrity.security.jwks import KeyResolver, verify_jwt


class PrincipalKind(StrEnum):
    USER = "user"
    AGENT = "agent"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    kind: PrincipalKind
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    via: Literal["oidc", "fabric"]
    token: str = Field(default="", exclude=True, repr=False)


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("bearer token required")
    token = authorization[7:].strip()
    if not token or len(token) > 8192:
        raise UnauthorizedError("malformed bearer token")
    return token


def unverified_issuer(token: str) -> str | None:
    """Read ``iss`` without verification. Used only to choose a verifier, never trusted."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment))
    except (binascii.Error, ValueError):
        return None
    issuer = payload.get("iss") if isinstance(payload, dict) else None
    return issuer if isinstance(issuer, str) else None


def _strings(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(value.split())
    if isinstance(value, list):
        return frozenset(v for v in value if isinstance(v, str))
    return frozenset()


class Authenticator:
    def __init__(
        self,
        resolver: KeyResolver,
        *,
        issuer: str,
        audience: str,
        roles_claim: str = "roles",
        agent_claim: str = "idtyp",
        agent_claim_value: str = "app",
        fabric: IdentityFabricClient | None = None,
        fabric_issuer: str | None = None,
    ) -> None:
        self._resolver = resolver
        self._issuer = issuer
        self._audience = audience
        self._roles_claim = roles_claim
        self._agent_claim = agent_claim
        self._agent_claim_value = agent_claim_value
        self._fabric = fabric
        self._fabric_issuer = fabric_issuer.rstrip("/") if fabric_issuer else None

    async def authenticate(self, authorization: str | None) -> Principal:
        token = bearer_token(authorization)
        issuer = unverified_issuer(token)
        if (
            self._fabric is not None
            and self._fabric_issuer is not None
            and issuer is not None
            and issuer.rstrip("/") == self._fabric_issuer
        ):
            info = await self._fabric.introspect(token)
            if not info.active or not info.sub:
                raise UnauthorizedError("agent credential is not active")
            if info.agent_status not in (None, "active"):
                raise UnauthorizedError(f"agent is {info.agent_status}")
            return Principal(
                subject=info.sub,
                kind=PrincipalKind.AGENT,
                scopes=info.scopes,
                via="fabric",
                token=token,
            )
        claims = await verify_jwt(
            token, self._resolver, issuer=self._issuer, audience=self._audience
        )
        kind = (
            PrincipalKind.AGENT
            if claims.get(self._agent_claim) == self._agent_claim_value
            else PrincipalKind.USER
        )
        return Principal(
            subject=str(claims["sub"]),
            kind=kind,
            roles=_strings(claims.get(self._roles_claim)),
            scopes=_strings(claims.get("scp", claims.get("scope"))),
            via="oidc",
            token=token,
        )


def action_in_scope(action: str, scopes: frozenset[str]) -> bool:
    """Scopes match exactly or by a trailing wildcard segment, e.g. ``cases:*``."""
    for scope in scopes:
        if scope == action:
            return True
        if scope.endswith(":*") and action.startswith(scope[:-1]):
            return True
    return False


_RESTRICTIVENESS = {Decision.ALLOW: 0, Decision.REQUIRE_APPROVAL: 1, Decision.DENY: 2}


class AuthzOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    reason: str
    source: str

    def record(self) -> PolicyRecord:
        return PolicyRecord(
            decision=self.decision.value,
            matched_rules=self.matched_rules,
            policy_version=self.policy_version,
            policy_digest=self.policy_digest,
            source=self.source,
        )


class Authorizer:
    def __init__(
        self,
        policy: PolicyEngine,
        audit: AuditLog,
        *,
        fabric: IdentityFabricClient | None = None,
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._fabric = fabric

    @property
    def policy(self) -> PolicyEngine:
        return self._policy

    async def decide(
        self,
        principal: Principal,
        action: str,
        resource: Resource,
        *,
        environment: dict[str, Any] | None = None,
    ) -> AuthzOutcome:
        local = self._policy.evaluate(
            PolicyRequest(
                subject=Subject(
                    id=principal.subject,
                    kind=principal.kind.value,
                    roles=principal.roles,
                    attributes={
                        "scopes": sorted(principal.scopes),
                        "action_in_scope": action_in_scope(action, principal.scopes),
                        "via": principal.via,
                    },
                ),
                action=action,
                resource=resource,
                environment=environment or {},
            )
        )
        outcome = AuthzOutcome(
            decision=local.decision,
            matched_rules=local.matched_rules,
            policy_version=local.policy_version,
            policy_digest=local.policy_digest,
            reason=local.reason,
            source="local",
        )
        if principal.via == "fabric" and self._fabric is not None:
            remote = await self._fabric.authorize(
                principal.token,
                action=action,
                resource_type=resource.type,
                resource_id=resource.id,
                classification=resource.classification,
                attributes=resource.attributes,
                environment=environment,
            )
            if _RESTRICTIVENESS[remote.decision] > _RESTRICTIVENESS[outcome.decision]:
                outcome = AuthzOutcome(
                    decision=remote.decision,
                    matched_rules=tuple(f"fabric:{r}" for r in remote.matched_rules),
                    policy_version=remote.policy_version,
                    policy_digest=remote.policy_digest,
                    reason=f"identity fabric: {remote.reason}",
                    source="fabric",
                )
            else:
                outcome = outcome.model_copy(update={"source": "local+fabric"})
        await self._audit.record(
            actor=principal.subject,
            action=f"authorize:{action}",
            resource=f"{resource.type}:{resource.id}",
            outcome=outcome.decision.value,
            correlation_id=correlation_id(),
            details={
                "principal_kind": principal.kind.value,
                "via": principal.via,
                "matched_rules": list(outcome.matched_rules),
                "policy_version": outcome.policy_version,
                "policy_digest": outcome.policy_digest,
                "source": outcome.source,
            },
        )
        return outcome

    async def require(
        self,
        principal: Principal,
        action: str,
        resource: Resource,
        *,
        environment: dict[str, Any] | None = None,
    ) -> AuthzOutcome:
        """Decide and raise ``ForbiddenError`` on deny. ``require_approval`` is returned."""
        outcome = await self.decide(principal, action, resource, environment=environment)
        if outcome.decision is Decision.DENY:
            raise ForbiddenError(f"{action} denied: {outcome.reason}")
        return outcome

    async def require_allow(
        self,
        principal: Principal,
        action: str,
        resource: Resource,
        *,
        environment: dict[str, Any] | None = None,
    ) -> AuthzOutcome:
        """For actions with no approval path: anything but ``allow`` is refused."""
        outcome = await self.require(principal, action, resource, environment=environment)
        if outcome.decision is not Decision.ALLOW:
            raise ForbiddenError(f"{action} requires human approval and cannot be performed here")
        return outcome
