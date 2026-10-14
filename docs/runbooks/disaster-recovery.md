# Runbook: disaster recovery

| Component | Protection | RPO | RTO target |
|---|---|---|---|
| PostgreSQL (results, cases, PBRTQC) | Cloud SQL regional HA, PITR, 14 daily backups | minutes | 1 h |
| Audit chains | Per-replica PVC, shipped to retention-locked GCS | shipping interval | n/a (archive) |
| Catalogue, policy | Git and Helm values | 0 | redeploy |
| Audit signing key | Secret Manager (created outside Terraform) | 0 | minutes |

## Regional loss

1. Restore Cloud SQL to the recovery region from backup or PITR.
2. Update the `lab-result-integrity-database-url` secret; External Secrets
   syncs it.
3. Deploy the chart to the recovery cluster with the same values.
4. Middleware resubmits results from its queue; resubmission is idempotent,
   and results whose screening was interrupted resume.
5. PBRTQC state restored from the database continues; if restored to an
   older point, expect the moving statistic to differ briefly from the
   pre-incident value.

## Audit volume loss

New pods start a new chain (sequence 0) on a fresh PVC. The archived chain in
GCS remains verifiable with the same public key. Record the gap in the
quality log.
