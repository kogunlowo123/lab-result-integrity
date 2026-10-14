# Runbook: monitoring

Dashboard: `configs/grafana/lab-result-integrity.json`. Alerts:
`configs/prometheus/alerts.yaml`.

## What healthy looks like

- Request success at or above 99.9%; p95 `/v1/screenings` under 1 s.
- `lri_review_cases_overdue` at 0 most of the time.
- Hold rate stable week to week for the same catalogue version.
- No PBRTQC alarms outside instrument maintenance windows.

## Latency

`LriScreeningLatencyHigh`: check Cloud SQL CPU and connections, batch sizes
(`lri_results_screened_total` rate vs request rate), and PBRTQC conflicts in
logs (`modified concurrently`). Scale replicas if CPU bound; if a single
instrument dominates, contention on its PBRTQC row is expected; middleware
should send that instrument's results in fewer, larger batches.

## Hold rate

`LriHoldRateAnomalous`: break down `lri_findings_total` by kind. A jump in one
kind after a catalogue deployment points at the catalogue change (compare
`catalogue_digest` in recent outcomes); a jump in `hil_interference` or
`pbrtqc_alarm` for one instrument points at the instrument or pre-analytics.

## Narratives

`LriNarrativeUnavailable`: screening is unaffected. Check the model gateway
and `lri_evidence_narratives_total{status}`; `blocked` means ContentGuard
refused output and is worth sampling from case evidence.

## Authorization denials

A sustained rise in `lri_authorization_decisions_total{decision="deny"}`
usually means an agent was deployed without its scopes; the audit chain
shows `authorize:<action>` events with the actor.
