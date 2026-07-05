# Zyra Docs

This directory records project-local design decisions and implementation notes.

Primary planning references remain in the parent workspace:

- `../../docs/比赛项目开源Agent架构借鉴分析.md`
- `../../docs/第一阶段工程计划.md`

## Current M2 Runtime Notes

- CodeWorker uses `apps/code-worker` as a Node sidecar over the vendored `claude-code-best` source tree.
- The sidecar exposes source-level inventory at `GET /workers/code/inventory`; this currently scans tools, commands, permission, compact, MCP, skills, and subagent runtime boundaries without importing Bun-only modules.
- BrowserWorker loads action metadata from vendored `browser-use` source files and exposes the mapped Zyra actions at `GET /workers/browser/actions`.
- Runtime artifacts are written under the configured artifact root, referenced through `ArtifactRef`, and exposed through `GET /artifacts`, `GET /tasks/{task_id}/artifacts`, and `GET /artifacts/{artifact_id}`.
- M2 is still in progress; these boundaries are evidence-bearing adapters, not the final QueryEngine or full Playwright/browser-use agent migration.
