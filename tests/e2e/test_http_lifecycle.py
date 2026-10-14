"""Full HTTP lifecycle: screen -> queue -> claim -> decide, plus operations endpoints."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    CALCIUM,
    NOW,
    POTASSIUM,
    FakeIdp,
    agent_bearer,
    bearer,
    make_result,
    wire,
)

pytestmark = pytest.mark.e2e


def test_screen_review_decide(client: TestClient, idp: FakeIdp) -> None:
    tech = bearer(idp, "technologist", sub="tech.ruiz@lab.test")
    path = bearer(idp, "pathologist", sub="dr.okafor@lab.test")

    clean = make_result(POTASSIUM, 4.3)
    edta_k = make_result(POTASSIUM, 7.2, specimen_id="S-E2E")
    edta_ca = make_result(CALCIUM, 5.0, specimen_id="S-E2E")
    response = client.post(
        "/v1/screenings",
        json={"results": [wire(clean), wire(edta_k), wire(edta_ca)]},
        headers={**tech, "X-Correlation-ID": "corr-123"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Correlation-ID"] == "corr-123"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    body = response.json()
    assert body["held_for_review"] == 2
    assert body["screened_no_findings"] == 1
    assert all(o["released_by_agent"] is False for o in body["outcomes"])

    queue = client.get("/v1/review-queue", headers=path).json()["items"]
    assert len(queue) == 2
    assert {item["priority"] for item in queue} == {"stat"}
    case_id = queue[0]["case_id"]

    case = client.get(f"/v1/cases/{case_id}", headers=path).json()
    assert case["evidence_digest"].startswith("sha256:")
    assert case["evidence"]["catalogue"]["status"] == "starter"

    assert client.post(f"/v1/cases/{case_id}/claim", headers=path).status_code == 200
    missing_comment = client.post(
        f"/v1/cases/{case_id}/decision",
        json={"outcome": "request_recollection"},
        headers=path,
    )
    assert missing_comment.status_code == 409
    decided = client.post(
        f"/v1/cases/{case_id}/decision",
        json={"outcome": "request_recollection", "comment": "EDTA carry-over; redraw"},
        headers=path,
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "decided"
    result_id = decided.json()["result_id"]
    stored = client.get(f"/v1/results/{result_id}", headers=path).json()
    assert stored["review_outcome"] == "request_recollection"

    remaining = client.get("/v1/review-queue?status=open", headers=path).json()["items"]
    assert len(remaining) == 1
    decided_only = client.get("/v1/review-queue?status=decided", headers=path).json()["items"]
    assert [c["case_id"] for c in decided_only] == [case_id]


def test_agent_proposal_is_accepted_but_pending(client: TestClient, idp: FakeIdp) -> None:
    screener = agent_bearer(idp, "results:screen", "cases:read", "review:propose")
    held = client.post(
        "/v1/screenings",
        json={"results": [wire(make_result(POTASSIUM, 6.9, hemolysis=300))]},
        headers=screener,
    ).json()
    case_id = held["outcomes"][0]["case_id"]
    proposal = client.post(
        f"/v1/cases/{case_id}/proposals",
        json={"outcome": "request_recollection", "rationale": "gross hemolysis"},
        headers=screener,
    )
    assert proposal.status_code == 202
    assert proposal.json()["status"] == "open"
    assert proposal.json()["proposals"][0]["status"] == "pending_human_approval"
    decide = client.post(
        f"/v1/cases/{case_id}/decision", json={"outcome": "confirm"}, headers=screener
    )
    assert decide.status_code == 403
    assert decide.json()["error"] == "forbidden"


def test_trend_qc_catalogue_and_audit(client: TestClient, idp: FakeIdp) -> None:
    tech = bearer(idp, "technologist")
    for hours, value in ((40, 1.0), (20, 1.2), (2, 1.6)):
        result = make_result("2160-0", value, collected_at=NOW - timedelta(hours=hours))
        client.post("/v1/screenings", json={"results": [wire(result)]}, headers=tech)
    trend = client.get("/v1/patients/P-1001/analytes/2160-0/trend", headers=tech).json()
    assert trend["trend"]["direction"] == "rising"
    assert client.get("/v1/qc/pbrtqc", headers=tech).json()["items"] == []
    assert client.get("/v1/catalogue", headers=tech).json()["status"] == "starter"
    audit = client.post("/v1/audit/verify", headers=bearer(idp, "auditor"))
    assert audit.status_code == 200
    assert audit.json()["verified"] is True


def test_operations_endpoints(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
    client.get("/v1/review-queue")
    metrics = client.get("/metrics")
    assert "lri_http_request_duration_seconds" in metrics.text


def test_errors_use_oauth_style_bodies(client: TestClient, idp: FakeIdp) -> None:
    unauthenticated = client.get("/v1/review-queue")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
    assert unauthenticated.json()["error"] == "unauthorized"
    invalid = client.post(
        "/v1/screenings", json={"results": []}, headers=bearer(idp, "technologist")
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "invalid_request"
    missing = client.get(f"/v1/cases/{'0' * 32}", headers=bearer(idp, "pathologist"))
    assert missing.status_code == 404


def test_batch_limit_is_enforced(client: TestClient, idp: FakeIdp) -> None:
    client.app.state.container.settings.max_batch = 2  # type: ignore[attr-defined]
    batch = [wire(make_result()) for _ in range(3)]
    response = client.post(
        "/v1/screenings", json={"results": batch}, headers=bearer(idp, "technologist")
    )
    assert response.status_code == 400
