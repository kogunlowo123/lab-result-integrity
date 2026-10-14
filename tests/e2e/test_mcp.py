"""MCP over streamable HTTP: discovery, typed schemas, authentication, authorization, audit."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import POTASSIUM, FakeIdp, agent_bearer, bearer, make_result, wire

pytestmark = pytest.mark.e2e

ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(
    client: TestClient,
    headers: dict[str, str],
    method: str,
    params: dict[str, Any] | None = None,
    rid: int = 1,
) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post("/mcp", json=body, headers={**ACCEPT, **headers})
    assert response.status_code == 200, response.text
    return response.json()


def call(client: TestClient, headers: dict[str, str], tool: str, **arguments: Any) -> Any:
    return rpc(client, headers, "tools/call", {"name": tool, "arguments": arguments})["result"]


def test_initialize_and_list_tools(client: TestClient, idp: FakeIdp) -> None:
    headers = agent_bearer(idp, "results:screen")
    init = rpc(
        client,
        headers,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "lis-agent", "version": "1"},
        },
    )
    assert init["result"]["serverInfo"]["name"] == "lab-result-integrity"
    tools = {t["name"]: t for t in rpc(client, headers, "tools/list")["result"]["tools"]}
    assert set(tools) == {
        "screen_results",
        "get_result",
        "patient_trend",
        "review_queue",
        "get_case",
        "propose_disposition",
        "pbrtqc_status",
    }
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object"
        assert "outputSchema" in tool
    assert not any("release" in name or "verify" in name for name in tools)


def test_screen_and_propose_through_mcp(client: TestClient, idp: FakeIdp) -> None:
    headers = agent_bearer(idp, "results:screen", "cases:read", "review:propose")
    screened = call(
        client,
        headers,
        "screen_results",
        results=[wire(make_result(POTASSIUM, 6.9, hemolysis=260))],
    )
    assert not screened["isError"]
    outcome = screened["structuredContent"]["outcomes"][0]
    assert outcome["disposition"] == "held_for_review"
    assert outcome["released_by_agent"] is False

    queue = call(client, headers, "review_queue", limit=5)["structuredContent"]
    assert queue["items"][0]["case_id"] == outcome["case_id"]
    case = call(client, headers, "get_case", case_id=outcome["case_id"])["structuredContent"]
    assert case["evidence_digest"].startswith("sha256:")

    receipt = call(
        client,
        headers,
        "propose_disposition",
        case_id=outcome["case_id"],
        outcome="request_recollection",
        rationale="hemolysis index 260",
    )["structuredContent"]
    assert receipt["policy_decision"] == "require_approval"
    assert receipt["case_status"] == "open"


def test_tool_authorization_is_enforced(client: TestClient, idp: FakeIdp) -> None:
    reader = agent_bearer(idp, "cases:read")
    denied = call(client, reader, "screen_results", results=[wire(make_result())])
    assert denied["isError"]
    assert "forbidden" in denied["content"][0]["text"]
    missing = call(client, reader, "get_case", case_id="0" * 32)
    assert missing["isError"]
    assert "forbidden" in call(client, reader, "pbrtqc_status")["content"][0]["text"]
    reader_with_qc = agent_bearer(idp, "qc:read", "results:read")
    assert call(client, reader_with_qc, "pbrtqc_status")["structuredContent"] == {"items": []}
    trend = call(client, reader_with_qc, "patient_trend", patient_id="P-1", analyte_code=POTASSIUM)
    assert trend["structuredContent"]["trend"]["points"] == 0
    assert call(client, reader_with_qc, "get_result", result_id="R-none")["isError"]


def test_unauthenticated_requests_never_reach_mcp(client: TestClient, idp: FakeIdp) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = client.post("/mcp", json=body, headers=ACCEPT)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    forged = client.post(
        "/mcp", json=body, headers={**ACCEPT, "Authorization": "Bearer abc.def.ghi"}
    )
    assert forged.status_code == 401


def test_dns_rebinding_protection(client: TestClient, idp: FakeIdp) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = client.post(
        "/mcp",
        json=body,
        headers={**ACCEPT, **bearer(idp, "technologist"), "Host": "attacker.example"},
    )
    assert response.status_code in (400, 421)


def test_tool_calls_are_audited(client: TestClient, idp: FakeIdp) -> None:
    headers = agent_bearer(idp, "qc:read")
    call(client, headers, "pbrtqc_status")
    log = client.app.state.container.settings.audit_log_path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert '"action":"mcp.pbrtqc_status"' in log
