# Threat model

Scope: the API process (REST, MCP, A2A), its PostgreSQL database, the
per-replica audit chains, the analyte catalogue and policy, and the optional
model gateway call. Assets: patient results (PHI, RESTRICTED), review
decisions, evidence packets, the catalogue and policy, the audit chain.

The worst outcome is a wrong result reaching a clinician because the
integrity layer was bypassed, misconfigured or manipulated. The second worst
is a correct result delayed past clinical need.

## STRIDE

| ID | Threat | Control | Test |
|---|---|---|---|
| S1 | Forged or unsigned bearer token (alg `none`, attacker key) | JWKS signature verification, algorithm allow list, required claims | `tests/security/test_attacks.py::test_forged_tokens_are_rejected` |
| S2 | Replay of expired token | `exp`/`iat` checked with 30 s leeway | same |
| S3 | Token minted for another service (confused deputy) | `aud` must equal `LRI_OIDC_AUDIENCE` | same |
| S4 | Suspended agent keeps acting with a fabric credential | Fabric introspection per request; inactive or non-active agents rejected | `tests/unit/test_auth.py` |
| T1 | Injection through identifiers | Pattern-validated path and body identifiers; parameterised SQL Core | `test_identifier_injection_is_rejected` |
| T2 | Resubmitting a result with an altered value | Content digest per result ID; mismatch returns 409 | `test_tampered_result_content_conflicts` |
| T3 | Catalogue edited to loosen thresholds | Code-owner review, governance approval, catalogue digest in every outcome | `tests/unit/test_catalogue.py` |
| T4 | Policy edited to let agents release | Start-up policy self-test refuses to run | `tests/unit/test_container.py` |
| R1 | Audit events altered or removed | Hash chain + Ed25519 signature; verification endpoint; WORM archive | `test_audit_tampering_is_detected` |
| R2 | Pathologist disputes a decision | Decision stores reviewer, time, policy digest and evidence digest | `tests/unit/test_service.py` |
| I1 | PHI sent to a model provider | Narrative built from de-identified facts; CONFIDENTIAL classification gate; ContentGuard PII redaction | `tests/unit/test_container.py::test_evidence_narrative` |
| I2 | PHI in logs | Structured logging with sensitive-key scrubbing; no request bodies logged | review |
| D1 | Oversized batches | Body validation (max 5000), `LRI_MAX_BATCH` (500), MCP max 500 | `test_batch_limit_is_enforced` |
| D2 | Hot PBRTQC row contention | Bounded retries then 409; idempotent resubmission | `tests/chaos` |
| E1 | Over-scoped agent decides or releases | Policy denies by kind, independent of scopes; no such tools | `test_agent_cannot_release_or_decide_on_any_channel` |
| E2 | Role injection via unexpected claims | Only the configured roles claim is read | `test_role_claim_from_wrong_claim_name_is_ignored` |
| E3 | Agent proposals used to force a decision | Proposals never change state; humans only decide | `test_proposals_cannot_change_state` |

## MITRE ATT&CK (enterprise)

| Technique | Relevance | Mitigation |
|---|---|---|
| T1078 Valid Accounts | Stolen staff or agent credentials | Short-lived OIDC tokens, fabric revocation, role separation, denial-spike alert |
| T1190 Exploit Public-Facing Application | API exposed via ingress | Schema validation, property-based contract test, Semgrep, Trivy, Grype |
| T1565.001 Stored Data Manipulation | Altering results or cases in the database | Private-IP Cloud SQL, least-privilege DB user, content digests, audit chain |
| T1552 Unsecured Credentials | Database URL, audit key | Secret Manager via External Secrets, read-only mounts, no keys in Terraform state |
| T1499 Endpoint Denial of Service | Batch flooding | Batch limits, HPA, rate limiting at the ingress |

## MITRE ATLAS (AI-specific, narrative path)

| Technique | Relevance | Mitigation |
|---|---|---|
| AML.T0051 LLM Prompt Injection | Text reaching the narrative prompt | Prompt contains only catalogue text and numbers; ContentGuard inspects input; output is advisory only |
| AML.T0057 LLM Data Leakage | Model echoing sensitive data | No identifiers in prompt; output secret scanning and PII redaction |
| AML.T0048 External Harms | Narrative misleading a reviewer | Labelled advisory; findings and evidence are authoritative; release never depends on it |

## OWASP Top 10 for LLM Applications (2025)

| Risk | Treatment |
|---|---|
| LLM01 Prompt Injection | Untrusted free text (proposal rationale) never enters prompts; ContentGuard on input |
| LLM02 Sensitive Information Disclosure | De-identified prompt, CONFIDENTIAL routing ceiling, output PII redaction |
| LLM03 Supply Chain | Pinned dependencies (uv.lock), pip-audit, SBOM, Grype |
| LLM04 Data and Model Poisoning | No training or fine-tuning; rules are deterministic configuration |
| LLM05 Improper Output Handling | Output stored as text, never executed or used for decisions |
| LLM06 Excessive Agency | Agents cannot release, claim or decide; proposals need human approval |
| LLM07 System Prompt Leakage | System prompt contains no secrets |
| LLM08 Vector and Embedding Weaknesses | Not applicable: no retrieval store |
| LLM09 Misinformation | Advisory label; evidence packet is the record |
| LLM10 Unbounded Consumption | Narrative only for held results, `max_tokens` 400, provider timeouts, circuit breaker in the router |

## OWASP API Security Top 10 (2023)

| Risk | Treatment |
|---|---|
| API1 BOLA | Every object read authorized per action and resource; pathologist roles required for decisions |
| API2 Broken Authentication | JWKS verification, fabric introspection, no API keys |
| API3 Broken Object Property Level Authorization | `extra="forbid"` on inputs; response models explicit |
| API4 Unrestricted Resource Consumption | Batch and pagination limits |
| API5 Broken Function Level Authorization | Policy per action; agent kind denied review functions |
| API6 Unrestricted Access to Sensitive Business Flows | Release flow does not exist here; proposals are rate-bounded by authorization |
| API7 SSRF | No user-controlled outbound URLs; outbound hosts are configuration |
| API8 Security Misconfiguration | Production guards (starter catalogue, ephemeral audit key refused), Helm required values, Trivy config scan |
| API9 Improper Inventory Management | OpenAPI and JSON Schemas generated and drift-checked in CI |
| API10 Unsafe Consumption of APIs | Fabric and model responses schema-validated; failures fail closed |

## Risk register

| ID | Risk | Likelihood | Impact | Treatment | Owner | Residual |
|---|---|---|---|---|---|---|
| R-01 | Starter thresholds used clinically | Low | High | Production refusal, Helm-required approved catalogue | Lab director | Low |
| R-02 | Overdue critical review delays care | Medium | High | Stat priority, 30 min SLA, escalation sweeper, paging alert | Lab operations | Medium |
| R-03 | PBRTQC tuned too tight (alarm fatigue) or too loose (missed drift) | Medium | Medium | Cited parameters, hold-rate alert, monthly KPI review | Lab director | Medium |
| R-04 | Identity fabric outage blocks agents | Low | Medium | Fail closed; OIDC path unaffected for staff | Platform | Low |
| R-05 | Audit chain loss on volume failure | Low | High | Retention-locked archive, StatefulSet PVCs, DR runbook | Platform | Low |
| R-06 | Model gateway leaks data | Low | Medium | De-identified prompt, classification ceiling, optional feature | Security | Low |
| R-07 | Policy mis-edit grants agents review rights | Low | High | Start-up self-test, code owners, tests | Security | Low |
