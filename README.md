# Zyra

Dynamic heterogeneous multi-agent system for long-horizon complex tasks.

Zyra is the project workspace for the competition system "智衍群策：动态异构多智能体协同推理系统". The first engineering phase aims to build a complete agent architecture for long-horizon complex tasks, then optimize it against the competition requirements.

## Current Phase

Canonical status: the previous M0-M5 work is now consolidated into a single completed `M0: foundation and main-path bootstrap`. It is not treated as six completed heavyweight milestones.

Consolidated M0 delivered:

- shared schema, event log, checkpoint store, task graph, control commands, slash-command surface, skills/tools/worker registries, artifact catalog, and a static inspection console
- vendored `claude-code-best` and `browser-use` snapshots inside `vendor/`, plus first CodeWorker and BrowserWorker adapter boundaries
- permissioned tool execution, API-backed tool events, context/session commands, runtime inventory, and task graph execution through worker runtimes
- first structured collaboration path with `ConstraintKeeper`, `TopologyRouter`, structured messages, route decisions, `/change`, `/inject`, `/verify`, and `/eval`
- first MemoryFabric path for memory records, compact records, trajectory replay, memory/compact/context commands, and API/console panels
- first scheduler/fault path with worker manifests, resource decisions, backend dispatch envelopes, watchdog classification, recovery planning, scheduler APIs, and a connected scheduler panel

What M0 does not prove:

- full Claude Code QueryEngine/ToolPermission/MCP/SkillTool/AgentTool/compact/session-command internalization
- full browser-use message manager, watchdog, browser session, trace, and agent-history internalization
- mature OpenHands/OpenClaw/AgentScope-style sandbox, workspace, gateway, event stream, or backend failover integration
- complete memory retrieval, skill memory, MemoryCurator, compact restore, or trajectory-driven recovery
- formal control console with event stream, topology replay, artifact/diff/browser/terminal viewers, permission/session/context panels, command palette, and live requirement-change flow

Next milestone: new `M1: heavyweight Runtime / Memory / Scheduler / Fault internalization backfill`. New M1 must use the consolidated M0 boundaries to migrate, encapsulate, or productize substantial mature modules from the reference repositories. It should not be completed by adding only thin adapters, small hand-written schedulers, inventory scans, or static UI panels.

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

Run the consolidated M0 foundation verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m0.py
```

Run the historical M0.1 task-graph verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m1.py
```

Run the historical M0.2 protocol/vendor verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m2.py
```

Run the historical M0.3 symbolic collaboration verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m3.py
```

Run the historical M0.2 cross-module acceptance scenario:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.scenarios.test_m2_runtime_acceptance
```

Run the historical M0.2 demo scenario report generator:

```powershell
.\.venv\Scripts\python.exe scripts\run_m2_scenarios.py
```

Run the historical M0.3 structured collaboration scenario:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.scenarios.test_m3_symbolic_collaboration
```

Run the historical M0.4 memory, compact, and trajectory verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m4.py
```

Run the historical M0.5 scheduler, fault injection, and recovery verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m5.py
```

Audit the historical M0.0-M0.3 work against the heavyweight internalization goal:

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
- `GET /tasks/{task_id}/memory`
- `POST /tasks/{task_id}/memory/ingest`
- `POST /tasks/{task_id}/memory/compact`
- `GET /tasks/{task_id}/trajectory`
- `GET /tasks/{task_id}/compactions`
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
