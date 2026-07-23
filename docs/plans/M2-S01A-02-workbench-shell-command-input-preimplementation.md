# M2-S01A-02 workbench shell and command input preimplementation decision

Date: 2026-07-23

Authority:
`docs/milestones/M2-console-demo/slice-01a-02-workbench-shell-command-input-integration.md`

Baseline commit: `3f51da450bd65fa1f20e53613e1b5bc791b811fb`

Parent-unit baseline commit:
`f53bf78287a2b2a6eec74102e7218d664307c137`

This record is frozen before the first production-code change. It applies the
2026-07-22 effective-code/language/migration gate and fixes the source roles,
language path, canonical owners, and migration modes for this slice.

## Source, language, migration, and owner decision

| Role | Source repository and bounded path | Source language | Zyra target | Target language | Migration mode | Canonical owner after the slice | Preimplementation decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| primary | `opencode/packages/app/src/context/layout.tsx`, `utils/session-route.ts` | TypeScript / TSX | `apps/web/src/shell/**`, `apps/web/src/app/**` | TypeScript / TSX | `same_language_crop_and_adapt` | Zyra web shell owns only local route, focus, pane, and overlay state | Preserve normalized routes, explicit workbench layout state, route handoff, focus restoration, and route-failure recovery. Remove Solid context, OpenCode server/session stores, workspace ownership, and source-specific tab state. |
| primary | `opencode/packages/app/src/components/prompt-input.tsx`, `components/prompt-input/{submit,submission-state,history,slash-popover}.tsx` | TypeScript / TSX | `apps/web/src/command/**`, `apps/web/src/components/command-input/**` | TypeScript / TSX | `same_language_crop_and_adapt` | Zyra command runtime owns draft/history/suggestion/queued-preview state only | Preserve submission capture/restore, slash filtering, cursor-aware history, keyboard dispatch, busy/abort behavior, and draft restoration on failure. Replace OpenCode SDK calls and optimistic session store with the existing Zyra typed API client and receipt-backed lifecycle coordinator. |
| supplementary | `claude-code-best/src/screens/REPL.tsx`, `src/components/PromptInput/PromptInput.tsx`, `PromptInputQueuedCommands.tsx`, `src/hooks/useCommandQueue.ts`, `src/utils/{handlePromptSubmit,messageQueueManager}.ts` | TypeScript / TSX | `apps/web/src/command/**`, `apps/web/src/components/command-input/**`, `apps/web/src/components/overlays/**` | TypeScript / TSX | `same_language_crop_and_adapt` | Zyra local command coordinator and queue preview; backend task/run/receipt owners remain authoritative | Preserve a React external-store queue, priority plus FIFO ordering, editable queued commands, queued preview, immediate safe local overlays, active-run queueing, and one guarded execution path. Remove Claude query/tool/session state, Ink rendering, feature flags, source command registry, and source persistence. |
| supplementary | `OpenHands/frontend/src/routes/root-layout.tsx`, `routes/conversation.tsx`, `api/conversation-service/{conversation-service.api,v1-conversation-service.api}.ts` | TypeScript / TSX | `apps/web/src/app/**`, `apps/web/src/components/{task-list,task-detail,status}/**` | TypeScript / TSX | `same_language_bounded_integration` | Zyra shell owns request lifecycle projections only; M1 task/run/event stores remain canonical | Preserve route-scoped loading, empty, not-found, retry, read-only, and cleanup behavior. Use `apps/web/src/api` exclusively; do not import Axios/query cache or create a second task/session store. |
| primary foundation | Existing Zyra `packages/core/typed-api-client` and `apps/web/src/api/**` from M2-S01A-01 | TypeScript | `apps/web/src/shell/**`, `apps/web/src/command/**`, React components | TypeScript / TSX | `same_language_integrate_existing` | Existing typed client owns browser transport; M1 owners retain canonical task/run/event/receipt state | The workbench must call health, readiness, task list/detail/create/cancel/resume through `createZyraApi()` only. No direct `fetch`, alternate client, local canonical task mutation, or fabricated success fallback is allowed. |
| conformance_only | `oh-my-pi` RPC/ACP cancellation and request-correlation behavior already frozen by M2-S01A-01 | TypeScript | `apps/web/test/**` | TypeScript | `conformance_only` | No production owner | Check that cancel targets a real in-flight operation, duplicate submit coalesces, and disabled transport causes visible failure. No RPC/ACP runtime is migrated. |
| excluded_forward_only | OpenClaw | N/A | none | none | `excluded_forward_only` | none | Do not read, restore, depend on, test against, or cite OpenClaw as an implementation or conformance source. |

