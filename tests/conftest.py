"""Shared fixtures: an in-process OIDC issuer, a fixed clock, a container on SQLite, an app.

All data is synthetic. Patient, specimen and instrument identifiers are
generated test values and the clock is fixed so every scenario is
reproducible.
"""

from __future__ import annotations

import itertools
import os
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from lab_result_integrity.api.app import create_app
from lab_result_integrity.application.service import IntegrityService
from lab_result_integrity.config import Settings
from lab_result_integrity.domain.models import LabResult, Patient, SerumIndices, Sex
from lab_result_integrity.runtime.container import Container, build_container
from lab_result_integrity.security.auth import Principal, PrincipalKind
from lab_result_integrity.security.jwks import StaticJwks

ROOT = Path(__file__).resolve().parents[1]
OIDC_ISSUER = "https://login.lab.test/tenant"
AUDIENCE = "lab-result-integrity"
PUBLIC_HOST = "lri.lab.test"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

POTASSIUM = "2823-3"
SODIUM = "2951-2"
CALCIUM = "17861-6"
GLUCOSE = "2345-7"
CREATININE = "2160-0"
HEMOGLOBIN = "718-7"
PHOSPHATE = "2777-1"
LDH = "2532-0"

UNITS = {
    POTASSIUM: "mmol/L",
    SODIUM: "mmol/L",
    CALCIUM: "mg/dL",
    GLUCOSE: "mg/dL",
    CREATININE: "mg/dL",
    HEMOGLOBIN: "g/dL",
    PHOSPHATE: "mg/dL",
    LDH: "U/L",
    "1920-8": "U/L",
    "777-3": "10*3/uL",
    "19123-9": "mg/dL",
}


@dataclass
class FakeIdp:
    """Minimal OIDC issuer: ES256 key pair plus JWKS."""

    issuer: str = OIDC_ISSUER
    kid: str = "k1"
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "alg": "ES256", "use": "sig"})
        return {"keys": [jwk]}

    def mint(self, *, sub: str, aud: str = AUDIENCE, ttl: int = 300, **extra: Any) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": sub, "aud": aud, "iat": now, "exp": now + ttl}
        claims.update(extra)
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": self.kid})


@dataclass
class FakeClock:
    current: datetime = NOW

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


_ids = itertools.count(1)


def make_result(
    analyte: str = POTASSIUM,
    value: float = 4.2,
    *,
    patient_id: str = "P-1001",
    sex: Sex = Sex.MALE,
    birth_date: date = date(1970, 5, 1),
    collected_at: datetime | None = None,
    specimen_id: str | None = None,
    instrument_id: str = "CHEM-01",
    unit: str | None = None,
    hemolysis: float | None = None,
    icterus: float | None = None,
    lipemia: float | None = None,
    result_id: str | None = None,
) -> LabResult:
    n = next(_ids)
    collected = collected_at or NOW - timedelta(hours=1)
    return LabResult(
        result_id=result_id or f"R-{n:06d}",
        accession=f"A-{n:06d}",
        specimen_id=specimen_id or f"S-{n:06d}",
        patient=Patient(patient_id=patient_id, sex=sex, birth_date=birth_date),
        analyte_code=analyte,
        value=value,
        unit=unit or UNITS[analyte],
        collected_at=collected,
        resulted_at=collected + timedelta(minutes=40),
        instrument_id=instrument_id,
        indices=SerumIndices(hemolysis=hemolysis, icterus=icterus, lipemia=lipemia),
    )


def user(*roles: str, sub: str = "dr.okafor@lab.test") -> Principal:
    return Principal(
        subject=sub, kind=PrincipalKind.USER, roles=frozenset(roles), via="oidc", token="t"
    )


def agent(*scopes: str, sub: str = "lis-integration-agent") -> Principal:
    return Principal(
        subject=sub, kind=PrincipalKind.AGENT, scopes=frozenset(scopes), via="oidc", token="t"
    )


TECH = user("technologist", sub="tech.ruiz@lab.test")
PATHOLOGIST = user("pathologist", sub="dr.okafor@lab.test")
OTHER_PATHOLOGIST = user("pathologist", sub="dr.chen@lab.test")
DIRECTOR = user("lab_director", sub="dr.adeyemi@lab.test")
AUDITOR = user("auditor", sub="audit.kim@lab.test")
SCREENER = agent("results:screen", "results:read", "cases:read", "qc:read", "review:propose")


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    db = os.environ.get("LRI_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'lri.db'}")
    return Settings(
        environment="test",
        public_url=f"https://{PUBLIC_HOST}",
        database_url=db,
        oidc_issuer=OIDC_ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url="https://unused.invalid/jwks",
        catalogue_file=ROOT / "configs" / "analytes.yaml",
        allow_starter_catalogue=True,
        policy_file=ROOT / "policies" / "lab-result-integrity.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
    )


@pytest.fixture
async def container(settings: Settings, idp: FakeIdp, clock: FakeClock) -> AsyncIterator[Container]:
    built = await build_container(settings, oidc_resolver=StaticJwks(idp.jwks()), clock=clock)
    yield built
    await built.aclose()


@pytest.fixture
def service(container: Container) -> IntegrityService:
    return container.service


def bearer(
    idp: FakeIdp, *roles: str, sub: str = "dr.okafor@lab.test", **extra: Any
) -> dict[str, str]:
    return {"Authorization": f"Bearer {idp.mint(sub=sub, roles=list(roles), **extra)}"}


def agent_bearer(idp: FakeIdp, *scopes: str, sub: str = "lis-integration-agent") -> dict[str, str]:
    token = idp.mint(sub=sub, idtyp="app", scp=" ".join(scopes))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(settings: Settings, idp: FakeIdp, clock: FakeClock) -> Iterator[TestClient]:
    """App whose container is built inside the app's own event loop by its lifespan."""

    async def factory() -> Container:
        return await build_container(settings, oidc_resolver=StaticJwks(idp.jwks()), clock=clock)

    app = create_app(factory, public_host=PUBLIC_HOST)
    with TestClient(app, base_url=f"https://{PUBLIC_HOST}") as test_client:
        yield test_client


def wire(result: LabResult) -> dict[str, Any]:
    return result.model_dump(mode="json")
