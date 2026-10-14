# Runbook: review SLA breach

Alert: `LriReviewCasesOverdue`. The SLA sweeper marks overdue cases
`escalated` and logs `case.escalated`.

1. `GET /v1/review-queue` (pathologist): overdue cases show `overdue: true`;
   `stat` cases first.
2. Page the pathologist on call. For `stat` cases with critical values,
   follow the critical-result notification procedure in parallel; the
   integrity hold does not replace notification.
3. If the assigned reviewer is unavailable, the laboratory director takes
   the case over (`POST /v1/cases/{id}/claim` with the `lab_director` role).
4. If the queue is overloaded, check the hold rate (monitoring runbook): an
   instrument problem or a catalogue change can flood the queue.
