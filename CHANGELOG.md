# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-14

### Added

- Screening pipeline: trend analysis (delta checks, least-squares trend), interference
  detection (HIL indices, implausible combinations, plausibility limits), patient-based
  real-time QC (SMA and EWMA with truncation, alarm look-back and recovery) and evidence
  collection.
- Reference and critical ranges by age band and sex, with a conservative rule for
  unknown sex.
- Governed analyte catalogue: every threshold cites a source; production refuses a
  catalogue that is not laboratory-director approved.
- Pathologist review queue: priorities, SLA due times, claims, director override,
  decisions bound to the evidence digest, agent proposals pending human approval,
  SLA escalation sweeper.
- Never-release invariant: no channel can release or autoverify a result; the policy
  self-test refuses to start under a policy that would allow it.
- REST API, MCP server (FastMCP, streamable HTTP, typed tool schemas) and A2A Agent
  Card with JSON-RPC `message/send`.
- Authentication with enterprise OIDC and agent-identity-fabric credentials; local
  policy combined with fabric authorization (most restrictive wins).
- Optional de-identified evidence narrative through the agentic-core model router
  with ContentGuard on input and output.
- Signed per-replica audit chains with an audit verification endpoint.
- Helm chart, Kubernetes namespace and secret sync, Terraform for GCP (primary), AWS
  and Azure.
- CI: lint, types, SAST, contract drift, tests (unit, security, chaos, performance,
  e2e, PostgreSQL integration), Helm/kubeconform, Terraform validate, Trivy, Syft,
  Grype, Semgrep, container smoke test and production-refusal check.
