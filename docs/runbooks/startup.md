# Runbook: first deployment and start-up (GCP primary)

## Prerequisites

- GKE cluster with Workload Identity, External Secrets Operator and a
  `ClusterSecretStore` named `platform-secrets` bound to Secret Manager.
- VPC with private services access (Cloud SQL private IP).
- Enterprise OIDC application for this service (audience, roles claim with
  `technologist`, `pathologist`, `lab_director`, `auditor`).
- A laboratory-director approved analyte catalogue (see
  [catalogue-change.md](catalogue-change.md)).

## Steps

1. Provision cloud resources:
   ```bash
   cd deployments/terraform/gcp
   terraform init && terraform apply -var project_id=<project> -var region=<region> \
     -var network_id=<vpc self link>
   ```
   Note the outputs `gcp_service_account`, `database_url_secret`, `audit_bucket`.
2. Create the audit signing key outside Terraform so it never enters state:
   ```bash
   openssl genpkey -algorithm ed25519 -out audit-2026-10.pem
   gcloud secrets create lab-result-integrity-audit-key-2026-10 --data-file=audit-2026-10.pem
   shred -u audit-2026-10.pem
   ```
3. Apply namespace and secret sync: `kubectl apply -k deployments/kubernetes`.
4. Write environment values (`prod.yaml`): `lri.publicUrl`, `lri.oidc.*`,
   `lri.catalogue` (the approved YAML), `secrets.auditKeyFile:
   audit-2026-10.pem`, `serviceAccount.annotations:
   {iam.gke.io/gcp-service-account: <output>}`, optional `lri.fabricUrl`.
5. Install: `helm install lri oci://ghcr.io/kogunlowo123/charts/lab-result-integrity
   --version 0.1.0 -n lab-integrity -f prod.yaml`.

## Verify

- `kubectl -n lab-integrity get pods` all `Ready`; `/readyz` returns `ready`.
- Logs show `startup` and no `CatalogueError`. A starter catalogue or a policy
  that does not deny agent release stops start-up by design.
- `curl https://<host>/.well-known/agent-card.json` lists four skills.
- `GET /v1/catalogue` (technologist token) shows `status: approved` and the
  expected digest.
- Screen a synthetic result with a test client and confirm it appears in the
  dashboard counters.
