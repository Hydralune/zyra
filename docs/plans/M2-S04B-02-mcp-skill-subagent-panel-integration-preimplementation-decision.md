# M2-S04B-02 MCP / Skill / Subagent Panel Integration Preimplementation Decision

Status: frozen before production changes

Decision date: 2026-07-25

Slice: `M2-S04B-02`

Parent unit: `M2-04B`

Zyra implementation baseline: `2d3d8c16075f1dda298831578cd339478f63afad`

M2-04B parent baseline: `af6f6dc0c66a846c739b73e8c6006c67de219ba8`

M2-04 numeric-stage baseline: `53b002bac1e97eab23b7553d344da068dd8dd3c9`

## 1. Scope and protected ownership

This decision freezes sources, languages, migration modes, target modules, and
state custody before production implementation begins.

The slice productizes MCP, skill, and subagent drill-down and controls in the
existing M2 workbench. It closes M2-04B and the M2-04 numeric stage. It does not
create browser-owned MCP, skill, subagent, permission, task, session, command,
or event stores.

Protected owners remain:

- the TypeScript MCP connection/config/auth/resource/prompt/tool owners already
  internalized under `packages/runtime/claude-runtime`;
- the TypeScript SkillTool/plugin/invocation/version owners already internalized
  under `packages/runtime/claude-runtime`;
- the TypeScript AgentTool logical child-run/control/checkpoint owners already
  internalized under `packages/runtime/claude-runtime`;
- Python durable MCP, skill, subagent, task, event, artifact, scheduler,
  workspace, and recovery stores where the existing cross-runtime contract
  assigns physical custody;
- the M1 permission runtime and exact one-use permit path;
- the M2-01B `CanonicalProjectionStore` and selectors as the sole committed
  browser projection truth;
- the M2-04A command surface, queue, receipt, permission, sealed-mode, and
  intervention paths for every operator control.

Feature runtimes may build bounded indexes, pagination windows, admission
explanations, reconnect schedules, and disposable selections. They may not
commit backend state or interpret an optimistic browser receipt as completion.

## 2. Source-role and language decision

| Role | Repository and revision | Exact source paths selected | Source language | Zyra target | Target language | Migration mode | Canonical-owner result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| primary | `claude-code-best@c57f5a29e88e9a814bea47abeb9a0a6f725dc102` | `src/components/mcp/MCPListPanel.tsx`; `MCPReconnect.tsx`; `CapabilitiesSection.tsx`; `ElicitationDialog.tsx`; `src/services/mcp/useManageMCPConnections.ts`; `types.ts`; `auth.ts`; `src/components/skills/SkillsMenu.tsx`; `src/skills/loadSkillsDir.ts`; `src/utils/skills/skillChangeDetector.ts`; `src/tools/AgentTool/UI.tsx`; `src/tasks/BackgroundTasksDialog.tsx`; `src/tasks/types.ts`; `src/tasks/stopTask.ts`; `src/screens/REPL.tsx`; `src/hooks/useCommandQueue.ts`; `src/utils/messageQueueManager.ts` | TypeScript / TSX | `apps/web/src/features/mcp/**`; `apps/web/src/features/skills/**`; `apps/web/src/features/subagents/**`; existing app/workbench composition | TypeScript / TSX | `cropped_migration`, `retained_control_flow_adapt`, and `same_language_component_integration` | Retain connection lifecycle, needs-auth and reconnect state, capability/resource/prompt drill-down, elicitation, skill discovery/change/version display, AgentTool parent-child task lifecycle, stop/control settlement, and command-to-overlay handoff. Replace process-local state with strict Zyra selector inputs and owner receipts. |
| supplementary | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | `packages/schema/src/mcp-event.ts`; `packages/schema/src/skill.ts`; `packages/schema/src/agent.ts`; `packages/tui/src/routes/session/subagent-footer.tsx`; `sidebar.tsx`; `packages/server/src/handlers/skill.ts`; `agent.ts`; `packages/session-ui/src/components/basic-tool.tsx`; `tool-error-card.tsx`; `tool-count-summary.tsx` | TypeScript / TSX | the three feature trees and shared cross-panel navigation | TypeScript / TSX | `cropped_migration` and `same_language_component_integration` | Supplement typed status/error/result presentation, compact tool/capability summaries, session-scoped subagent navigation, and panel settlement. No OpenCode session, MCP, skill, agent, or cache owner is migrated. |
| supplementary | `hermes-agent@44ddc552f5e054759a6970af8997ea588a9d81c9` | `tools/mcp_tool.py`; `tools/mcp_oauth_manager.py`; `tools/mcp_oauth.py`; `tools/skill_provenance.py`; `tools/skills_guard.py`; `tools/skill_usage.py`; `agent/skill_utils.py`; `agent/skill_commands.py`; `ui-tui/src/components/skillsHub.tsx` | Python and TypeScript / TSX | TypeScript MCP auth and skill provenance/supply-chain admission plus conformance tests | TypeScript / TSX | prospectively bounded `semantic_port` for OAuth/provenance/admission facts and `cropped_migration` for view behavior | Fill the primary UI's explicit OAuth expiry/refresh diagnostics and skill provenance/supply-chain risk gaps. No Hermes process, database, OAuth token store, skill installer, hub, or gateway becomes an owner. |
| conformance only | Agent Framework and AgentScope at their pinned source-graph revisions | AG-UI approval/history; workflow/checkpoint; scoped worker/KB lifecycle contracts | Python / C# | tests and negative contract audit only | none | `conformance_only` | Validate approval, scope, lineage, and checkpoint correlation without a second panel runtime or state owner. |
| reference only | browser-use and Oh My Pi at pinned source-graph revisions | browser disconnect/restore, MCP reconnect, TaskTool child-session, typed settlement and heartbeat contracts | Python / TypeScript | failure/adversarial review only | none | `reference_only` | Review crash, late-result, reconnect, viewer-close, and heartbeat behavior. No implementation quota or runtime dependency. |
| excluded | OpenClaw | none | none | none | none | `excluded_forward_only` | No source read, role, ledger row, migration, test quota, dependency, path, or process is introduced. |

