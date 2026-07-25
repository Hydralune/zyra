# M2-S04A-02 Permission / Sealed Control Preimplementation Decision

Status: frozen before production changes

Decision date: 2026-07-25

Slice: `M2-S04A-02`

Parent unit: `M2-04A`
Zyra implementation baseline: `33327d49ef1fab62d314709e169eebc781eb2a9c`

## 1. Scope and protected boundary

This decision freezes the source roles and target ownership before any
production change for `M2-S04A-02`.

The slice adds a real permission and sealed-control surface to the existing
workbench. It does not reopen `M2-S04A-01`, replace the permission evaluator,
create a browser-owned pending queue, or make a UI callback the authority for
tool execution.

The protected M1 runtime already divides permission custody as follows:

- `typescript.PermissionCoordinator` is the canonical evaluator and exact-call
  resume/permit owner on the current default runtime path.
- Python `PermissionStateStore` / `PermissionControlPlane` own durable
  session-custody and HTTP transport records where that transport is used.
- the Python HTTP API forwards E02 approval responses and does not make a
  fallback allow/deny decision;
- the Web console is a projection and authenticated response client only.

This is the concrete interpretation of the slice requirement that the existing
backend permission runtime remain canonical: neither React nor a new local
store receives decision ownership.

## 2. Source-role decision

