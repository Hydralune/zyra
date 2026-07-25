# M2-S04A-01 Command Input And Queue Control Pre-implementation Decision

Status: frozen before production changes

Decision date: 2026-07-25

Baseline commit: `53b002bac1e97eab23b7553d344da068dd8dd3c9`

Slice: `docs/milestones/M2-console-demo/slice-04a-01-command-input-queue-control.md`

## 1. Canonical ownership and integration boundary

- The existing Python `RuntimeControlDispatcher`, `ControlRequestStore`,
  `PromptQueueRuntime`, task `SQLiteStore`, permission runtime, owner handlers,
  canonical event log, and M2-01B `CanonicalProjectionStore` remain the only
  command admission, queue, permission, state mutation, receipt, and browser
  projection owners.
- TypeScript may own command text parsing, typed argument validation, palette
  ranking, keyboard interaction, request construction, transport correlation,
  in-flight cancellation, bounded local overlay lifecycle, and derived queue
  view models. It must not decide permission, persist a second queue, or infer
  committed success without a typed backend receipt and canonical event.
- Busy-run enqueue, cancellation, retry, and reconnect restoration use typed API
  operations over the existing Python owners. The browser may retain a pending
  request correlation while an HTTP call is in flight, but queue membership and
  terminal state come only from backend queue/request receipts and M2-01B
  projections.
- `/btw` enters the existing `SideQuestionRuntime`. It is tool-disabled,
  single-turn, separately metered, and cannot append to the main query session.
- `/status`, `/graph`, `/trace`, `/artifacts`, `/permissions`, `/btw`,
  `/inject`, `/change`, `/verify`, `/eval`, and `/doctor` all enter
  `TaskApi.controlCommand`; none may fall through to ordinary task creation or
  normal chat.

## 2. Source, language, and migration decision

| Source role | Source repository and exact paths | Source language | Zyra target | Target language | Migration mode | Counted production boundary |
| --- | --- | --- | --- | --- | --- | --- |
| primary implementation | `claude-code-best@c57f5a29e88e9a814bea47abeb9a0a6f725dc102`: `src/screens/REPL.tsx`; `src/components/PromptInput/{PromptInput.tsx,PromptInputQueuedCommands.tsx}`; `src/hooks/{useCommandQueue,useQueueProcessor}.ts`; `src/utils/{handlePromptSubmit,messageQueueManager,queueProcessor,sideQuestion,forkedAgent}.ts`; `src/types/textInputTypes.ts`; `src/commands/btw/btw.tsx` | TypeScript/TSX | `packages/commands/src/**`; `apps/web/src/features/commands/**`; connected command input | TypeScript/TSX | `cropped_migration` plus `retained_control_flow_adapt` | Command parsing, external-store subscription, guarded submit, priority/FIFO projection, queue preview, cancellation/retry correlation, local overlay gating, and side-question lifecycle count after replacement with Zyra identities and canonical backend ownership. Claude query/session/tool/Ink state does not migrate. |
| supplementary implementation | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda`: `packages/app/src/context/command.tsx`; `packages/app/src/components/{command-palette,dialog-command-palette-v2}.tsx`; `packages/app/src/components/prompt-input/{slash-popover,submit}.tsx` | TypeScript/TSX | `packages/commands/src/{registry,palette,receipts}/**`; `apps/web/src/features/commands/**` | TypeScript/TSX | `cropped_migration` plus `same_language_component_integration` | Registry composition, stable palette identity, grouping/ranking, keyboard selection, duplicate submit settlement, typed request/receipt correlation, and disabled state count. OpenCode stores, SDK, session, provider, and permission owners do not migrate. |
| conformance only | `hermes-agent` gateway auth/replay behavior; AgentScope session interaction behavior; oh-my-pi typed RPC receipt behavior | Python/TypeScript | focused TypeScript and Python integration tests | tests only | `conformance_only` | No production code quota and no owner. |
| reference only | upstream CLI/RPC command surfaces named by the slice | mixed | self-review negative checks | none | `reference_only` | No production code quota; source CLI/RPC is not a Zyra production path. |
| excluded forward only | OpenClaw | n/a | none | n/a | `excluded_forward_only` | No reading, comparison, migration, dependency, or quota. |

## 3. Planned Zyra modules

- `packages/commands/src`: normalized registry, lexer/parser, typed argument
  binder, availability policy, palette index, request/idempotency builder,
  strict receipt admission, command/event correlation, queue projection, and
  control coordinator.
- `apps/web/src/features/commands`: task-bound command surface, canonical queue
  selectors, keyboard controller, overlay/result projection, receipt list, and
  connected React queue/control panel.
- `apps/web/src/api/task-api.ts` and `apps/api/zyra_api/main.py`: additive typed
  queue snapshot/cancel protocol and control request delivery fields, delegating
  every effect to `RuntimeControlDispatcher` and `PromptQueueRuntime`.
- Existing `apps/web/src/command/**` and command input become adapters into the
  new command runtime for the required control commands. They retain only
  task-create/navigation/help behavior that is outside this slice.

## 4. Required behavior and failure evidence

- Duplicate submit must reuse the same in-flight operation or return the
  backend replayed receipt; a conflicting body with the same idempotency key
  must fail.
- Busy serial commands must appear from the backend as queued and restore after
  reconnect. Priority and sequence must produce deterministic FIFO within one
  priority.
- Cancel must call the backend dispatcher/queue owner. Retry must create a new
  request identity linked to the failed/cancelled request and then settle from
  a new canonical receipt.
- Malformed typed arguments and unavailable commands must be rejected before
  transport; permission and mutation decisions remain backend-owned.
- `/btw` must return one tool-free side result with usage/error/close state and
  leave the main session transcript/revision untouched.
- Every applied control must be reverse-resolvable through command ID, request
  ID, event ID, span/correlation metadata, and state mutation/checkpoint facts.
- Disabling the TypeScript coordinator must block the claimed browser behavior;
  disabling the Python dispatcher or queue owner must make the real command or
  queue-control integration tests fail.

## 5. Effective-code accounting intent

The slice threshold is at least 7,500 conservative effective TypeScript/React
production lines in `baseline..implementation`. Executable parsing, validation,
ranking, policy, correlation, projection, orchestration, reconnect, queue
control, overlay behavior, and connected UI behavior may count. Tests, docs,
comments, blanks, type-only declarations, generated output, schema/data tables,
fixture/mock code, transport-only glue, presentation-only JSX/CSS, ledger
records, and Python protocol adapters are excluded. Every changed file above
500 raw lines, every file contributing more than 20% of effective production,
and every file with an excluded bucket above 30% will receive an explicit
per-file audit before the evidence commit.

## 6. Risk decision

This slice adds typed operations within already allocated command and queue
owners. It does not transfer canonical ownership, change permission policy,
introduce an external dependency/process/port/MCP/plugin/dynamic import, or
change task persistence semantics. The API additions are additive. Therefore no
high-risk upgrade is triggered; focused behavior, failure-path, adjacent Web,
typed transport, API command, dependency/path, and effective-line verification
are required. Parent cumulative and broader cleanroom closure remain assigned
to M2-S04A-02 and the M2-04 aggregate layer.
