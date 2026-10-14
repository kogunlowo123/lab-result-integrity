# Runbook: analyte catalogue change

The catalogue holds every threshold the agents apply. Changes are clinical
laboratory decisions and follow the laboratory's change-control procedure.

1. Draft the change in a branch. Every new or changed numeric value carries
   `cite:` ids; add any new source text to `sources`.
2. Validate locally:
   ```bash
   uv run python -c "from pathlib import Path; from lab_result_integrity.domain.catalogue import AnalyteCatalogue as C; print(C.load(Path('new.yaml'), allow_starter=False).digest)"
   ```
3. Verification evidence: replay a representative set of recent synthetic or
   de-identified results against the new catalogue in a non-production
   environment and compare hold rates and finding kinds with the current
   version.
4. Laboratory director approval: set `governance.version`, `status:
   approved`, `approved_by`, `approved_on`. Record the digest in the change
   ticket.
5. Deploy by updating `lri.catalogue` in the Helm values. The checksum
   annotation rolls the pods.
6. After deployment, `GET /v1/catalogue` must show the recorded digest.
   Changing PBRTQC parameters for an analyte resets its moving statistic.
