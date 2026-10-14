# ADR 0003: Thresholds live in a governed, cited analyte catalogue

Status: accepted (2026-10-14)

## Context

Reference intervals, critical limits, delta limits, HIL thresholds and
moving-average parameters are laboratory decisions. They depend on method,
analyzer, reagent lot and population, and CLIA requires the laboratory to
verify them (42 CFR 493.1253) and define critical-value notification
(42 CFR 493.1291). Hard-coding them, or shipping them as if they were
authoritative, would be wrong and unsafe.

## Decision

- All thresholds are configuration in `configs/analytes.yaml`, validated by a
  Pydantic model. Every numeric rule carries `cite:` ids that must resolve to
  entries in `sources`; dangling citations are rejected.
- The catalogue has a `governance` block. Status `approved` requires
  `approved_by` and `approved_on`. The shipped file is `starter`: typical
  values to exercise the rules with synthetic data, stated as not clinical
  guidance.
- The service refuses a non-approved catalogue unless
  `LRI_ALLOW_STARTER_CATALOGUE=true`, which itself is refused when
  `LRI_ENVIRONMENT=production`. Helm requires the approved catalogue as a
  value and mounts it from a ConfigMap.
- Each screening outcome and evidence packet records the catalogue version
  and digest; packets include the full text of each cited source.

## Consequences

- A laboratory must author its catalogue before go-live; this is intended.
- Catalogue changes are reviewed changes with an audit trail
  (runbook: catalogue-change). Changing PBRTQC parameters resets that
  analyte's moving statistic (state is keyed by configuration digest).
