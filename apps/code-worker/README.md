# Zyra Code Worker Sidecar

This sidecar is the TypeScript/Node boundary for the vendored `claude-code-best` runtime.

Current M2 status:

- Uses Node standard library so the boundary can be verified without Bun.
- Reads only `zyra/vendor/claude-code-best`.
- Exposes a JSON-line protocol for health and vendor snapshot checks.
- Is used by `packages/workers/zyra_workers/CodeWorkerRuntime` before executing a Zyra tool plan, so the Python worker loop stays tied to the vendored Claude Code runtime boundary.

Next migration steps:

- Replace the inspection-only snapshot with a narrowed adapter around `QueryEngine`.
- Route tool calls through Zyra `ToolPermissionRuntime`.
- Emit Zyra `EventRecord`, `ArtifactRef`, and `ToolResult` payloads.
- Keep runtime code inside `zyra`; never import from `../claude-code-best`.
