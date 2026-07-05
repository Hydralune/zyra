# Zyra

Dynamic heterogeneous multi-agent system for long-horizon complex tasks.

Zyra is the project workspace for the competition system "智衍群策：动态异构多智能体协同推理系统". The first engineering phase aims to build a complete agent architecture for long-horizon complex tasks, then optimize it against the competition requirements.

## Current Phase

M0 established the engineering baseline:

- shared core schema for runs, tasks, plan nodes, messages, commands, artifacts, budgets, workers, and events
- JSONL event log with stable `run_id`, `task_id`, `node_id`, and `event_id`
- zero-dependency development API that can create an empty task and persist the creation event
- static Web console shell for local inspection and task creation
- local verification script and stdlib unit tests

M1 established the control plane and task graph:

- staged graph shape: `plan -> route -> execute -> verify -> finalize`
- SQLite event log and checkpoint store at `tmp/zyra.sqlite3`
- task creation, listing, query, run, cancellation, and event query endpoints
- Web console task list and event stream shell

M2 established the worker runtime and tool governance layer:

- slash command, skill, tool, and worker runtime registries
- `/skills` command and `POST /tasks/{task_id}/skills` skill invocation endpoint, with independent `skill_invoked` events and checkpoint metadata
- vendored `claude-code-best` and `browser-use` snapshots inside `vendor/`
- CodeWorker sidecar boundary for the vendored TypeScript runtime
- permissioned `ToolExecutor` for file read/write/edit, shell, and artifact output
- controlled `web_search` execution over workspace research files, local file URLs, and explicitly allowed network URLs, with search trace artifacts
- `browser` tool execution for single-call inline HTML or allowed URL state snapshots; multi-step browser plans remain under `BrowserWorkerRuntime`
- `trace` tool execution through the API, backed by SQLite task events and optional trace artifacts
- `checkpoint` tool execution through the API, backed by SQLite task checkpoints and optional checkpoint artifacts
- JSON-backed permission rules and pending approval requests for allow/deny/ask shell governance
- API tool execution endpoint that records tool results in the task event stream
- `CodeWorkerRuntime` QueryEngine contract-backed loop exposed through the API, with vendored Claude Code sidecar contract, `stream_request_start`, session/turn lifecycle events, read-only concurrent/write-serial tool batching, tool use summaries, turn limits, tool result budget, query context compaction artifacts, and error stop/continue behavior
- CodeWorker source inventory for `claude-code-best` tools, commands, permission, compact, MCP, skills, and subagent boundaries
- `BrowserWorkerRuntime` browser-use adapter boundary for URL/HTML state capture, extracted text, structured page state, click/input/search-page actions, live-only browser-use tool actions, artifacts, and event trace
- optional `browser-use-live` backend for `BrowserWorkerRuntime`, backed by vendored `browser-use` `BrowserSession` in headless Chrome/Edge, including real input/click/search, wait, scroll, scroll-to-text, keyboard, screenshot/PDF artifacts, JavaScript evaluation, back-navigation, workspace file upload, and downloaded-file artifact collection; runtime config/cache/temp/profile/download writes are isolated under `tmp/browser-use-runtime`
- optional `browser-use-agent` backend for `BrowserWorkerRuntime`, backed by vendored `browser-use` `Agent`, explicit LLM provider/key configuration, `BrowserSession`, Agent history artifacts, Agent trace events, available-file constraints, and project-local runtime directories
- BrowserWorker action metadata loaded from vendored `browser-use` action models and registry source files
- BrowserWorker runtime health API for vendored `browser-use` Python imports, Agent/AgentHistoryList, action models, LLM factory, CDP dependency, and project-local browser-use environment
- artifact catalog endpoints and Web console panel for task-scoped runtime evidence
- expanded slash command control plane with command result views, context compact artifacts, and run export artifacts
- lightweight trace evaluator behind `/verify` and `/eval`, recording score summaries in task metadata
- stateful context session runtime for `/clear`, `/rewind`, `/resume`, `/context`, and `/memory`, backed by visible context windows and resumable snapshots in task checkpoint metadata
- task graph `execute` stage can now call worker runtimes when the API provides `GraphExecutionContext`
- Web console task graph visualization, runtime inventory, artifact, and permission panels

M3 established the structured collaboration and neuro-symbolic control layer:

