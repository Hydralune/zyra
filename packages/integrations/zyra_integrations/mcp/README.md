# Zyra MCP client runtime

This package is the Zyra-owned MCP client implementation for `M1-S03B-01`.
It does not load or execute source code from workspace sibling repositories.

The runtime is one continuous path:

`McpConfigStore -> McpConnectionRuntime -> McpAuthRuntime -> capability catalog -> McpToolProjectionRuntime/McpResourcePromptRuntime -> 03A permission + 02C execution -> output artifacts/events -> McpInstructionsRuntime -> 02D compact restore`.

State custody is split deliberately:

- `McpRuntimeStateStore` owns atomic config provenance, policy, approvals,
  connection/catalog metadata, task handles, elicitation metadata and journal.
- `FileCredentialVault` owns credential material separately; normal snapshots,
  events and API bodies contain only opaque references and digests.
- live transports, callbacks and execution grants are process-local and must be
  rebuilt after restart.
- `CodeWorkerSessionStore.runtime_state["mcp_runtime"]` owns per-session
  instruction/restore handoff. Restored connections are never considered live.

The active source decisions are encoded in `source_audit.py`. Claude Code
config/client/auth/tool/resource/output mechanisms, Agent Framework pagination,
sampling and long-task lifecycle, and selected opencode catalog/session patterns
were decomposed into these modules. AgentScope remains an interoperability
adapter/reference; Hermes contributes lifecycle/security hardening but its
default-allow sampling behavior is intentionally not copied. Claude
`mcpSkills.ts` is a no-op stub and is not treated as an active skill runtime.

Primary verification:

```powershell
python -m unittest discover -s tests -p "test_mcp*.py"
python scripts/smoke_mcp_runtime.py
python scripts/sync_mcp_source_ledger.py --check
```

The HTTP control plane is `apps/api/zyra_api/mcp_api.py`; CodeWorker wiring is
in `packages/workers/zyra_workers/code_worker_runtime.py`. Projected MCP tools
cannot execute in `ToolExecutor` without a one-use grant from the 03A permission
runtime, including tools annotated read-only.

HTTP mutation authority is intentionally narrower than the internal runtime
API. A dynamic STDIO server is accepted only when `command` is an existing,
executable absolute path exactly present in `ZYRA_MCP_STDIO_COMMAND_ALLOWLIST`;
`args` and `env` must be empty until an exact invocation-template policy exists.
A dynamic streamable-HTTP server is accepted only when its complete URL is an
exact entry in `ZYRA_MCP_HTTP_ENDPOINT_ALLOWLIST` and its host is a public
IP literal. Domain names and loopback/private/link-local/reserved addresses are
rejected, and this facade performs no DNS lookup, preventing DNS-rebind/TOCTOU
authorization drift. Trusted domain connectors must use a separately governed
configuration path; an HTTP permission cannot widen these deployment rules.
