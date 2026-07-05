# Evaluation

Runtime control evaluation, trace scoring, milestone checks, and scenario harness logic live here.

Current M2 scope:

- `evaluate_task_trace` scores trace completeness from task checkpoint JSON and task events.
- `/verify` and `/eval` call this evaluator through the API command surface and store compact summaries in `TaskState.metadata.evaluations`.
- `run_m2_scenarios` executes software engineering, browser research, and dynamic control scenarios, then writes JSON and Markdown reports.
- The evaluator is intentionally lightweight: it reports metrics, checks, score, and recommendations; it does not own routing or runtime control.
