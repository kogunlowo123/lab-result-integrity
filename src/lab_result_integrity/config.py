"""Runtime settings, read from LRI_* environment variables."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LRI_", env_nested_delimiter="__")

    environment: str = "production"
    public_url: HttpUrl = Field(description="External base URL; used in the A2A Agent Card")
    database_url: str = Field(description="SQLAlchemy async URL, e.g. postgresql+asyncpg://...")

    oidc_issuer: str = Field(description="Enterprise OIDC issuer for staff and agent clients")
    oidc_audience: str = Field(description="Audience tokens for this service must carry")
    oidc_jwks_url: str
    roles_claim: str = "roles"
    agent_claim: str = Field(default="idtyp", description="Claim that marks an agent token")
    agent_claim_value: str = "app"

    fabric_url: HttpUrl | None = Field(
        default=None, description="agent-identity-fabric base URL; enables fabric credentials"
    )
    fabric_issuer: str | None = Field(
        default=None, description="iss of fabric credentials; defaults to fabric_url"
    )

    catalogue_file: Path = Path("configs/analytes.yaml")
    allow_starter_catalogue: bool = Field(
        default=False,
        description="Accept a catalogue whose status is 'starter'. Refused in production.",
    )
    policy_file: Path = Path("policies/lab-result-integrity.yaml")
    providers_file: Path | None = Field(
        default=None, description="agentic-core providers.yaml; enables evidence narratives"
    )

    audit_log_path: Path = Path("/var/lib/lab-result-integrity/audit/audit.jsonl")
    audit_key_file: Path | None = Field(
        default=None, description="Ed25519 PEM key for the audit chain; required in production"
    )

    max_batch: int = Field(default=500, ge=1, le=5000)
    sla_sweep_interval_s: float = Field(default=60.0, gt=0)
    log_level: str = "INFO"
    otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.environment == "production":
            if self.allow_starter_catalogue:
                raise ValueError("allow_starter_catalogue cannot be used in production")
            if self.audit_key_file is None:
                raise ValueError("audit_key_file is required in production")
        if self.fabric_issuer is not None and self.fabric_url is None:
            raise ValueError("fabric_issuer requires fabric_url")
        return self

    @property
    def public_base(self) -> str:
        return str(self.public_url).rstrip("/")

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_base).netloc

    @property
    def fabric_base(self) -> str | None:
        return str(self.fabric_url).rstrip("/") if self.fabric_url is not None else None

    @property
    def fabric_issuer_value(self) -> str | None:
        return self.fabric_issuer or self.fabric_base
