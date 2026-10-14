# Runbook: patient-based QC alarm

Alert: `LriPatientBasedQcAlarm`. Audit event: `pbrtqc.alarm` with the
statistic, target, control limit and `affected_result_ids`.

1. `GET /v1/qc/pbrtqc` (technologist): find the instrument and analyte with
   `in_control: false`.
2. Every new result for that instrument and analyte is held with a
   `pbrtqc_alarm` finding until the statistic recovers. Tell the bench.
3. Run conventional QC on the instrument for that analyte. Check recent
   reagent lot changes, calibration and maintenance.
4. If conventional QC fails: take the analyte offline on that instrument,
   correct, recalibrate, rerun QC, and re-test the `affected_result_ids`
   (the look-back window) per the laboratory's corrective-action procedure.
5. If conventional QC passes: consider a population shift (e.g. a dialysis
   or oncology run) and review the held results individually; recurring false
   alarms are a reason to revisit `block_size`, truncation limits or
   `control_limit` through a catalogue change.
6. Recovery is logged as `pbrtqc.recovered` once the statistic returns inside
   the limits.