There is one primary and two bounded supplementary implementation sources. The
Hermes Python-to-TypeScript exception is prospective and limited to projection
and admission mechanics. It does not transfer OAuth token custody, primary MCP
control flow, skill invocation control flow, runtime custody, transaction,
lease, idempotency, or restore semantics. Equivalent behavior is verified by
expiry/refresh, provenance mismatch, quarantine, permission, and disable tests.

## 3. Retained mechanisms

The implementation retains and adapts:

1. MCP server rows preserve configuration provenance, transport, status,
   capability revisions, disabled reasons, last errors, retry/backoff, and
   reconnect eligibility.
2. Tool, resource, and prompt catalogs have independently bounded,
   revision-bound pagination and reject cursors from another server,
   capability revision, task, run, or session.
3. Auth status exposes presence, provider, expiry, scope class, and refresh
   eligibility but never a token, client secret, authorization header, code
   verifier, callback query, or environment value.
4. Elicitation is schema-bound, task/run/session/server/request scoped, subject
   to permission, unavailable in sealed mode, and settles only from the owner
   receipt.
5. Toggle, auth refresh, reconnect, and catalog refresh remain pending until a
   later canonical capability/config/connection projection proves the effect.
6. Skill detail retains body sections, declared resources, allowed tools,
   provenance, version, content hash, dependency graph, staged approval,
   supply-chain findings, runtime assets, and invocation settlement.
7. Skill update compares expected and observed hashes, refuses unreviewed
   dependencies or provenance drift, passes through permission/command
   handoff, and reconciles a canonical revision before success.
8. Subagent views preserve parent/child/depth/scope, lifecycle, heartbeat,
   budget, worker/route/checkpoint, result/error, crash, reconnect, and
   late-result quarantine.
9. Kill and steer bind exact task/run/session/parent/child/attempt/owner
   identities, expected revision, nonce, and idempotency key before permission
   and command submission.
10. Browser panel close only detaches disposable subscriptions and never kills
    a server, skill invocation, or subagent. Reopen rebuilds from M2-01B state.

## 4. Rejected mechanisms

The implementation rejects:

- feature-local canonical stores, localStorage, IndexedDB, React-context truth,
  source-runtime databases, polling caches, or label-based joins;
- static/mock MCP, skill, or subagent listing as behavior evidence;
- raw credentials, token values, callback codes, authorization URLs containing
  secrets, environment values, private skill resources, or unredacted tool
  arguments in browser state;
- MCP control that bypasses the existing MCP owner or permission runtime;
- browser-side skill install/update/invoke or supply-chain acceptance;
- browser-side subagent process, task, budget, heartbeat, checkpoint, kill, or
  steer ownership;
- optimistic receipt success without later canonical state reconciliation;
- sealed mutation, approval wait, elicitation wait, steering, retry, or
  fabricated human intervention;
- fallback to an old listing, direct HTTP mutation, source CLI, fixture, or
  replay when an owner, binding, permission, or projection module is disabled;
- OpenCode, Hermes, Agent Framework, AgentScope, browser-use, or OMP parallel
  state stores; any OpenClaw read or dependency.

## 5. Target module map

Production work is constrained to:

