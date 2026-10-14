# ADR 0005: Model-drafted narrative is optional, de-identified and advisory

Status: accepted (2026-10-14)

## Context

Pathologists read many held results a day. A short narrative restating the
evidence can speed review, but model output must never be the basis of a
release decision, and sending patient identifiers to a model widens the PHI
footprint for little benefit.

## Decision

- Narratives are disabled unless `LRI_PROVIDERS_FILE` is set. Providers are
  built with the agentic-core `ModelRouter` from `configs/providers.yaml`;
  values are `${ENV}` references that fail closed when unset. The primary
  deployment points at the in-cluster model gateway, so this service holds no
  model credential.
- The prompt is built from de-identified facts only (analyte, value, unit,
  flag, finding messages, trend, sex, age in years). Patient, specimen and
  accession identifiers and birth dates are excluded. Requests are classified
  `CONFIDENTIAL`, so only providers approved for that class are eligible.
- ContentGuard inspects input and output. A block, provider failure or
  timeout leaves the narrative empty with a status; screening continues.
- The narrative is labelled `advisory: true`, is generated only for held
  results, and the system prompt forbids release language.

## Consequences

- Screening has no dependency on any model provider.
- Narrative quality is monitored through `lri_evidence_narratives_total`; it
  is not part of the evidence digest's decision basis beyond being recorded.
