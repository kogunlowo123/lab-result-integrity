# Compliance control mapping

This maps implemented controls to framework requirements. It is an
engineering aid for the laboratory's quality and compliance teams, not a
certification or legal opinion. The laboratory remains responsible for
validating the service in its own environment.

| Framework | Requirement area | Implementation | Evidence |
|---|---|---|---|
| CLIA 42 CFR 493.1253 | Verify performance specifications, including reference intervals for the patient population | Catalogue is laboratory configuration; production requires `governance.status: approved` with approver and date | `configs/analytes.yaml`, ADR 0003, CI production-refusal check |
| CLIA 42 CFR 493.1291(g) | Immediate notification of panic/alert values | Critical limits open `stat` cases with a catalogue-defined SLA; overdue cases escalate and page | `review_policy`, `LriReviewCasesOverdue` |
| CLIA 42 CFR 493.1281 / 493.1282 | Comparison of results; corrective action | Delta checks and PBRTQC alarms with look-back lists of affected results; decisions recorded | evidence packet, `pbrtqc.alarm` audit events |
| CAP Laboratory General checklist (autoverification) | Autoverification validated and controlled by the laboratory director | The service never autoverifies; it only screens and holds (ADR 0001) | policy self-test, `released_by_agent: false` |
| CLSI EP33, C56, EP28, GP47, Badrick et al. 2019 | Delta checks, HIL indices, reference intervals, critical results, PBRTQC | Each rule cites its source; packets carry source text | catalogue `sources` |
| HIPAA Security Rule 164.312(a) access control | Unique user identification, emergency access, automatic logoff | OIDC per user and per agent; short-lived tokens; role- and scope-based policy | `policies/lab-result-integrity.yaml` |
| HIPAA 164.312(b) audit controls | Record and examine activity | Signed hash-chained audit of every authorization and state change | `/v1/audit/verify` |
| HIPAA 164.312(c) integrity | Protect ePHI from improper alteration | Content digests, idempotent intake, audit chain, CMEK storage | tests/security |
| HIPAA 164.312(e) transmission security | Encryption in transit | TLS at ingress, `ssl=require` to Cloud SQL, HTTPS to fabric | Terraform, Helm |
| HIPAA 164.502(b) minimum necessary | Limit PHI disclosure | De-identified narrative prompt; patient-scoped history queries | ADR 0005 |
| HITRUST CSF 01 (access control), 09 (operations), 10 (development) | Access, logging, secure SDLC | Policy, audit, CI gates (SAST, SCA, SBOM, IaC scan) | `.github/workflows/ci.yml` |
| SOC 2 CC6.1 / CC6.3 | Logical access, least privilege | RBAC + ABAC; agents limited to scopes; Workload Identity | agent specification |
| SOC 2 CC7.2 / CC7.3 | Monitoring and incident response | Prometheus alerts, runbooks | `configs/prometheus/alerts.yaml`, `docs/runbooks` |
| SOC 2 CC8.1 | Change management | Code owners, CI gates, ADRs, catalogue change runbook | CODEOWNERS |
| ISO/IEC 27001:2022 A.5.15, A.8.2 | Access control, privileged access | Role separation (pathologist vs. director override) | policy |
| ISO/IEC 27001:2022 A.8.15, A.8.16 | Logging, monitoring | Audit chain, metrics, traces | overview |
| ISO/IEC 27001:2022 A.8.28 | Secure coding | mypy strict, bandit, Semgrep, property-based API tests | CI |
| NIST AI RMF (Govern, Map, Measure, Manage) | AI risk management | Model use limited to advisory narrative; human decision; monitoring of narrative outcomes; documented risks | ADR 0005, threat model |
| NIST SP 800-53 AC-3, AC-6 | Access enforcement, least privilege | Policy per action; agent denial of review functions | tests |
| NIST SP 800-53 AU-2, AU-9, AU-10 | Event logging, protection of audit information, non-repudiation | Signed chain, WORM archive | ADR 0004 |
| NIST SP 800-53 SI-7 | Software and information integrity | Digests, SBOM, provenance attestation on release | release workflow |
| NIST SP 800-53 CM-3, CM-5 | Configuration change control | Governed catalogue, policy self-test | ADR 0003 |
| 21 CFR Part 11 (where results support regulated studies) | Audit trails, signature manifestation | Audit trail with actor, time, action; decisions bound to reviewer identity. Electronic signature workflows are out of scope and remain in the LIS | audit chain |
