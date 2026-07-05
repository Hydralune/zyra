# Zyra Docs

This directory records project-local design decisions and implementation notes.

Primary planning references remain in the parent workspace:

- `../../docs/比赛项目开源Agent架构借鉴分析.md`
- `../../docs/第一阶段工程计划.md`
- `plans/M0-M3-heavyweight-self-check.md`
- `plans/M4-memory-compact-trajectory.md`

## Runtime Integration Notes

- CodeWorker uses `apps/code-worker` as a Node sidecar over the vendored `claude-code-best` source tree.
- The sidecar exposes source-level inventory at `GET /workers/code/inventory`; this currently scans tools, commands, permission, compact, MCP, skills, and subagent runtime boundaries without importing Bun-only modules.
- BrowserWorker loads action metadata from vendored `browser-use` source files and exposes the mapped Zyra actions at `GET /workers/browser/actions`.
- Runtime artifacts are written under the configured artifact root, referenced through `ArtifactRef`, and exposed through `GET /artifacts`, `GET /tasks/{task_id}/artifacts`, and `GET /artifacts/{artifact_id}`.
- M2 and M3 established the worker, command, skill, symbolic-control, and event-log boundaries. M4 now internalizes MemoryFabric, compact records, and trajectory replay. M5-M6 must use these boundaries to internalize real scheduler, fault-recovery, and control-console capabilities.
- A wrapper or inventory scan is not enough for later milestones. Each phase should record which source modules were moved or encapsulated, where they live in `zyra`, how they are called, and which verification command proves the integration.
