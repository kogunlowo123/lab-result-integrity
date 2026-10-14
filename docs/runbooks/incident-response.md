# Runbook: incident response

## Elevated 5xx

1. `kubectl -n lab-integrity logs statefulset/lri-lab-result-integrity --since=15m`
   and filter by `correlation_id` from a failing request.
2. `/readyz` 503 means the database is unreachable: check Cloud SQL status
   and the private IP route. Pods stay live; middleware should retry
   (submissions are idempotent).
3. 503 `upstream_unavailable` on agent calls: the identity fabric is down.
   Fabric-authenticated agents fail closed by design; OIDC users continue.
4. While screening is unavailable the LIS must not fall back to releasing
   results it would have sent for screening. Laboratory procedure: hold
   middleware queues and notify the pathologist on call.

## Suspected bypass or tampering (security incident)

1. Run `POST /v1/audit/verify` (auditor) on every replica. A `verified:false`
   names the first broken sequence; preserve the PVC and the archived chain.
2. Compare the catalogue digest in recent outcomes with the approved digest.
3. Search the audit chain for `authorize:review:decide` with `outcome:allow`
   where `principal_kind` is `agent`; there must be none.
4. Revoke the involved agent at the identity fabric or disable the OIDC
   client; rotate the audit key (new kid, keep the old public key for
   verification).

## Authorization denials

Identify the actor from `authorize:*` deny events. If it is a legitimate
agent missing a scope, fix its registration; if not, treat as probing and
block the client at the ingress.

## Patient-impacting error

If a held result was wrongly confirmed or a released result was wrong, follow
the laboratory's corrected-report procedure in the LIS. Export the case
(`GET /v1/cases/{id}`) with its evidence digest for the quality record.