| Role | Repository and pinned revision | Exact source files read | Source language | Target | Migration mode | Owner result |
| --- | --- | --- | --- | --- | --- | --- |
| primary | `claude-code-best@c57f5a29e88e9a814bea47abeb9a0a6f725dc102` | `src/components/permissions/PermissionPrompt.tsx`; `src/components/permissions/PermissionRequest.tsx`; `src/components/permissions/PermissionDialog.tsx`; `src/components/permissions/FallbackPermissionRequest.tsx`; `src/components/permissions/PermissionRequestTitle.tsx`; `src/components/permissions/ComputerUseApproval/ComputerUseApproval.tsx`; `src/components/permissions/WorkerPendingPermission.tsx`; `src/hooks/toolPermission/PermissionContext.ts`; `src/hooks/toolPermission/handlers/interactiveHandler.ts`; `src/bridge/bridgePermissionCallbacks.ts` | TypeScript / TSX | `apps/web/src/features/permissions/**`; bounded response-proof additions under `packages/runtime/claude-runtime/src/permission/**` | `cropped_migration` plus `retained_control_flow_adapt` | Keep the request-detail/prompt split, explicit one-shot response claiming, active-request race handling, tool-specific warning surface, and backend callback boundary. Replace process-local React queue ownership with Zyra API/event projections and exact backend identity. |
| supplementary | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | `packages/app/src/context/permission.tsx`; `packages/app/src/context/permission-auto-respond.ts`; `packages/app/src/pages/session/composer/session-permission-dock.tsx`; `packages/app/src/components/dialog-command-palette-v2.tsx` | TypeScript / TSX | `apps/web/src/features/permissions/**` | `cropped_migration` | Reuse bounded dock/list interaction, responding-state exclusion, reconnect list refresh, stable session/directory scoping, and accessible action ordering. Do not migrate auto-accept as a policy owner or persistent browser permission rule. |
| conformance only | `hermes-agent@44ddc552f5e054759a6970af8997ea588a9d81c9` | `tools/approval.py`; `tools/write_approval.py`; `gateway/session.py`; gateway approval response paths described by `source-graphs/hermes-agent` | Python | adversarial tests and self-review only | `conformance_only` | Compare timeout/interrupt release, secret-redacted presentation, and concurrent response behavior. Its blocking queue and platform slash commands do not enter production. |
| conformance only | `agentscope@b6698c5dbaa1aa916925e27402767f45e2405fa4` | `src/agentscope/permission/_types.py`; `_context.py`; `_decision.py`; `_rule.py`; `_engine.py`; HITL projection paths described by `source-graphs/agentscope` | Python | adversarial tests and self-review only | `conformance_only` | Check mode/rule ordering and UI event projection. It does not become a second permission engine or state owner. |
| reference only | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | approval/tool execution and ACP/RPC control paths recorded in `source-graphs/oh-my-pi/batch-02-tools-permission-mcp-skills.md` and `batch-05-provider-rpc-control.md` | TypeScript | rejection criteria and self-review only | `reference_only` | Preserve execute-boundary approval and reject parent-task/yolo scope expansion in sealed mode. No production migration quota. |
| reference only | `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | conversation pending-action and confirmation UI patterns already indexed by the M2 source graph | TypeScript / React | UI composition review only | `reference_only` | No state, runtime, or code migration obligation. |
| reference only | `claude-reviews-and-fine-tuning` | `14-ui-state-management.md`; `14-ui-state-rendering.md`; `13-bridge-system.md` | Markdown analysis | state/rendering review only | `reference_only` | Used to reject duplicated UI state and stale bridge callbacks; it is not an implementation source. |
| reference only | `Dive-into-DeepResearch` | human-oversight / long-running control analysis selected by the parent source graph | Markdown analysis | sealed-mode review only | `reference_only` | Used only to test no-human/no-hang behavior. |
| excluded | OpenClaw | none | none | none | `excluded_forward_only` | No source read, migration, adapter, test quota, runtime dependency, or path reference will be introduced. |

There is one primary implementation source and one supplementary source for
this interaction domain. Conformance and reference sources receive no
production migration quota.

## 3. Retained source mechanisms

The implementation retains and adapts these source mechanisms:

1. A permission request is an identity-bound object, not a generic modal.
2. The selected request is rendered by kind while fallback details remain
   available for unknown tools.
3. Allow and deny are mutually exclusive one-shot actions. A response in
   flight disables all competing controls.
4. Local, remote, timeout, reconnect, and cancellation paths race through one
   claim gate; a late loser cannot resolve the request again.
5. Request delivery and decision are separate phases.
6. A reconnect re-queries pending backend state instead of trusting retained
   component state.
7. Presentation distinguishes allow-once from any persistent policy update.
   This slice exposes exact-call allow/deny only; it does not smuggle a new
   “always allow” rule through the UI.
8. Tool-specific warnings are projections over the request binding and never
   rewrite executable arguments.

The implementation deliberately rejects these upstream mechanisms:

- browser-persisted auto-accept as a decision policy;
- a process-local pending queue as canonical permission truth;
- arbitrary UI-modified tool arguments;
- bridge responses that carry only `allow` or `deny` without exact request
  binding;
- classifier or LLM output as the final permission decision;
- yolo/parent-task authority expansion in a sealed run;
- a sealed `ask` state that waits for a person.

## 4. Target module map

Production work is constrained to these Zyra-owned boundaries:

- `packages/runtime/claude-runtime/src/permission/**`
  - add deterministic response challenge/proof verification around the
    existing approval envelope;
  - retain `PermissionContinuationRuntime`, execution permits, response replay,
    expiry, policy revision, mode revision, and exact physical-call checks as
    canonical enforcement.
- `packages/runtime/claude-runtime/src/e02/api-port-runtime.ts`
  - expose only a redacted permission envelope;
  - verify the console response proof before calling the existing resume path.
- `packages/integrations/zyra_integrations/e02_ports.py` and
  `apps/api/zyra_api/main.py`
  - transport response binding fields without evaluating them in Python;
  - continue server-stamping the actor and custody channel.
- `apps/web/src/api/permission-api.ts`
  - open/resume permission session custody in memory;
  - list/detail/respond/expire through the typed HTTP transport;
  - never persist bearer custody tokens.
- `apps/web/src/features/permissions/**`
  - normalize and redact backend projections;
  - build pending/detail/timeline/sealed/intervention view models;
  - calculate the deterministic response proof;
  - claim one response race and reconcile the canonical receipt;
  - provide selectors consumed by task, timeline, browser, terminal, and
    command surfaces without owning a second pending store.
- `apps/web/src/app/runtime.ts` and task-detail composition
  - bind and close the permission controller with the selected task;
  - render the real permission workbench on the task route.

Tests will live beside existing TypeScript runtime tests, Web behavior tests,
and focused Python API integration tests. Fixed response data, mocks, and
fixtures are excluded from effective production code.

## 5. Response proof and security boundary

An active redacted envelope supplies:

- request, envelope, run, task, session and exact tool-call identity;
- session, policy and mode revisions;
- tool namespace/server/operation;
- arguments digest and request fingerprint;
- canonical owner, expiry, response version and response nonce.

The Web client sends:

- `response_id` / idempotency key;
- server-stamped responder request plus display actor hint;
- effect;
- all identity echoes;
- response version and nonce;
- a SHA-256 response binding proof over the canonical echo fields.

The proof is an integrity binding, not a replacement for authentication.
Session custody remains the authentication boundary and the API stamps the
actual actor. The backend rejects stale version/revision, wrong nonce, wrong
digest, cross-session, cross-task, wrong owner, changed effect, response-ID
reuse, expired request, and duplicate payload mismatch before exact resume.

Tool arguments and secrets are never returned in the permission projection.
Only a redacted semantic preview and the backend-computed arguments digest are
shown. Browser, MCP, remote-fetch, plugin/skill, shell and terminal requests
receive deterministic injection/supply-chain warnings derived from safe
binding fields.

## 6. Interactive and sealed product modes

Interactive mode:

- identifies the server-stamped operator;
- allows exact-call allow or deny;
- records feedback only as bounded explanatory metadata;
- resumes only through the canonical backend receipt.

Sealed mode:

- displays the frozen policy hash and revision;
- disables and rejects human allow, deny, steer, retry and mode-changing
  controls;
- converts unresolved `ask` to deterministic deny/replan in the existing
  runtime path;
- displays `human_intervention_count = 0` and flags any attempted intervention
  as rejected ledger evidence;
- never exposes a control whose success could advance a sealed benchmark.

## 7. Risk and validation decision

This slice does not transfer a canonical owner, change the default permission
mode, or add an external process, package, port, plugin, MCP server, Docker
context, dynamic import, or root-repository dependency.

It does strengthen a cross-runtime public response contract. That is treated
as a focused permission-contract risk: the implementation must run the
permission runtime behavior tests, Web integration tests, API approval
integration tests, browser/terminal/control adjacency tests, TypeScript
typecheck, and Web build. The full M2-04 cleanroom and aggregate audit remain
at the documented `04B` closure unless a regression escapes those focused
checks.

Disable proof is mandatory: disabling the new permission Web controller or
response-proof verifier must make the corresponding real approval behavior
fail or become unavailable, while ordinary task/event rendering remains
operational.

## 8. Effective-code accounting

The slice minimum is `7,500` effective production lines. Direct slice and
cumulative parent accounting will both be computed from Git numstat:

- direct slice baseline: `33327d49ef1fab62d314709e169eebc781eb2a9c`;
- parent baseline: `53b002b...` as frozen by `M2-04A`;
- original-language production credit must be non-zero;
- Python transport additions are adapter-only unless they introduce
  independently testable backend behavior;
- tests, docs/comments, generated output, schema/data, fixtures, presentation
  CSS, type-only declarations, ledger records and evidence scripts are
  excluded from effective production;
- every production file over 500 raw added lines, every file contributing more
  than 20% of effective code, and every file with more than 30% excluded lines
  requires an explicit per-file review in the slice self-review.

If the real product responsibility cannot meet the threshold, implementation
must stop for a planning correction rather than add aliases, forwarding
wrappers, duplicated branches, or inert code.
