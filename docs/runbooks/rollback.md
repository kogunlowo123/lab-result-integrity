# Runbook: rollback

- Application: `helm rollback lri <revision> -n lab-integrity`. The schema is
  additive (`schema_version` 1); rolling back the image does not require a
  database change.
- Catalogue: roll back by re-deploying the previous approved catalogue value.
  PBRTQC statistics for analytes whose parameters changed restart from empty
  (state is keyed by configuration digest); expect no alarms until the window
  refills.
- Policy: the previous image carries the previous policy. A policy that fails
  the start-up self-test never becomes ready, so the old pods keep serving.
- After any rollback confirm `/readyz`, the catalogue digest from
  `GET /v1/catalogue`, and screen a synthetic result.
