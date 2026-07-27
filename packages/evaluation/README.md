# Evaluation

Runtime control evaluation, trace scoring, milestone checks, and scenario harness logic live here.

Current M2 scope:

- `evaluate_task_trace` scores trace completeness from task checkpoint JSON and task events.
- `/verify` and `/eval` call this evaluator through the API command surface and store compact summaries in `TaskState.metadata.evaluations`.
- `run_m2_scenarios` executes software engineering, browser research, and dynamic control scenarios, then writes JSON and Markdown reports.
- The evaluator is intentionally lightweight: it reports metrics, checks, score, and recommendations; it does not own routing or runtime control.

M3 freeze scope:

- `regression_hardening` registers dependency-aware generated-input cases,
  assigns deterministic shards, enforces isolation/timeouts, writes
  integrity-bound redacted artifacts, and triages failures.
- Its campaigns validate default CLI/API/Web/worker reachability and owner
  disable/mutation behavior; bidirectional causality; clean state; deterministic
  LLM control boundaries; Patch/Git safety; secret/injection protection;
  code-index effects; and approval races/crash restore.
- `zyra-regression-hardening verify-receipt` admits only an exact-revision,
  digest-valid, all-passing suite receipt with the protected M3-01 prerequisite.
- The package observes active owners through explicit subject ports. It never
  becomes a session, permission, scheduler, recovery, workspace, index or
  approval fallback owner.
