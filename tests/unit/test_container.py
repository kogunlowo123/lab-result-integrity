"""Composition root, settings guards, policy self-test and the optional narrative drafter."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from lab_result_integrity.config import Settings
from lab_result_integrity.domain.errors import CatalogueError
from lab_result_integrity.domain.models import Disposition
from lab_result_integrity.runtime.container import build_container
from lab_result_integrity.security.jwks import StaticJwks
from tests.conftest import (
    AUDITOR,
    PATHOLOGIST,
    POTASSIUM,
    ROOT,
    TECH,
    FakeClock,
    FakeIdp,
    make_result,
)

MODEL_ENV = {"LRI_MODEL_GATEWAY_URL": "https://gateway.ci.internal", "LRI_NARRATIVE_MODEL": "m1"}


def settings_with(settings: Settings, **changes: object) -> Settings:
    return settings.model_copy(update=changes)


def test_production_guards(settings: Settings) -> None:
    base = settings.model_dump()
    with pytest.raises(ValidationError, match="allow_starter_catalogue"):
        Settings(**{**base, "environment": "production"})
    with pytest.raises(ValidationError, match="audit_key_file"):
        Settings(**{**base, "environment": "production", "allow_starter_catalogue": False})
    with pytest.raises(ValidationError, match="fabric_issuer requires"):
        Settings(**{**base, "fabric_issuer": "https://fabric.test"})
    fabric = Settings(**{**base, "fabric_url": "https://fabric.agents.test/"})
    assert fabric.fabric_issuer_value == "https://fabric.agents.test"
    assert settings.public_host == "lri.lab.test"


async def test_policy_that_lets_agents_release_is_refused(
    settings: Settings, tmp_path: Path, idp: FakeIdp
) -> None:
    permissive = tmp_path / "policy.yaml"
    permissive.write_text(
        "name: unsafe\nversion: '1'\nrules:\n"
        "  - {id: allow-all, effect: allow, actions: ['*']}\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogueError, match="does not deny result:release"):
        await build_container(
            settings_with(settings, policy_file=permissive), oidc_resolver=StaticJwks(idp.jwks())
        )


async def test_starter_catalogue_needs_explicit_allowance(settings: Settings, idp: FakeIdp) -> None:
    with pytest.raises(CatalogueError):
        await build_container(
            settings_with(settings, allow_starter_catalogue=False),
            oidc_resolver=StaticJwks(idp.jwks()),
        )


async def test_audit_key_file_is_used(settings: Settings, tmp_path: Path, idp: FakeIdp) -> None:
    key_file = tmp_path / "audit-2026-10.pem"
    key_file.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    built = await build_container(
        settings_with(settings, audit_key_file=key_file), oidc_resolver=StaticJwks(idp.jwks())
    )
    try:
        await built.service.screen(TECH, [make_result()])
        assert (await built.service.verify_audit(AUDITOR)).verified
        first = json.loads(settings.audit_log_path.read_text(encoding="utf-8").splitlines()[0])
        assert first["key_id"] == "audit-2026-10"
    finally:
        await built.aclose()

    wrong = tmp_path / "audit-ec.pem"
    from cryptography.hazmat.primitives.asymmetric import ec

    wrong.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError, match="Ed25519"):
        await build_container(
            settings_with(settings, audit_key_file=wrong), oidc_resolver=StaticJwks(idp.jwks())
        )


def model_transport(text: str, status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][-1]["content"]
        assert "P-1001" not in prompt  # de-identified
        assert "1970" not in prompt
        if status != 200:
            return httpx.Response(status, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            },
        )

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("text", "status", "expected"),
    [
        (
            "Potassium 6.8 mmol/L meets the critical high limit; hemolysis index not reported.",
            200,
            "drafted",
        ),
        ("key " + "AKIA" + "Z" * 16, 200, "blocked"),
        ("", 400, "unavailable"),
    ],
)
async def test_evidence_narrative(
    settings: Settings, idp: FakeIdp, clock: FakeClock, text: str, status: int, expected: str
) -> None:
    built = await build_container(
        settings_with(settings, providers_file=ROOT / "configs" / "providers.yaml"),
        oidc_resolver=StaticJwks(idp.jwks()),
        clock=clock,
        model_transport=model_transport(text, status),
        providers_env=MODEL_ENV,
    )
    try:
        [outcome] = await built.service.screen(TECH, [make_result(POTASSIUM, 6.8)])
        assert outcome.disposition is Disposition.HELD_FOR_REVIEW
        assert outcome.case_id is not None
        case = await built.service.case(PATHOLOGIST, outcome.case_id)
        narrative = case.evidence["narrative"]
        assert narrative["status"] == expected
        assert narrative["advisory"] is True
        if expected == "drafted":
            assert narrative["provider"] == "enterprise-model-gateway"
    finally:
        await built.aclose()


async def test_provider_config_fails_closed_without_environment(
    settings: Settings, idp: FakeIdp
) -> None:
    with pytest.raises(ValueError, match="LRI_MODEL_GATEWAY_URL"):
        await build_container(
            settings_with(settings, providers_file=ROOT / "configs" / "providers.yaml"),
            oidc_resolver=StaticJwks(idp.jwks()),
            providers_env={},
        )
