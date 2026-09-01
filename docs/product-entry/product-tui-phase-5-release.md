# Product TUI Phase 5 — Productization and Windows evidence

## Final command ownership

- Product: `zyra`, `zyra "<goal>"`, `zyra resume <task|session>`.
- Developer: `zyra dev [<goal>]`, `zyra events <task|session>`.
- Automation preserved: `run`, `ls`, `scenario`, `ui`, `daemon`, exit codes `0..5`.

The product and developer interfaces share `CliApi`, canonical task/session facts, SSE cursor/snapshot recovery, command transport, permission custody, terminal-node lifecycle, artifacts, and workspace delivery. No Codex App Server protocol or second Agent runtime was added.

## Real Windows acceptance

Executed on Windows PowerShell against a real local daemon:

- daemon cold start succeeded with a generation-bound managed process;
- product smoke submitted “测试，收到请回复” as task `task_afdbb03134af`;
- product output showed the canonical final answer and “最终验证通过” without `runtime.*` or alternate-screen control;
- deterministic 80-column replay matched the same canonical final answer;
- `resume task_afdbb03134af` displayed the same completed answer without rerunning the task;
- Web route `/tasks/task_afdbb03134af?api=...` returned HTTP 200 and the API task projection had the same `completed` status and final answer;
- `zyra run` completed task `task_8e6f3e3172ef`; all 147 stdout lines parsed as JSON objects and contained no ANSI;
- `zyra events task_afdbb03134af` returned 143 developer transcript lines with sequence, raw event type, artifact, and revision.

The in-app browser controller had no available browser instance, so screenshot/click validation is recorded as unavailable rather than passed. Production Web route/API validation and the full Web suite remain executable evidence.

## Release gates

- CLI: 90 tests passed; typecheck and Node build passed.
- Web/typed client: 312 tests passed; production build passed.
- Full TypeScript workspace typecheck passed.
- Product-entry Python release tests: 2 passed.
- Release verifier: deterministic double build, Node/Bun entry probes, ten-command surface, dependency closure, exit codes, and Windows platform all reported `ready=true`.
- Linux and macOS hosts were unavailable and are not claimed as tested.

## Handoff

- Full setup and operation: `QUICKSTART.zh-CN.md`.
- One-page classroom demonstration: `DEMO.zh-CN.md`.
- Product architecture and phase evidence: `docs/architecture/product-tui/` and `docs/product-entry/product-tui-phase-*.md`.