## Fixed implementation boundaries

- `apps/web/src/api` remains the only browser-facing Zyra API client.
  Workbench code may not call `fetch`, construct HTTP URLs, or create a second
  transport/client/store.
- The workbench shell may own local route selection, pane sizes, overlay stack,
  focus return targets, request phases, reconnect timers, command draft,
  command history, suggestion selection, and queued-preview entries.
- Canonical task, run, event, receipt, cancellation, and resume state remains in
  the existing backend and M1 stores. A successful local projection must always
  be derived from a typed API response and, for mutation, a verified receipt.
- The shell reads task list and task detail from the typed API, treats route IDs
  as untrusted input, and renders explicit loading, empty, not-found, error,
  reconnecting, and retry states. It does not fabricate sample tasks.
- A single command coordinator captures a submission before clearing the
  editor, rejects empty/disabled/remote-unsafe commands, coalesces duplicate
  submits, queues eligible prompts while a task mutation is active, restores
  unchanged drafts on failure, and exposes an abort path tied to the real
  lifecycle coordinator.
- Local JSX-equivalent commands may open only Zyra-owned overlays and change
  shell-local state. Remote-safe commands may invoke the existing typed API.
  Commands without a valid execution policy fail closed and remain disabled.
- Keyboard behavior is deterministic: `Enter` submits, `Shift+Enter` inserts a
  newline, `Escape` closes an overlay or cancels the current operation, arrows
  navigate suggestions/history only at the applicable cursor boundary, and
  focus returns to the initiating control.
- Responsive presentation must not change state ownership or API semantics.
  Large task lists and long command queues use bounded visible projections
  while retaining correct navigation and accessible status announcements.
- Disabling the typed client, the workbench data source, or the command
  coordinator must make the corresponding real behavior fail visibly; no
  fallback may synthesize task or run state.

## Language and effective-code obligations

The primary and supplementary implementation sources are TypeScript/TSX and
the Zyra targets are TypeScript/TSX, so non-zero original-language production
code is mandatory. React is the target rendering boundary required by this
slice. No cross-language rewrite exception is requested.

The slice minimum is 6,000 effective production lines. The parent M2-01A
minimum is 12,000 effective production lines and will be recomputed directly
from the parent baseline through this slice's implementation commit. Final
evidence will bucket changed files into effective behavior, excluded
declarations/DTOs, UI presentation, adapter-only glue, tests, docs, generated
or data content, and vendor-like/source-pool material. Production volume alone
does not close the slice: build plus route, task lifecycle, keyboard, queue,
busy/double-submit, error, reconnect, accessibility, default-path, and
disable-path behavior tests must pass.

## Commit boundary

- `baseline_commit`: `3f51da450bd65fa1f20e53613e1b5bc791b811fb`
- `parent_baseline_commit`:
  `f53bf78287a2b2a6eec74102e7218d664307c137`
- This decision record is intentionally committed before production code.
- `implementation_commit` will be created only after production code and its
  directly related tests pass, but before the completion report, ledger
  updates, and execution-state update.
- The slice implementation range is
  `baseline_commit..implementation_commit`; documentation is excluded from
  effective production counts.
- The parent implementation range is
  `parent_baseline_commit..implementation_commit` and will be audited directly,
  not by summing child-slice reports.
