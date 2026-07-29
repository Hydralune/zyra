# First-stage freeze and submission runbook

## Stable entry points

Run from the `zyra` repository root at implementation commit
`faf78d1ec6aa1bfe197dbb6120baedabbb723eb7`.

Verify the complete retrospective final-freeze output:

```powershell
.\.venv\Scripts\python.exe scripts\verify_first_stage.py `
  --output docs/reviews/evidence/M3-S03-02/current-faf78d1/final-freeze
```

Verify the submission candidate from archive bytes:

```powershell
.\.venv\Scripts\python.exe scripts\verify_first_stage.py `
  --submission `
  --output docs/reviews/evidence/M3-S03-02/current-faf78d1/submission-candidate
```

Verify the target-bound report and evidence archive:

```powershell
.\.venv\Scripts\python.exe scripts\generate_first_stage_report.py verify `
  --output docs/reviews/evidence/M3-S03-01/generated-88b88e05 `
  --expected-commit 88b88e05aad14e1091f4536bcead02037622408f
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
3. Use `final-freeze/evidence-navigation.json` to navigate semantic health,
   current provider evidence, two live domains, topology ablation,
   fault/recovery, causal replay, the 100-point index, and the final
   submission verifier.
4. Use
   `../M3-S03-01/generated-88b88e05/generated/case-studies.json` for the two
   cross-domain live cases and their independent repetitions.
5. Use
   `../M3-S03-01/generated-88b88e05/generated/compatibility-material.json`
   for the current DeepSeek, Kimi, and GLM provider/model receipts.
6. Use
   `../M3-S03-01/generated-88b88e05/generated/100-point-evidence-index.json`
   for all 19 scored requirements.

## Rehearsal and release archive

- The current release archive is built twice from commit `faf78d1` and the
  resulting bytes are identical. The current archive is included in the
  submission candidate.
- The inherited M3-S02B-02 13-gate release receipt remains authoritative for
  the hour-long clean-install and full-suite gate. The retrospective review
  re-runs focused and adjacent tests instead of repeating that hour-long job.
- Offline dependency absence is fail-closed. The current evidence proves the
  offline policy and missing/hash-mismatch behavior, but does not claim that a
  complete offline wheelhouse has been assembled.
- Restart recovery is verified by a real three-profile integration test.
- Provider backoff, circuit behavior, and terminal classification are verified
  by the provider-failure drill.
- The semantic-health drill binds the inherited release health to the current
  12-call, three-provider formal campaign without persisting credentials.
- If any required drill fails, the final-freeze orchestrator returns
  `BLOCKED`.

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

The current records establish first-stage implementation freeze and submission
dry-run readiness. They intentionally do not claim a future human sign-off or
send confirmation.