- `apps/web/src/features/mcp/**`: strict projection admission, config/auth
  redaction, capability/catalog pagination, elicitation, lifecycle controls,
  reconnect/backoff, effect reconciliation, controller, and workbench.
- `apps/web/src/features/skills/**`: strict projection admission, provenance
  and hash verification, dependency/supply-chain audit, staged approval,
  resource/tool detail, update/invoke effect reconciliation, controller, and
  workbench.
- `apps/web/src/features/subagents/**`: strict projection admission, hierarchy,
  scope and budget analysis, heartbeat/crash/reconnect, result/error and
  late-result handling, permission-bound kill/steer reconciliation,
  controller, and workbench.
- `apps/web/src/app/runtime.ts`, `components/tasks/task-detail.tsx`, and existing
  workbench styles: instantiate and render one disposable controller per
  workbench runtime.
- existing M2-04A command/permission and M2-01B selector boundaries: add only
  typed handoff metadata and composition needed by `/mcp`, `/skills`, `/agents`,
  and `/tasks`; no new queue or projection owner.

No API facade is planned. The existing canonical event and command paths must
carry the required facts. If they do not, implementation stops for a
prospective owner-boundary decision rather than creating browser truth.

## 6. Control, sealed, reconnect, and restore contract

Every mutating control:

1. resolves current task, run, session, owner, entity, attempt, and canonical
   revision;
2. performs deterministic local admission for immediate feedback;
3. submits through `CommandSurfaceRuntime` and the existing permission runtime;
4. displays queued, permission-pending, running, failed, quarantined, or
   receipt-committed phases;
5. records nonce, idempotency, request, command, event, span, checkpoint,
   artifact, capability, skill, or child-run references without label joins;
6. waits for a later M2-01B projection to reconcile the requested semantic
   effect.

In sealed mode, all human MCP, skill, subagent, elicitation, approval, steer,
retry, or kill attempts are rejected before owner mutation or wait. The
existing intervention path records the rejected attempt and autonomous
fail-closed/recovery result while `human_intervention_count` remains zero.

Disconnect cancels browser work, invalidates cursors and transient optimistic
effects, and disables controls. Reconnect rebuilds from the canonical
projection and command receipts. Viewer close releases subscriptions and
indexes only; background owner activity continues and later results restore.

## 7. Risk and validation decision

This slice introduces no dependency, product subprocess, port, Docker context,
MCP server, plugin runtime, dynamic import, root-source path, canonical-owner
transfer, incompatible persistence migration, or global permission/scheduler/
recovery/compact default change.

Focused validation must prove:

- MCP auth expiry/refresh, pagination, capability change, toggle, reconnect,
  backoff, elicitation, disabled owner, stale cursor, and secret redaction;
- skill provenance/version/hash/resource/tool/dependency/supply-chain display,
  update, invoke, result/error, permission, hash drift, staged approval,
  disable, and canonical reconciliation;
- subagent hierarchy, scope, heartbeat, budget, checkpoint, crash/reconnect,
  late-result quarantine, permission-bound kill/steer, sealed denial,
  idempotency, owner mismatch, disable, close, and restore;
- `/mcp`, `/skills`, `/agents`, and `/tasks` open the real overlays and reuse
  the existing command/result/permission selectors;
- disabling each feature binding makes the corresponding real behavior fail
  closed without a mock or legacy fallback;
- M2-04B parent and M2-04 numeric-stage direct effective-line recomputation;
- exact-implementation-commit cleanroom, applicable broad regression,
  cumulative source-to-target/state-owner review, build, and dependency scan.

## 8. Effective-code accounting

The slice interval begins at
`2d3d8c16075f1dda298831578cd339478f63afad`. The parent interval begins at
`af6f6dc0c66a846c739b73e8c6006c67de219ba8`; the numeric-stage interval begins
at `53b002bac1e97eab23b7553d344da068dd8dd3c9`.

The slice minimum is `8,000` effective production lines and the parent minimum
is `16,000`. Tests, docs/comments, static JSX/CSS/SVG presentation, type-only
declarations, repeated DTO/schema, generated wire/client code, data, ledger,
source map, fixtures, mocks, runtime assets, forwarding adapters, vendor-like
source pools, and unreachable code receive zero credit.

Original-language TypeScript/TSX production must be non-zero for Claude and
OpenCode implementation roles. The bounded Hermes semantic port is separately
reported and cannot offset the primary same-language obligation.

Every file above 500 raw additions, above 20 percent of slice effective
production, or above 30 percent in any excluded bucket receives a symbol-level
review of source mechanism, target entry chain, permission/state/error
responsibility, mutation/disable test, and raw/effective delta.
