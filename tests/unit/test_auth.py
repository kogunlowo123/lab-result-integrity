"""Authentication (OIDC and identity fabric) and combined authorization."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agentic_core.audit import AuditLog, InMemoryAuditSink
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.policy import PolicyEngine, PolicySet, Resource
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab_result_integrity.domain.errors import ForbiddenError, UnauthorizedError
from lab_result_integrity.integrations.identity_fabric import IdentityFabricClient
from lab_result_integrity.security.auth import (
    Authenticator,
    Authorizer,
    Principal,
    PrincipalKind,
    action_in_scope,
    bearer_token,
    unverified_issuer,
)
from lab_result_integrity.security.jwks import StaticJwks
from tests.conftest import AUDIENCE, OIDC_ISSUER, ROOT, FakeIdp, agent, user

FABRIC = "https://fabric.agents.test"


def fabric_transport(
    *, active: bool = True, status: str = "active", decision: str = "allow"
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/introspect":
            body: dict[str, Any] = {"active": active}
            if active:
                body.update(
                    sub="spiffe://agents.test/agent/lab/screener",
                    scope="results:screen cases:read",
                    agent_status=status,
                    iss=FABRIC,
                )
            return httpx.Response(200, json=body)
        if request.url.path == "/v1/authorize":
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "decision": decision,
                    "agent": "spiffe://agents.test/agent/lab/screener",
                    "action": payload["action"],
                    "resource": f"{payload['resource_type']}:{payload['resource_id']}",
                    "matched_rules": ["fabric-rule"],
                    "policy_version": "f1",
                    "policy_digest": "sha256:f",
                    "reason": "fabric says so",
                    "delegation_chain": [],
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def fabric_token(idp: FakeIdp) -> str:
    return FakeIdp(issuer=FABRIC, key=idp.key).mint(sub="spiffe://agents.test/agent/lab/screener")


def authenticator(idp: FakeIdp, http: httpx.AsyncClient | None = None) -> Authenticator:
    return Authenticator(
        StaticJwks(idp.jwks()),
        issuer=OIDC_ISSUER,
        audience=AUDIENCE,
        fabric=IdentityFabricClient(FABRIC, http=http) if http else None,
        fabric_issuer=FABRIC if http else None,
    )


def policy() -> PolicyEngine:
    return PolicyEngine(PolicySet.from_file(ROOT / "policies" / "lab-result-integrity.yaml"))


def audit() -> tuple[AuditLog, InMemoryAuditSink]:
    sink = InMemoryAuditSink()
    return AuditLog(sink, Ed25519PrivateKey.generate(), key_id="k"), sink


async def test_oidc_user_and_agent_tokens(idp: FakeIdp) -> None:
    auth = authenticator(idp)
    who = await auth.authenticate(
        f"Bearer {idp.mint(sub='dr.okafor@lab.test', roles=['pathologist', 7])}"
    )
    assert who.kind is PrincipalKind.USER
    assert who.roles == frozenset({"pathologist"})
    bot = await auth.authenticate(
        f"Bearer {idp.mint(sub='lis-agent', idtyp='app', scp=['results:screen'])}"
    )
    assert bot.kind is PrincipalKind.AGENT
    assert bot.scopes == frozenset({"results:screen"})
    assert bot.roles == frozenset()


async def test_fabric_credentials_are_introspected(idp: FakeIdp) -> None:
    async with httpx.AsyncClient(transport=fabric_transport()) as http:
        who = await authenticator(idp, http).authenticate(f"Bearer {fabric_token(idp)}")
    assert who.via == "fabric"
    assert who.kind is PrincipalKind.AGENT
    assert "results:screen" in who.scopes


@pytest.mark.parametrize(("active", "status"), [(False, "active"), (True, "suspended")])
async def test_inactive_or_suspended_fabric_agents_are_rejected(
    idp: FakeIdp, active: bool, status: str
) -> None:
    async with httpx.AsyncClient(transport=fabric_transport(active=active, status=status)) as http:
        with pytest.raises(UnauthorizedError):
            await authenticator(idp, http).authenticate(f"Bearer {fabric_token(idp)}")


def test_bearer_parsing_and_issuer_peek() -> None:
    for header in (None, "", "Basic abc", "Bearer ", "Bearer " + "x" * 9000):
        with pytest.raises(UnauthorizedError):
            bearer_token(header)
    assert unverified_issuer("not-a-jwt") is None
    assert unverified_issuer("a.!!!.c") is None
    assert unverified_issuer("a.WzFd.c") is None  # payload is a JSON list
    assert unverified_issuer("a.eyJpc3MiOjF9.c") is None  # iss is not a string


def test_scope_matching() -> None:
    assert action_in_scope("cases:read", frozenset({"cases:read"}))
    assert action_in_scope("cases:read", frozenset({"cases:*"}))
    assert not action_in_scope("cases:read", frozenset({"results:*", "cases"}))


async def test_local_policy_decisions_are_audited() -> None:
    log, sink = audit()
    authz = Authorizer(policy(), log)
    resource = Resource(type="review-case", classification=DataClassification.RESTRICTED)
    allowed = await authz.decide(user("pathologist"), "review:decide", resource)
    assert allowed.decision is Decision.ALLOW
    denied = await authz.decide(agent("review:decide"), "review:decide", resource)
    assert denied.decision is Decision.DENY
    assert [e.outcome for e in sink.events] == ["allow", "deny"]
    with pytest.raises(ForbiddenError):
        await authz.require(agent("results:screen"), "result:release", resource)


async def test_fabric_cannot_loosen_local_policy(idp: FakeIdp) -> None:
    log, _ = audit()
    fabric_agent = Principal(
        subject="spiffe://agents.test/agent/lab/screener",
        kind=PrincipalKind.AGENT,
        scopes=frozenset({"review:decide", "results:screen"}),
        via="fabric",
        token="tok",
    )
    resource = Resource(type="review-case", classification=DataClassification.RESTRICTED)
    async with httpx.AsyncClient(transport=fabric_transport(decision="allow")) as http:
        authz = Authorizer(policy(), log, fabric=IdentityFabricClient(FABRIC, http=http))
        local_deny = await authz.decide(fabric_agent, "review:decide", resource)
        assert local_deny.decision is Decision.DENY
        both = await authz.decide(fabric_agent, "results:screen", resource)
        assert both.decision is Decision.ALLOW
        assert both.source == "local+fabric"
    async with httpx.AsyncClient(transport=fabric_transport(decision="deny")) as http:
        authz = Authorizer(policy(), log, fabric=IdentityFabricClient(FABRIC, http=http))
        fabric_deny = await authz.decide(fabric_agent, "results:screen", resource)
        assert fabric_deny.decision is Decision.DENY
        assert fabric_deny.source == "fabric"
        assert fabric_deny.matched_rules == ("fabric:fabric-rule",)


async def test_require_allow_refuses_approval_outcomes() -> None:
    log, _ = audit()
    authz = Authorizer(policy(), log)
    resource = Resource(type="review-case")
    outcome = await authz.require(agent("review:propose"), "review:propose", resource)
    assert outcome.decision is Decision.REQUIRE_APPROVAL
    with pytest.raises(ForbiddenError, match="human approval"):
        await authz.require_allow(agent("review:propose"), "review:propose", resource)
    assert authz.policy.policy.name == "lab-result-integrity"
