# First-stage freeze and submission runbook

## Stable entry points

Run from the `zyra` repository root at implementation commit
`8825722359e2ca30998d42e9fccf4a35ee307f31`.

Verify the complete final-freeze output:

```powershell
.\.venv\Scripts\python.exe scripts\verify_first_stage.py `
  --output docs/reviews/evidence/M3-S03-02/final-freeze
```

Verify the submission candidate from archive bytes:

```powershell
.\.venv\Scripts\python.exe scripts\verify_first_stage.py `
  --submission `
  --output docs/reviews/evidence/M3-S03-02/submission-candidate
```

Verify the target-bound report and evidence archive:

```powershell
.\.venv\Scripts\python.exe scripts\generate_first_stage_report.py verify `
  --output docs/reviews/evidence/M3-S03-02/first-stage-report-8825722 `
  --expected-commit 8825722359e2ca30998d42e9fccf4a35ee307f31
```

Verify that deliverable code has no root source-repository dependency:

```powershell
.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
```

## Product and evidence navigation

1. Start the API with
   `.\.venv\Scripts\python.exe scripts\dev_api.py`.
2. Start the Web console with
   `.\.venv\Scripts\python.exe scripts\dev_web.py`.
3. Use `evidence-navigation.json` to move from the default entry to semantic
   health, two live domains, topology ablation, fault/recovery, causal replay,
   the 100-point index, and the final submission verifier.
4. Use `first-stage-report-8825722/generated/case-studies.json` for the two
   cross-domain live cases and their independent repetitions.
5. Use
   `first-stage-report-8825722/generated/100-point-evidence-index.json` for the
   complete 19-row, 100-point requirement navigation.

## Offline and restart plan

- Offline dependency absence is fail-closed. Do not synthesize a dependency,
  provider credential, or successful provider result.
- The offline policy and missing/hash-mismatched artifact cases are verified
  by the `offline-startup` rehearsal drill.
- Restart recovery is verified by a real three-profile dispatch/recover/restart
  integration test in the `process-restart` drill.
- Provider backoff, circuit behavior, and terminal classification are verified
  by the `provider-failure` drill.
- If any required drill fails, the final-freeze orchestrator must return
  `BLOCKED`; do not bypass or hand the failure to the second stage.

## Submission checklist

- RC1: 2026-09-01.
- Rehearsal and feature freeze: 2026-09-08.
- Final bundle, checksums, and human two-person check: 2026-09-12.
- Official submission deadline: 2026-09-15.
- Use only the archive name and digest in
  `submission-candidate/submission-email-checklist.json`.
- Complete the registration fields listed in `submission-checklist.json`.
- Record the human technical reviewer and submission reviewer against the same
  manifest digest.
- Retain the platform or email acceptance receipt and compare the received
  artifact digest with the submission-lock manifest.

The current records establish implementation freeze and submission dry-run
readiness. They intentionally do not claim a future send confirmation.
