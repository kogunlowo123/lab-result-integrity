# ADR 0004: Shared state in PostgreSQL with optimistic concurrency; audit per replica

Status: accepted (2026-10-14)

## Context

Several replicas screen results and serve the review queue at once. Patient
history, review cases and PBRTQC moving statistics must be shared; two
pathologists must not both decide a case; a moving average must not count a
result twice. The agentic-core audit chain is a single ordered sequence and
needs one writer.

## Decision

- Results, cases and PBRTQC state are stored in PostgreSQL through SQLAlchemy
  Core. Cases and PBRTQC rows carry a `version`; updates are conditional on
  the version read. A lost race raises a conflict: the case API returns 409,
  PBRTQC updates are retried up to eight times.
- Screening is idempotent by result ID and content digest. An interrupted
  screening resumes: the existing case is reused and a result already in the
  PBRTQC window is not folded in again.
- Each replica writes its own signed audit chain to its own volume
  (StatefulSet `volumeClaimTemplates`); a log shipper copies chains to
  retention-locked object storage. Verification is per chain.

## Consequences

- StatefulSet rather than Deployment, solely for the audit volume.
- Under heavy contention on one instrument/analyte a batch may fail with 409
  and must be resubmitted; idempotency makes that safe.
- Cross-replica audit ordering is by timestamp and correlation ID, not by a
  single global sequence.
