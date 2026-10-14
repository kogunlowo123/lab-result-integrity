"""Dependency container built once per process from Settings (composition root)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from agentic_core.audit import AuditLog, JsonlAuditSink
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.models.factory import ProvidersConfig, build_providers
from agentic_core.models.router import ModelRouter
from agentic_core.policy import PolicyEngine, PolicyRequest, PolicySet, Resource, Subject
from agentic_core.security import ContentGuard
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncEngine

from lab_result_integrity.agents.analysis import (
    InterferenceDetectionAgent,
    PatientBasedQcAgent,
    TrendAnalysisAgent,
)
from lab_result_integrity.agents.evidence import EvidenceCollectionAgent, NarrativeDrafter
from lab_result_integrity.agents.pipeline import ScreeningPipeline
from lab_result_integrity.application.service import IntegrityService
from lab_result_integrity.config import Settings
from lab_result_integrity.domain.catalogue import AnalyteCatalogue
from lab_result_integrity.domain.errors import CatalogueError
from lab_result_integrity.domain.ports import Clock
from lab_result_integrity.infrastructure.database import (
    SqlCaseRepository,
    SqlPbrtqcStore,
    SqlResultRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from lab_result_integrity.integrations.identity_fabric import IdentityFabricClient
from lab_result_integrity.observability.metrics import IntegrityMetrics
from lab_result_integrity.security.auth import Authenticator, Authorizer
from lab_result_integrity.security.jwks import KeyResolver, RemoteJwks

# Actions no agent may ever be allowed to perform. Checked against the loaded
# policy at start-up so a policy edit cannot silently grant autoverification.
FORBIDDEN_AGENT_ACTIONS = (
    "result:release",
    "result:autoverify",
    "result:verify",
    "review:claim",
    "review:decide",
    "review:override",
)


@dataclass
class Container:
    settings: Settings
    service: IntegrityService
    authenticator: Authenticator
    metrics: IntegrityMetrics
    engine: AsyncEngine
    http: httpx.AsyncClient
    router: ModelRouter | None

    async def aclose(self) -> None:
        if self.router is not None:
            await self.router.aclose()
        await self.http.aclose()
        await self.engine.dispose()


def assert_policy_invariants(engine: PolicyEngine) -> None:
    """Refuse a policy under which a fully scoped agent could release or decide results."""
    agent = Subject(
        id="policy-self-test",
        kind="agent",
        roles=frozenset({"pathologist", "lab_director"}),
        attributes={"scopes": ["*"], "action_in_scope": True, "via": "fabric"},
    )
    for action in FORBIDDEN_AGENT_ACTIONS:
        decision = engine.evaluate(
            PolicyRequest(
                subject=agent,
                action=action,
                resource=Resource(type="review-case", classification=DataClassification.RESTRICTED),
            )
        )
        if decision.decision is not Decision.DENY:
            raise CatalogueError(
                f"policy {engine.policy.name} {engine.policy.version} does not deny {action} "
                "to agents; refusing to start"
            )


def _audit_signer(settings: Settings) -> tuple[Ed25519PrivateKey, str]:
    if settings.audit_key_file is None:
        return Ed25519PrivateKey.generate(), "audit-ephemeral"
    key = serialization.load_pem_private_key(settings.audit_key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("audit key must be Ed25519")
    return key, settings.audit_key_file.stem


async def build_container(
    settings: Settings,
    *,
    oidc_resolver: KeyResolver | None = None,
    clock: Clock | None = None,
    http: httpx.AsyncClient | None = None,
    model_transport: httpx.AsyncBaseTransport | None = None,
    providers_env: Mapping[str, str] | None = None,
) -> Container:
    catalogue = AnalyteCatalogue.load(
        settings.catalogue_file, allow_starter=settings.allow_starter_catalogue
    )
    policy = PolicyEngine(PolicySet.from_file(settings.policy_file))
    assert_policy_invariants(policy)

    router: ModelRouter | None = None
    drafter: NarrativeDrafter | None = None
    if settings.providers_file is not None:
        config = ProvidersConfig.from_file(settings.providers_file, env=providers_env)
        router = ModelRouter(build_providers(config, env=providers_env, transport=model_transport))
        drafter = NarrativeDrafter(router, ContentGuard())

    http = http or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    clock = clock or SystemClock()
    engine = make_engine(settings.database_url)
    await create_schema(engine)

    signer, key_id = _audit_signer(settings)
    sink = JsonlAuditSink(settings.audit_log_path)
    audit = AuditLog(sink, signer, key_id=key_id)
    metrics = IntegrityMetrics()

    fabric = (
        IdentityFabricClient(settings.fabric_base, http=http)
        if settings.fabric_base is not None
        else None
    )
    authenticator = Authenticator(
        oidc_resolver or RemoteJwks(settings.oidc_jwks_url, client=http),
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        roles_claim=settings.roles_claim,
        agent_claim=settings.agent_claim,
        agent_claim_value=settings.agent_claim_value,
        fabric=fabric,
        fabric_issuer=settings.fabric_issuer_value,
    )
    results = SqlResultRepository(engine)
    cases = SqlCaseRepository(engine)
    pbrtqc = SqlPbrtqcStore(engine)
    pipeline = ScreeningPipeline(
        catalogue=catalogue,
        results=results,
        cases=cases,
        trend=TrendAnalysisAgent(),
        interference=InterferenceDetectionAgent(catalogue),
        qc=PatientBasedQcAgent(pbrtqc, audit),
        evidence=EvidenceCollectionAgent(
            catalogue,
            drafter=drafter,
            prior_limit=catalogue.review_policy.prior_results_in_evidence,
        ),
        audit=audit,
        clock=clock,
        metrics=metrics,
    )
    service = IntegrityService(
        pipeline=pipeline,
        results=results,
        cases=cases,
        pbrtqc=pbrtqc,
        authorizer=Authorizer(policy, audit, fabric=fabric),
        audit=audit,
        audit_sink=sink,
        audit_keys={key_id: signer.public_key()},
        clock=clock,
        metrics=metrics,
    )
    return Container(
        settings=settings,
        service=service,
        authenticator=authenticator,
        metrics=metrics,
        engine=engine,
        http=http,
        router=router,
    )
