# Zyra Docs

This directory records project-local design decisions and implementation notes.

Primary planning references remain in the parent workspace:

- `../../docs/第一阶段总工程计划.md`
- `../../docs/milestones/`
- `../../docs/比赛项目开源Agent架构借鉴分析.md`
- `../../docs/第一阶段工程计划（旧版，仅作背景参考）.md`
- `plans/M0-M3-heavyweight-self-check.md`
- `plans/M4-memory-compact-trajectory.md`
- `plans/M5-scheduler-fault-recovery.md`

For future implementation work, read `../../docs/第一阶段总工程计划.md`, the active `../../docs/milestones/**/unit-*.md`, and `../../docs/比赛项目开源Agent架构借鉴分析.md` section `0.4` before coding. The old `../../docs/第一阶段工程计划（旧版，仅作背景参考）.md` is background, not the execution entrypoint.

## Runtime Integration Notes

- CodeWorker uses `apps/code-worker` as a Node sidecar over the vendored `claude-code-best` source tree.
- The sidecar exposes source-level inventory at `GET /workers/code/inventory`; this currently scans tools, commands, permission, compact, MCP, skills, and subagent runtime boundaries without importing Bun-only modules.
- BrowserWorker loads action metadata from vendored `browser-use` source files and exposes the mapped Zyra actions at `GET /workers/browser/actions`.
- Runtime artifacts are written under the configured artifact root, referenced through `ArtifactRef`, and exposed through `GET /artifacts`, `GET /tasks/{task_id}/artifacts`, and `GET /artifacts/{artifact_id}`.
- The previous M0-M5 records are now consolidated into the new M0 foundation baseline. They establish worker, command, skill, symbolic-control, memory, scheduler, fault, and event-log boundaries, but they do not prove heavyweight internalization is complete. New M1 must use these records as input for runtime/memory/scheduler/fault backfill, and new M2 must build the connected control console.
- A wrapper or inventory scan is not enough for later milestones. Each phase should record which source modules were moved or encapsulated, where they live in `zyra`, how they are called, and which verification command proves the integration.