- core schema now carries M3 collaboration fields on `AgentMessage`, `PlanNode`, `ConstraintSet`, and `DecisionRecord`
- task graph version upgraded to `m3-symbolic-v1`, with low-entropy structured `agent_message`, `constraint_check`, and `topology_route` events
- `ConstraintKeeper` checks node schema, dependencies, worker allow-lists, budget, message-size pressure, forbidden terms, state transitions, and terminal criteria
- `TopologyRouter` selects top-k heterogeneous worker routes from task text, runtime hints, worker capabilities, resource constraints, and failure history, then appends replayable `DecisionRecord` entries
- route nodes now assign the M2 `CodeWorkerRuntime` or `BrowserWorker` execution node instead of recording a static supervisor route
- `/change` now associates the requirement change with affected `PlanNode` ids, supersedes stale nodes, creates a local replan node, and emits route/check events
- `/inject` now creates a structured failure recovery node and route decision after preserving a `node_failed` event
- `/verify` and `/eval` metrics now include symbolic control evidence: decision records, topology routes, constraint checks, structured messages, and replanned/superseded nodes

## Direction For M4-M6

M0-M3 established contracts and runtime control paths. The next milestones must use those boundaries to internalize substantial mature capabilities from the reference repositories, not merely add thin wrappers.

- M4 should turn memory, context compaction, trajectory replay, checkpoint/retrieval, and skill memory into callable Zyra modules.
- M5 should make resource scheduling, worker manifests, sandbox/gateway boundaries, fault injection, and recovery policies affect real task execution.
- M6 should replace the current console shell with a connected control console for task graph, event timeline, worker state, artifact/diff/browser/terminal views, slash commands, and live requirement changes.

Vendor snapshots are a migration pool or explicit runtime boundary. Long-term capabilities should be collected into `apps/`, `packages/`, or clearly named runtime adapters before the first-stage freeze.

## Layout

```text
apps/
  api/      Development API shell.
  web/      Static console shell.
packages/
  core/     Shared schemas, identifiers, serialization, and event log.
  commands/ Slash command runtime boundary.
  skills/   Built-in skill runtime boundary.
  symbolic/ ConstraintKeeper, TopologyRouter, and control-event state transitions.
  workers/  Heterogeneous worker runtime boundary.
  ...
docs/       Project-local architecture notes and phase records.
scripts/    Local development and verification entrypoints.
tests/      Unit, integration, and scenario tests.
```

The higher-level planning documents live one level up:

- `../docs/比赛项目开源Agent架构借鉴分析.md`
- `../docs/第一阶段工程计划.md`

## Local Commands

Run the M0 verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m0.py
```

Run the M1 verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m1.py
```

Run the current M2 protocol/vendor verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m2.py
```

Run the current M3 symbolic collaboration verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m3.py
```

Run the M2 cross-module acceptance scenario:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.scenarios.test_m2_runtime_acceptance
```

Run the M2 demo scenario report generator:

```powershell
.\.venv\Scripts\python.exe scripts\run_m2_scenarios.py
```

Run the M3 structured collaboration scenario:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.scenarios.test_m3_symbolic_collaboration
```

Audit M0-M3 against the heavyweight internalization goal:

```powershell
.\.venv\Scripts\python.exe scripts\audit_m0_m3_internalization.py
```

Check vendored browser-use Python runtime health:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_browser_use_runtime.py
```

Run a real browser-use BrowserSession smoke test:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_browser_use_runtime.py --live --timeout 45
```

Run a real BrowserWorker live-backend smoke test:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_browser_worker_live.py
```

Run a real BrowserWorker live file-transfer smoke test:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_browser_worker_file_transfer.py
```

Verify the CodeWorker sidecar boundary:

```powershell
.\.venv\Scripts\python.exe scripts\verify_code_worker_sidecar.py
```

Verify that runtime code does not depend on source repositories outside `zyra`:

```powershell
.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
```

Run stdlib unit tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Start the development API:

```powershell
.\.venv\Scripts\python.exe scripts\dev_api.py
```

Start the static Web console:

```powershell
.\.venv\Scripts\python.exe scripts\dev_web.py
```

The API listens on `http://127.0.0.1:8000` by default. The Web console listens on `http://127.0.0.1:5173` by default.
When running an alternate API port, open the Web console with `?api=...`, for example `http://127.0.0.1:5174/?api=http://127.0.0.1:8010`.

Useful development API endpoints:

- `GET /health`
- `POST /tasks`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `POST /tasks/{task_id}/run`
- `POST /tasks/{task_id}/cancel`
- `POST /tasks/{task_id}/commands`
- `POST /tasks/{task_id}/skills`
- `POST /tasks/{task_id}/tools`
- `POST /tasks/{task_id}/workers/code`
- `POST /tasks/{task_id}/workers/browser`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/artifacts`
- `GET /events?limit=100`
- `GET /artifacts`
- `GET /artifacts/{artifact_id}`
- `GET /commands`
- `GET /skills`
- `GET /tools`
- `GET /workers`
- `GET /workers/code/inventory`
- `GET /workers/browser/actions`
- `GET /workers/browser/health`
- `GET /permissions`
- `POST /permissions/rules`
- `POST /permissions/requests/{request_id}/resolve`
