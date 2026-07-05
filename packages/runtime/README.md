# Runtime

Shared execution runtime modules will live here, including session lifecycle, context windows, tool calls, permissions, and interruption handling.

Current M2 runtime boundary:

- `ContextSessionRuntime` manages `/clear`, `/rewind`, `/resume`, `/context`, and `/memory` semantics through checkpoint metadata while keeping the event log append-only.
- Context session state stores an active session id, generation, visible event segments, resumable snapshots, and command history under `TaskState.metadata.context_session`.
- `ToolRegistry` describes available tools and their input schemas.
- `ToolPermissionPolicy` gates workspace reads/writes and shell execution with allow/deny/ask decisions.
- `JsonPermissionStore` persists permission rules and pending approval requests in `tmp/permissions.json`.
- `ToolExecutor` executes `file_read`, `file_write`, `file_edit`, `shell`, `browser`, `web_search`, `trace`, `checkpoint`, and `artifact_write` inside a controlled workspace.
- `browser` provides a single-call HTML/URL state snapshot and text extraction tool; multi-step browser plans still belong to `BrowserWorkerRuntime`.
- `web_search` currently provides controlled research retrieval over workspace files, local file URLs, and explicitly allowed network URLs; it writes a trace artifact for downstream evidence handling.
- `trace` reads task events through an injected event reader and can write bounded trace artifacts for worker or API-level evidence inspection.
- `checkpoint` reads task checkpoint summaries through an injected checkpoint reader and can persist full checkpoint artifacts when requested.
- Shell commands that require `ask` generate pending permission requests when a permission store is attached; approved/denied requests can optionally create persistent rules.
- Large tool outputs are written through `LocalArtifactStore` and returned as `ArtifactRef` instead of being pushed into agent messages.
- `LocalArtifactStore` also describes stored artifacts and serves bounded text previews, so API/UI consumers do not need to trust raw filesystem paths.
- API tool calls use `POST /tasks/{task_id}/tools` and are recorded as task events through `tool_result_event`.
- Artifact control endpoints expose task-scoped evidence through `GET /tasks/{task_id}/artifacts`, `GET /artifacts`, and `GET /artifacts/{artifact_id}`.

Deeper CodeWorker/QueryEngine integration and full interactive browser automation are still M2 work items; the current runtime package provides the shared context, permission, tool, and artifact substrate those workers call through.
