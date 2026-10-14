# Lab Result Integrity

Integrity agents for clinical laboratory results. They screen numeric
results for problems the laboratory's own data can reveal, assemble a cited,
hashed evidence packet for anything suspicious, and queue it for a
pathologist. They never release, verify or autoverify a result: release
stays with the LIS's validated rules or a human.

```
Results ─▶ Trend Analysis ─▶ Interference Detection ─▶ Patient-based QC
        ─▶ Evidence Collection ─▶ Pathologist Review ─▶ Human decision
```

## Capabilities

| Capability | Implementation |
|---|---|
| Delta checks | Most recent prior result inside a per-analyte window; absolute and/or percent limits, `any` or `all` |
| Trend analysis | Least-squares slope (units/day) over the analyte's trend window |
| Reference and critical ranges | Age bands and sex; unknown sex evaluated against the narrowest combined interval; critical limits make cases `stat` |
| Interference detection | HIL (hemolysis, icterus, lipemia) index rules per analyte with bias direction and flag/withhold action |
| Implausible combinations | Same-specimen rules, e.g. hemolysis with high potassium, K2EDTA carry-over (high K, low Ca), delayed separation, dextrose-line contamination |
| Plausibility and units | Physiologically implausible values and unit mismatches hold the result |
| Patient-based real-time QC | SMA or EWMA over truncated patient results per instrument and analyte; alarm, look-back list, recovery; shared across replicas |
| Evidence packet | Result, prior results, findings with citation text, specimen results, QC snapshot, catalogue digest, provenance record, SHA-256 digest |
| Pathologist review queue | Priority and SLA from the catalogue, claims, director override, decisions bound to the evidence digest, escalation of overdue cases |
| Agent proposals | Agents may propose a disposition; recorded as `pending_human_approval`, never changes state |
| Governed catalogue | Every threshold cites a source; production refuses an unapproved catalogue |
| Never-release invariant | No route, tool or skill releases; the service refuses to start under a policy that would let an agent release or decide |
| MCP | Official `mcp` SDK (FastMCP, streamable HTTP) at `/mcp`, seven typed tools, authenticated, authorized and audited |
| A2A | Agent Card at `/.well-known/agent-card.json`; JSON-RPC `message/send` at `/a2a` |
| Identity | Enterprise OIDC and agent-identity-fabric credentials; local policy plus fabric authorization, most restrictive wins |
| Narrative (optional) | De-identified, advisory draft via the agentic-core model router with ContentGuard on input and output |
| Audit | Hash-chained, Ed25519-signed events per replica; verification endpoint |
| Observability | Prometheus metrics, OpenTelemetry traces, JSON logs with correlation IDs, SLO alerts, Grafana dashboard |

Shared controls (policy engine, audit chain, model router, content guard,
logging, telemetry) come from
[agentic-core](https://github.com/kogunlowo123/agentic-core).

## Analyte thresholds

All thresholds live in [configs/analytes.yaml](configs/analytes.yaml). The
shipped catalogue is a **starter**: typical values chosen so the rules run on
synthetic data, each tagged with the source category it should be verified
against (CLSI EP28, GP47, EP33, C56, EP07, Badrick et al. 2019, 42 CFR 493).
They are not clinical guidance. A laboratory replaces them with its own
verified values and marks the catalogue `approved`; the service refuses a
starter catalogue in production. See
[ADR 0003](docs/architecture/adr/0003-governed-analyte-catalogue.md).

## Quick start (local)

Requires Docker. No API keys or cloud credentials are needed; all data is
synthetic.

```bash
docker compose up --build
curl -s localhost:8080/.well-known/agent-card.json | jq .skills
```

The compose stack runs PostgreSQL, an OIDC provider standing in for the
enterprise IdP, and an OpenTelemetry collector.

## Development

```bash
uv sync
make check        # ruff, black, mypy --strict, bandit, contracts, tests (coverage >= 90%)
make integration  # PostgreSQL integration tests (needs LRI_TEST_DATABASE_URL)
make helm terraform audit-deps
```

Test suites: `tests/unit` (rules, catalogue, PBRTQC, review state machine,
service, auth, fabric client, container), `tests/security` (attacks mapped to
the threat model), `tests/chaos` (dependency failures, contention, resume),
`tests/performance` (latency budgets; `locustfile.py` for load),
`tests/e2e` (HTTP lifecycle, MCP, A2A, property-based OpenAPI contract test),
`tests/integration` (PostgreSQL).

## API

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /v1/screenings` | LIS middleware, agents (`results:screen`), technologists | Screen a batch; returns dispositions, findings, case IDs |
| `GET /v1/results/{id}` | Staff, agents (`results:read`) | Result with outcome and review state |
| `GET /v1/patients/{id}/analytes/{loinc}/trend` | Staff, agents | Prior results and trend |
| `GET /v1/review-queue` | Staff, agents (`cases:read`) | Cases by priority and due time |
| `GET /v1/cases/{id}` | Staff, agents | Case with evidence packet |
| `POST /v1/cases/{id}/claim`, `/decision` | Pathologist, lab director | Human review |
| `POST /v1/cases/{id}/proposals` | Agents (`review:propose`) | Proposal pending human approval (202) |
| `GET /v1/qc/pbrtqc` | Staff, agents (`qc:read`) | Moving-statistic state per instrument and analyte |
| `GET /v1/catalogue` | Staff, agents | Catalogue governance, digest, sources |
| `POST /v1/audit/verify` | Auditor, lab director | Verify this replica's audit chain |
| `/mcp` | MCP clients | Tools: `screen_results`, `get_result`, `patient_trend`, `review_queue`, `get_case`, `propose_disposition`, `pbrtqc_status` |
| `/.well-known/agent-card.json`, `POST /a2a` | A2A peers | Skills: `screen-results`, `review-queue`, `get-case`, `propose-disposition` |

Full contract: [docs/api/openapi.json](docs/api/openapi.json); JSON Schemas in [schemas/](schemas/).

## Deployment

| Layer | Location |
|---|---|
| Container image | `Dockerfile` (non-root uid 10001, read-only root filesystem compatible) |
| Helm chart | `deployments/helm/lab-result-integrity` (StatefulSet, PDB, HPA, NetworkPolicy, ServiceMonitor; approved catalogue required) |
| Namespace and secret sync | `deployments/kubernetes` (restricted Pod Security, External Secrets) |
| Cloud infrastructure | `deployments/terraform/gcp` (primary: Cloud SQL, Cloud KMS, Secret Manager, retention-locked GCS, GKE Workload Identity), plus `aws` and `azure` |

See the [start-up runbook](docs/runbooks/startup.md).

## Documentation

- [Architecture](docs/architecture/overview.md) and [agent specification](docs/architecture/agent-specification.md)
- [ADRs](docs/architecture/adr/)
- [Threat model and risk register](docs/security/threat-model.md)
- [Compliance control mapping](docs/compliance/control-mapping.md)
- Runbooks: [startup](docs/runbooks/startup.md), [monitoring](docs/runbooks/monitoring.md), [incident response](docs/runbooks/incident-response.md), [PBRTQC alarm](docs/runbooks/pbrtqc-alarm.md), [review SLA breach](docs/runbooks/review-sla-breach.md), [catalogue change](docs/runbooks/catalogue-change.md), [rollback](docs/runbooks/rollback.md), [disaster recovery](docs/runbooks/disaster-recovery.md)
- [Configuration reference](docs/operations/configuration.md)

## License

Apache-2.0
