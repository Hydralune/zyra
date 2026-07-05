# Orchestration

Supervisor, planner, router, task graph, replanning, and process control modules will live here.

Current M2 orchestration boundary:

- `run_task_graph(state)` preserves the M1 deterministic graph behavior when no runtime context is provided.
- `run_task_graph(state, execution_context=GraphExecutionContext(...))` routes the `execute` stage through real worker runtimes.
- The default execution route uses `CodeWorkerRuntime` and writes an execution summary through `ToolExecutor`.
- Browser execution can be selected with `state.metadata["runtime_hints"]`, for example `preferred_worker="BrowserWorker"` plus a `browser_url` or explicit `browser_plan`.
- Worker events, worker artifacts, assigned worker IDs, and tool/browser action counts are written back into `TaskState`.

This keeps M1 tests stable while allowing the API run path to use M2 worker runtime execution.
