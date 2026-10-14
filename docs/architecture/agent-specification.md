# Agent specification

## Role and mission

The lab result integrity agents screen numeric laboratory results for
integrity problems before they are released, assemble the evidence, and route
anything suspicious to a pathologist. Mission: no integrity failure that the
laboratory's own data can reveal reaches a clinician unreviewed, and no result
is ever released on an agent's judgement.

| Agent | Identity (audit actor) | Responsibility |
|---|---|---|
| Screening orchestrator | `lab-result-integrity/screening-orchestrator` | Idempotent intake, ordering, disposition |
| Trend analysis | `lab-result-integrity/trend-analysis` | Delta checks, trends |
| Interference detection | `lab-result-integrity/interference-detection` | HIL rules, combinations, plausibility |
| Patient-based QC | `lab-result-integrity/patient-based-qc` | Moving statistics, alarms, recovery |
| Evidence collection | `lab-result-integrity/evidence-collection` | Packet, provenance, optional narrative |
| Review SLA monitor | `lab-result-integrity/review-sla-monitor` | Escalates overdue cases |

## Delegated scope

| | |
|---|---|
| **Allowed** | Read the governed analyte catalogue; read the patient's prior results for the analyte and the specimen's other results; maintain PBRTQC state; open review cases; record agent proposals; escalate overdue cases; draft an advisory, de-identified narrative |
| **Disallowed** | Releasing, verifying or autoverifying results; claiming or deciding review cases; editing results or the catalogue; sending identifiers or birth dates to a model; acting on a starter catalogue in production |
| **Risk tier** | High: outputs influence whether a result reaches a clinician. Mitigated by never-release, human decision, cited evidence and audit |
| **Escalation** | Critical findings open `stat` cases (SLA from catalogue, starter 30 min); overdue cases are escalated and alert `LriReviewCasesOverdue`; PBRTQC alarms page the laboratory on call (`LriPatientBasedQcAlarm`) |

## Objectives

| Type | Definition | Target |
|---|---|---|
| SLO availability | non-5xx share of requests | 99.9% monthly |
| SLO latency | p95 `POST /v1/screenings` (batch up to 50) | < 1 s |
| SLO review | undecided cases past SLA for more than 15 min | 0 |
| SLI | `lri_http_request_duration_seconds`, `lri_review_cases_overdue` | |
| KPI | hold rate, decisions by outcome (share of holds confirmed vs. recollected), PBRTQC alarms per instrument, time to decision | trended, reviewed monthly by the laboratory director |
| Completion | every submitted result has an outcome; every held result has exactly one case with an evidence digest; every case ends with a human decision | |

## Permissions

**Identity.** Staff authenticate with the enterprise OIDC provider; agents
either as OIDC confidential clients (app-only tokens, `idtyp=app`) or with
credentials from agent-identity-fabric (SPIFFE IDs, introspected per request).
The service itself runs with GKE Workload Identity (no static cloud keys).

**RBAC (users, OIDC `roles` claim).**

| Role | Actions |
|---|---|
| `technologist` | `results:screen`, `results:read`, `cases:read`, `qc:read`, `catalogue:read` |
| `pathologist` | reads, `review:claim`, `review:decide` |
| `lab_director` | as pathologist, plus `review:override`, `audit:verify` |
| `auditor` | reads, `audit:verify` |

**ABAC (agents).** An agent action is allowed only when the credential
carries it as a scope (exact or trailing wildcard, `cases:*`). `review:propose`
returns `require_approval`. `review:claim`, `review:decide`, `review:override`
are denied to any agent; `result:release`, `result:autoverify` and
`result:verify` are denied to everyone. Fabric credentials are also
authorized by the fabric; the most restrictive outcome wins.

**OAuth/OIDC.** Bearer JWTs verified for signature (RS256, ES256, EdDSA;
`none` and HMAC rejected), `iss`, `aud`, `exp`, `iat`, `sub`. JWKS cached with
rate-limited refresh. Fabric tokens: RFC 7662 introspection; inactive or
suspended agents rejected.

## Inputs and outputs

| Contract | Schema |
|---|---|
| Lab result | [schemas/lab-result.schema.json](../../schemas/lab-result.schema.json) |
| Screen request | [schemas/screen.request.schema.json](../../schemas/screen.request.schema.json) |
| Screening outcome | [schemas/screening-outcome.schema.json](../../schemas/screening-outcome.schema.json) |
| Evidence packet | [schemas/evidence-packet.schema.json](../../schemas/evidence-packet.schema.json) |
| Review case | [schemas/review-case.schema.json](../../schemas/review-case.schema.json) |
| Decision / proposal | [decision.request](../../schemas/decision.request.schema.json), [proposal.request](../../schemas/proposal.request.schema.json) |
| Analyte catalogue | [schemas/analyte-catalogue.schema.json](../../schemas/analyte-catalogue.schema.json) |
| HTTP API | [docs/api/openapi.json](../api/openapi.json) |

## Validation

- Results: identifiers pattern-checked, LOINC code format, finite values,
  timezone-aware timestamps, `resulted_at >= collected_at`, collection after
  birth, no unknown fields.
- Units must equal the catalogue unit, otherwise `unit_mismatch` holds the
  result; unknown analytes hold with `not_in_catalogue`.
- Catalogue: citations must resolve, intervals and limits must be ordered,
  PBRTQC targets inside truncation limits, combination rules reference known
  analytes, approval needs approver and date.
- Batches: at most `LRI_MAX_BATCH` results; duplicate IDs rejected;
  resubmission with different content returns 409.

## Provenance

Each evidence packet contains an agentic-core `ProvenanceRecord` for the
result (`source_uri lis://results/<id>`, SHA-256 of the canonical result,
trust `VERIFIED`, classification `RESTRICTED`, retrieved by the evidence
agent), the catalogue ID, version, status and digest, and the full text of
every source cited by a fired rule. The packet digest is stored on the case
and recorded in the `case.opened` and `case.decided` audit events.

## Audit metadata

Every authorization decision (`authorize:<action>`: matched rules, policy
version and digest, decision source), every state change (`results.submitted`,
`result.screened`, `case.opened`, `case.claimed`, `case.proposal`,
`case.decided`, `case.escalated`, `pbrtqc.alarm`, `pbrtqc.recovered`) and
every MCP tool call and A2A skill invocation is appended to an Ed25519-signed,
hash-chained log with actor, resource, outcome and correlation ID.
`POST /v1/audit/verify` re-verifies the replica's chain.
