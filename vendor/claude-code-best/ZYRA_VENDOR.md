# Zyra Vendor Manifest: claude-code-best

Source workspace repository: `G:\agent-zoo\claude-code-best`

Project-local location: `zyra/vendor/claude-code-best`

Purpose:

- Provide the primary M2 source base for `CodeWorkerRuntime`.
- Preserve mature implementations for query/session lifecycle, tool loop, permission runtime, compact, MCP, skills, subagents, slash commands, and task/session commands.

Initial adapter boundary:

- Python orchestration remains in `zyra/packages/orchestration`, `zyra/packages/runtime`, and `zyra/packages/workers`.
- TypeScript runtime code from this vendor tree should be exposed through a `CodeWorker` sidecar or narrowed package under `zyra/apps` or `zyra/packages`.
- The final runtime must communicate with Zyra through structured events, task IDs, artifact refs, permission decisions, and command events.

Priority migration groups:

- `src/QueryEngine.ts`, `src/query.ts`
- `src/Tool.ts`, `src/tools.ts`, `src/tools/*`
- `src/hooks/toolPermission`
- `src/services/compact`
- `src/services/mcp`
- `src/commands.ts`, `src/commands/*`
- `src/tools/SkillTool`
- `src/tools/AgentTool`
- `src/skills`
- `src/plugins`, `src/utils/plugins`

This vendor copy is intentionally inside `zyra`; runtime code must not import from `../claude-code-best`.
