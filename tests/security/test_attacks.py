"""Adversarial tests mapped to the threat model (docs/security/threat-model.md)."""

from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from lab_result_integrity.application.service import IntegrityService
from lab_result_integrity.domain.errors import ForbiddenError
from lab_result_integrity.domain.review import ReviewOutcome
from tests.conftest import (
    AUDIENCE,
    OIDC_ISSUER,
    POTASSIUM,
    SCREENER,
    FakeIdp,
    agent_bearer,
    bearer,
    make_result,
    wire,
)

pytestmark = pytest.mark.security


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def unsigned_token() -> str:
    claims = {"sub": "x", "iss": OIDC_ISSUER, "aud": AUDIENCE, "roles": ["lab_director"]}
    return f"{_b64({'alg': 'none', 'kid': 'k1'})}.{_b64(claims)}."


@pytest.mark.parametrize(
    "token_factory",
    [
        # S1 spoofing: unsigned token (alg none)
        lambda idp: unsigned_token(),
        # S1: token signed by an attacker key with a known kid
        lambda idp: FakeIdp().mint(sub="mallory", roles=["lab_director"]),
        # S2: expired token
        lambda idp: idp.mint(sub="dr.okafor@lab.test", roles=["pathologist"], ttl=-120),
        # S3: token for another audience (confused deputy)
        lambda idp: idp.mint(
            sub="dr.okafor@lab.test", aud="another-service", roles=["pathologist"]
        ),
        # S4: wrong issuer
        lambda idp: FakeIdp(issuer="https://evil.test", key=idp.key).mint(
            sub="x", roles=["pathologist"]
        ),
        # S5: HMAC with the public key as secret (algorithm confusion)
        lambda idp: jwt.encode(
            {
                "sub": "x",
                "iss": OIDC_ISSUER,
                "aud": AUDIENCE,
                "exp": int(time.time()) + 60,
                "iat": int(time.time()),
            },
            "secret",
            algorithm="HS256",
            headers={"kid": "k1"},
        ),
    ],
)
def test_forged_tokens_are_rejected(client: TestClient, idp: FakeIdp, token_factory) -> None:  # type: ignore[no-untyped-def]
    token = token_factory(idp)
    response = client.get("/v1/review-queue", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_agent_cannot_release_or_decide_on_any_channel(client: TestClient, idp: FakeIdp) -> None:
    """E1: an over-scoped agent still cannot decide; the domain invariant beats scopes."""
    greedy = agent_bearer(idp, "results:*", "cases:*", "review:*", "result:*")
    held = client.post(
        "/v1/screenings", json={"results": [wire(make_result(POTASSIUM, 7.1))]}, headers=greedy
    ).json()
    case_id = held["outcomes"][0]["case_id"]
    assert held["outcomes"][0]["released_by_agent"] is False
    for path in (f"/v1/cases/{case_id}/claim", f"/v1/cases/{case_id}/decision"):
        response = client.post(path, json={"outcome": "confirm"}, headers=greedy)
        assert response.status_code == 403, path
    mcp_tools = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**greedy, "Accept": "application/json, text/event-stream"},
    ).json()["result"]["tools"]
    assert not {"decide_case", "release_result", "claim_case"} & {t["name"] for t in mcp_tools}


def test_role_claim_from_wrong_claim_name_is_ignored(client: TestClient, idp: FakeIdp) -> None:
    """E2: privilege escalation by stuffing roles into another claim."""
    token = idp.mint(sub="tech.ruiz@lab.test", groups=["pathologist"], roles=["technologist"])
    response = client.post(
        f"/v1/cases/{'a' * 32}/decision",
        json={"outcome": "confirm"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_user_token_cannot_masquerade_as_agent_scopes(client: TestClient, idp: FakeIdp) -> None:
    """A user token carrying scopes gets no agent allowances (agent rules require kind=agent)."""
    token = idp.mint(sub="visitor@lab.test", scp="results:screen cases:read")
    response = client.post(
        "/v1/screenings",
        json={"results": [wire(make_result())]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/v1/results/..%2F..%2Fetc%2Fpasswd",
        "/v1/results/R-1'%20OR%20'1'='1",
        "/v1/cases/ZZZZ",
        "/v1/patients/P-1/analytes/DROP%20TABLE/trend",
    ],
)
def test_identifier_injection_is_rejected(client: TestClient, idp: FakeIdp, path: str) -> None:
    """T1: identifiers are pattern-validated before any query."""
    response = client.get(path, headers=bearer(idp, "pathologist"))
    assert response.status_code in (404, 422)


def test_tampered_result_content_conflicts(client: TestClient, idp: FakeIdp) -> None:
    """T2: a resubmission cannot silently change a screened value."""
    tech = bearer(idp, "technologist")
    result = make_result(POTASSIUM, 4.2)
    assert (
        client.post("/v1/screenings", json={"results": [wire(result)]}, headers=tech).status_code
        == 200
    )
    tampered = wire(result) | {"value": 3.1}
    response = client.post("/v1/screenings", json={"results": [tampered]}, headers=tech)
    assert response.status_code == 409


@pytest.mark.parametrize(
    "mutation",
    [
        {"value": float("nan")},
        {"collected_at": "2026-10-01T10:00:00"},
        {"resulted_at": "2020-01-01T00:00:00Z"},
        {"unexpected": "field"},
        {"analyte_code": "2823-3; DROP TABLE results"},
    ],
)
def test_malformed_results_are_rejected(
    client: TestClient, idp: FakeIdp, mutation: dict[str, object]
) -> None:
    body = wire(make_result()) | mutation
    raw = json.dumps({"results": [body]}, allow_nan=True)
    response = client.post(
        "/v1/screenings",
        content=raw,
        headers={**bearer(idp, "technologist"), "Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_audit_tampering_is_detected(client: TestClient, idp: FakeIdp) -> None:
    """R1: editing a stored audit event breaks verification."""
    tech = bearer(idp, "technologist")
    client.post(
        "/v1/screenings", json={"results": [wire(make_result(POTASSIUM, 6.6))]}, headers=tech
    )
    path = client.app.state.container.settings.audit_log_path  # type: ignore[attr-defined]
    lines = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[1])
    event["outcome"] = "allow-everything"
    lines[1] = json.dumps(event)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    verification = client.post("/v1/audit/verify", headers=bearer(idp, "auditor")).json()
    assert verification["verified"] is False
    assert verification["failed_sequence"] == 1


async def test_proposals_cannot_change_state(service: IntegrityService) -> None:
    """E3: a flood of agent proposals never decides the case."""
    [outcome] = await service.screen(SCREENER, [make_result(POTASSIUM, 7.3)])
    assert outcome.case_id is not None
    for _ in range(5):
        case = await service.propose(SCREENER, outcome.case_id, ReviewOutcome.CONFIRM, "looks fine")
    assert case.status.value == "open"
    assert case.decision is None
    with pytest.raises(ForbiddenError):
        await service.decide(SCREENER, outcome.case_id, ReviewOutcome.CONFIRM, None)


def test_sensitive_headers_not_reflected(client: TestClient, idp: FakeIdp) -> None:
    response = client.get("/v1/review-queue", headers=bearer(idp, "pathologist"))
    assert response.headers["Cache-Control"] == "no-store"
    assert "authorization" not in {k.lower() for k in response.headers}
