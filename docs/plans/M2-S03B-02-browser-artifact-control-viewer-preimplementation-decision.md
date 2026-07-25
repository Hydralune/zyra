# M2-S03B-02 Browser Artifact And Control Viewer Pre-implementation Decision

Status: frozen before production changes

Decision date: 2026-07-24

Baseline commit: `7e9483c00cd422afc537b916ab234c9221b53bda`
Slice: `docs/milestones/M2-console-demo/slice-03b-02-browser-artifact-control-viewer.md`

## 1. Canonical ownership and integration boundary

- `BrowserWorkerRuntime`, `BrowserActionApplication`, `BrowserSessionControlRuntime`,
  `JsonBrowserStateStore`, browser observability, and the task `SQLiteStore` remain
  the canonical browser execution, session, receipt, event, and checkpoint owners.
- The web feature consumes the canonical projection store, the existing
  `/tasks/{task_id}/browser-observability` projection, and the M2-S03A artifact
  API. It may keep bounded view selection, virtual window, inflight request, and
  reconnect state, but it must not persist or replay a second browser history.
- Navigate and bounded retry submit a browser plan through the existing
  `POST /tasks/{task_id}/workers/browser` boundary. Stop and reconnect/diagnose
  submit the existing browser lifecycle command. Active-action inspect/cancel
  submit the existing browser action-control command.
- A new typed viewer/control facade may validate identities, normalize receipts,
  and record sealed operator attempts, but it must delegate execution to those
  existing owners. It may not authorize a browser action or mutate browser state
  directly.
- Closing the viewer cancels only frontend reads and view-local work. It never
  stops, detaches, or releases the BrowserWorker session.

## 2. Source, language, and migration decision

| Source role | Source repository and exact paths | Source language | Zyra target | Target language | Migration mode | Counted production boundary |
| --- | --- | --- | --- | --- | --- | --- |
| primary semantic source | `browser-use@18484f23ac96bb955259a1c54530a7d265dfffdb`: `browser_use/browser/views.py`, `browser_use/agent/views.py`, `browser_use/tools/views.py`, `browser_use/screenshots/service.py` | Python | `apps/web/src/features/browser/**`; existing `packages/workers/zyra_workers/browser_*` owners remain unchanged | TypeScript/TSX over the existing Python protocol | bounded `semantic_port` plus typed `protocol_adapter` | Executable projection validation, history/action/result association, screenshot integrity, target/frame and download linkage count. Repeated DTO declarations and transport-only glue do not count. |
| supplementary implementation source | `OpenHands@c105a82387898e744423c8831d412e26495b38a9`: `frontend/src/components/features/browser/browser.tsx`, `frontend/src/components/features/browser/browser-snapshot.tsx` | TypeScript/TSX | `apps/web/src/features/browser/view/**` | TypeScript/TSX | `cropped_migration` and `same_language_component_integration` | The task-bound browser panel, live URL/screenshot composition, empty/error states, action controls, and accessible history navigation count after Zyra state, policy, artifact, and receipt integration. |
| supplementary implementation source | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`: `packages/coding-agent/src/modes/rpc/rpc-types.ts`, `packages/coding-agent/src/modes/rpc/rpc-client.ts` | TypeScript | `apps/web/src/features/browser/control/**`, `apps/web/src/features/browser/receipts/**` | TypeScript | `cropped_migration` and `same_language_component_integration` | Request/response correlation, typed success/failure receipt handling, abort/timeout settlement, and late-result fencing count. Generic RPC command breadth does not migrate. |
| conformance only | Hermes auth/replay behavior identified by the slice contract | Python | browser viewer integration tests | Python/TypeScript tests only | `conformance_only` | No production line quota and no replay/session owner. |
| excluded forward only | OpenClaw | n/a | none | n/a | `excluded_forward_only` | No reading, migration, comparison, dependency, or code quota. |

## 3. Prospective cross-language exception

The active primary source expresses browser state and history as Python models,
while the allocated product surface is a TypeScript/React console. A bounded
cross-language semantic port is therefore necessary and is explicitly authorized
by the slice's language contract. The exception is limited to:

1. strict admission of BrowserWorker projections and canonical task events;
2. derived URL/title/target/frame, screenshot, DOM/AX, action, tool-result,
   download, popup, failure, and causality models;
3. browser control request and receipt validation at the existing typed API
   boundary.

The port does not copy the Python execution loop, CDP transport, permission
runtime, browser store, action registry, screenshot writer, or replay logic.
Python remains non-zero and authoritative through the already-internalized
BrowserWorker main path; this slice does not replace that source-language runtime.
The same-language TypeScript obligations are independently met by cropped
OpenHands and oh-my-pi mechanisms in production UI/control modules.

## 4. Planned Zyra modules

- `apps/web/src/features/browser/projection/**`: defensive event/projection
  admission, target/frame topology, step/action/result association, DOM/AX
  summaries, screenshot and download linkage, causality findings, and crash
  reconciliation.
- `apps/web/src/features/browser/artifacts/**`: browser-specific selection over
  the existing S03A artifact catalog, integrity, redaction, MIME, range, cache,
  and download-permission owners.
- `apps/web/src/features/browser/history/**`: cursor and step/action navigation,
  bounded filtering, and large-history virtualization without a persistent store.
- `apps/web/src/features/browser/control/**`: policy-aware navigate, stop,
  bounded retry, inspect, stale-owner/timeout handling, idempotency, sealed
  denial assessment, and canonical receipt correlation.
- `apps/web/src/features/browser/view/**`: live task-bound browser workbench.
- Additive typed API operations for viewer reads and controls, with the server
  delegating every effect to BrowserWorker and persisting every response event
  through the existing task store.

## 5. Required behavioral evidence

- A real BrowserWorker task action produces a visible browser step associated
  with the same task/run/session, span/tool call, mutation, artifact, and
  canonical events.
- Popup/frame, screenshot hash mismatch, download, DOM/tool-result linkage,
  crash/reconnect, stale ownership, timeout, and viewer-close behavior are
  exercised.
- Navigate/stop/retry/inspect reach the existing BrowserWorker owners and return
  typed receipts. UI-only success is forbidden.
- Sealed mode records one operator intervention attempt, performs no manual
  mutation, enters no approval wait, and preserves
  `human_intervention_count == 0`.
- Prompt-injection-shaped page text remains display data and cannot change
  control policy, permission, identity binding, or secrets.
- Disabling the browser viewer prevents browser projection/control UI behavior
  while leaving BrowserWorker execution intact; disabling BrowserWorker owners
  prevents the claimed control behavior.

## 6. Effective-code accounting intent

The implementation threshold is at least 5,500 effective production lines in
`baseline..implementation`. Production algorithms, validators, projectors,
reconcilers, control settlement, runtime behavior, and connected UI behavior may
count. Tests, docs, comments, generated output, interfaces/type-only declarations,
schema-shaped data, fixture/mock code, presentation-only styling, protocol-only
adapters, and manifest/ledger content are excluded. Every changed file above 500
raw lines, every file with more than 20% of the effective total, and every file
with an excluded bucket above 30% will receive a detailed per-file audit before
the evidence commit.
