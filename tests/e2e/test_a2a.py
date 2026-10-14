"""A2A: Agent Card discovery and JSON-RPC message/send routed to the integrity workflow."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import POTASSIUM, FakeIdp, agent_bearer, make_result, wire

pytestmark = pytest.mark.e2e


def send(
    client: TestClient, headers: dict[str, str], data: dict[str, Any] | None, rid: Any = 7
) -> Any:
    parts: list[dict[str, Any]] = [{"kind": "text", "text": "please run"}]
    if data is not None:
        parts.append({"kind": "data", "data": data})
    body = {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "message/send",
        "params": {
            "message": {"role": "user", "messageId": "m-1", "kind": "message", "parts": parts}
        },
    }
    response = client.post("/a2a", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def artifact(reply: dict[str, Any]) -> Any:
    return reply["result"]["artifacts"][0]["parts"][0]["data"]


def test_agent_card(client: TestClient) -> None:
    card = client.get("/.well-known/agent-card.json").json()
    for field in (
        "name",
        "description",
        "url",
        "version",
        "capabilities",
        "skills",
        "securitySchemes",
    ):
        assert field in card
    assert card["url"] == "https://lri.lab.test/a2a"
    assert {s["id"] for s in card["skills"]} == {
        "screen-results",
        "review-queue",
        "get-case",
        "propose-disposition",
    }
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"


def test_message_send_runs_the_workflow(client: TestClient, idp: FakeIdp) -> None:
    headers = agent_bearer(idp, "results:screen", "cases:read", "review:propose")
    reply = send(
        client,
        headers,
        {"skill": "screen-results", "results": [wire(make_result(POTASSIUM, 7.0))]},
    )
    assert reply["id"] == 7
    assert reply["result"]["kind"] == "task"
    assert reply["result"]["status"]["state"] == "completed"
    outcome = artifact(reply)["outcomes"][0]
    assert outcome["disposition"] == "held_for_review"

    queue = artifact(send(client, headers, {"skill": "review-queue", "limit": 5}))
    assert queue["items"][0]["case_id"] == outcome["case_id"]
    case = artifact(send(client, headers, {"skill": "get-case", "case_id": outcome["case_id"]}))
    assert case["priority"] == "stat"
    proposal = artifact(
        send(
            client,
            headers,
            {
                "skill": "propose-disposition",
                "case_id": outcome["case_id"],
                "outcome": "confirm",
                "rationale": "repeat on second analyzer agrees",
            },
        )
    )
    assert proposal["policy_decision"] == "require_approval"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (None, -32602),
        ({"skill": "release-result"}, -32602),
        ({"skill": "screen-results"}, -32602),
        ({"skill": "screen-results", "results": [{"value": 1}]}, -32602),
        ({"skill": "get-case"}, -32602),
        ({"skill": "propose-disposition", "case_id": "0" * 32}, -32602),
        ({"skill": "get-case", "case_id": "0" * 32}, -32041),
        ({"skill": "get-case", "case_id": "not-hex"}, -32602),
    ],
)
def test_invalid_params(client: TestClient, idp: FakeIdp, data: Any, code: int) -> None:
    headers = agent_bearer(idp, "results:screen", "cases:read", "review:propose")
    assert send(client, headers, data)["error"]["code"] == code


def test_protocol_errors(client: TestClient, idp: FakeIdp) -> None:
    headers = agent_bearer(idp, "results:screen")
    parse = client.post("/a2a", content=b"{not json", headers=headers).json()
    assert parse["error"]["code"] == -32700
    invalid = client.post("/a2a", json={"jsonrpc": "1.0", "method": "x"}, headers=headers).json()
    assert invalid["error"]["code"] == -32600
    unknown = client.post(
        "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel"}, headers=headers
    ).json()
    assert unknown["error"]["code"] == -32601
    batch = client.post("/a2a", json=[1, 2], headers=headers).json()
    assert batch["error"]["code"] == -32600


def test_authorization_on_every_skill(client: TestClient, idp: FakeIdp) -> None:
    reader = agent_bearer(idp, "cases:read")
    reply = send(client, reader, {"skill": "screen-results", "results": [wire(make_result())]})
    assert reply["error"]["code"] == -32040
    assert client.post("/a2a", json={}).status_code == 401
