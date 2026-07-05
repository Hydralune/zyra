# Workers

Heterogeneous worker profiles, worker adapters, isolation modes, and worker execution contracts will live here.

Current M2 worker boundary:

- `CodeWorkerSidecarClient` verifies the vendored `claude-code-best` runtime snapshot through `apps/code-worker` and exposes a source-level runtime inventory for tools, commands, permission, compact, MCP, skills, and subagent boundaries.
- `CodeWorkerRuntime` executes structured `tool_plan` or multi-turn `query_turns` through `CodeQueryLoop`, after checking the sidecar runtime inventory.
- `CodeQueryLoop` carries QueryEngine-inspired runtime semantics: turn limits, per-tool event emission, read-only classification metadata, tool result budget artifacts, and error stop/continue behavior.
- Code worker tool results are converted to normal `EventRecord` entries through the same `tool_result_event` path used by direct API tool calls.
- `BrowserWorkerRuntime` validates the vendored `browser-use` snapshot, loads action/model metadata from `browser_use/tools/views.py` and `browser_use/tools/service.py`, validates a structured `browser_plan`, captures raw HTML, extracted text, structured page state, click/input/search-page actions, artifacts, and browser action events.
- The API exposes this worker path at `POST /tasks/{task_id}/workers/code`.
- The API exposes code worker source inventory at `GET /workers/code/inventory`.
- The API exposes the browser worker path at `POST /tasks/{task_id}/workers/browser`.
- The API exposes browser action metadata at `GET /workers/browser/actions`.

This is not the final TypeScript QueryEngine or Playwright/browser-use Agent migration. These are executable worker loops that bind vendored runtime boundaries to Zyra's permission, artifact, event, and task state contracts. The next M2 step should deepen one of these adapters around the real Claude Code QueryEngine sidecar or browser-use's live browser session/action registry.
