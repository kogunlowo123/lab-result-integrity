# Configuration reference

All settings are environment variables with the `LRI_` prefix.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `LRI_ENVIRONMENT` | | `production` | `production` enables the guards below |
| `LRI_PUBLIC_URL` | yes | | External base URL; Agent Card `url` and MCP allowed host |
| `LRI_DATABASE_URL` | yes | | SQLAlchemy async URL (`postgresql+asyncpg://...`) |
| `LRI_OIDC_ISSUER` | yes | | Enterprise OIDC issuer |
| `LRI_OIDC_AUDIENCE` | yes | | Audience tokens must carry |
| `LRI_OIDC_JWKS_URL` | yes | | Issuer JWKS URL |
| `LRI_ROLES_CLAIM` | | `roles` | Claim holding user roles |
| `LRI_AGENT_CLAIM` / `LRI_AGENT_CLAIM_VALUE` | | `idtyp` / `app` | Marks a token as an agent (Entra ID app-only convention) |
| `LRI_FABRIC_URL` | | | agent-identity-fabric base URL; enables fabric credentials |
| `LRI_FABRIC_ISSUER` | | `LRI_FABRIC_URL` | `iss` of fabric credentials |
| `LRI_CATALOGUE_FILE` | | `configs/analytes.yaml` | Analyte catalogue path |
| `LRI_ALLOW_STARTER_CATALOGUE` | | `false` | Accept `status: starter`; refused in production |
| `LRI_POLICY_FILE` | | `policies/lab-result-integrity.yaml` | Policy; must pass the start-up self-test |
| `LRI_PROVIDERS_FILE` | | | Enables narratives; agentic-core providers file |
| `LRI_MODEL_GATEWAY_URL`, `LRI_NARRATIVE_MODEL` | with providers file | | Referenced by `configs/providers.yaml`; unset values fail start-up |
| `LRI_AUDIT_LOG_PATH` | | `/var/lib/lab-result-integrity/audit/audit.jsonl` | Audit chain file (per replica) |
| `LRI_AUDIT_KEY_FILE` | in production | | Ed25519 PEM; file stem is the key id |
| `LRI_MAX_BATCH` | | `500` | Maximum results per screening request |
| `LRI_SLA_SWEEP_INTERVAL_S` | | `60` | Escalation sweep interval |
| `LRI_LOG_LEVEL` | | `INFO` | |
| `LRI_OTLP_ENDPOINT` | | | OTLP gRPC endpoint for traces |
| `LRI_HOST`, `LRI_PORT` | | `127.0.0.1`, `8080` | Bind address (image sets `0.0.0.0`) |
| `LRI_FORWARDED_ALLOW_IPS` | | `127.0.0.1` | Trusted proxy addresses |

Start-up fails (and the pod never becomes ready) when: the catalogue is not
approved and starter catalogues are not allowed; the policy would let an agent
release, autoverify, claim or decide; the providers file references unset
variables; the audit key is not Ed25519.
