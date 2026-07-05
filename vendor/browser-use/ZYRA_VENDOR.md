# Zyra Vendor Manifest: browser-use

Source workspace repository: `G:\agent-zoo\browser-use`

Project-local location: `zyra/vendor/browser-use`

Purpose:

- Provide the primary M2 source base for `BrowserWorker`.
- Preserve mature implementations for browser session control, DOM/state extraction, action registry, screenshots, trace artifacts, MCP hooks, tools, skills, and sandbox integration.

Initial adapter boundary:

- Python orchestration remains in `zyra/packages/orchestration`, `zyra/packages/runtime`, and `zyra/packages/workers`.
- Browser-use modules should be narrowed behind a `BrowserWorker` adapter that emits Zyra events and artifacts instead of leaking browser-use internal state directly into the global task graph.
- Screenshots, DOM dumps, browser traces, and page text should be written as artifacts and referenced by `ArtifactRef`.

Priority migration groups:

- `browser_use/agent`
- `browser_use/browser`
- `browser_use/controller`
- `browser_use/dom`
- `browser_use/screenshots`
- `browser_use/tools`
- `browser_use/mcp`
- `browser_use/skills`
- `browser_use/sandbox`

This vendor copy is intentionally inside `zyra`; runtime code must not import from `../browser-use`.
