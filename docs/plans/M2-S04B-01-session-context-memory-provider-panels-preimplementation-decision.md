# M2-S04B-01 Session / Context / Memory / Provider Panels Preimplementation Decision

Status: frozen before production changes

Decision date: 2026-07-25

Slice: `M2-S04B-01`

Parent unit: `M2-04B`
Zyra implementation baseline: `af6f6dc0c66a846c739b73e8c6006c67de219ba8`

## 1. Scope and protected ownership

This decision freezes implementation sources, language continuity, target
modules, and state custody before production changes.

The slice productizes session lineage, context/compact, memory, provider/model,
and heterogeneous placement controls in the existing M2 workbench. It does not
create a browser-owned session, memory, provider, scheduler, or checkpoint
store. The `CanonicalProjectionStore` from M2-01B remains the only frontend
projection truth, and all mutations cross the M2-01A typed client and M2-04A
command transport.

Protected canonical owners remain:

- TypeScript `QueryEngine`, session lifecycle, context builder, compact/restore,
  and `ProviderControlPlane` under `packages/runtime/claude-runtime`;
- Python `SQLiteStore`, `MemoryFabric`, retrieval, curator, skill-memory,
  graph/checkpoint, scheduler, worker-pool, placement, and recovery owners;
- M2-01B immutable projection state and selectors for browser rendering;
- M2-04A command receipts, queue recovery, permission gates, and sealed-mode
  enforcement for operator mutations.

The console may calculate previews, comparisons, admission explanations, and
redacted presentation. It may not commit backend state or invent a successful
receipt.

## 2. Source-role and language decision

| Role | Repository and pinned revision | Exact source files read | Source language | Zyra target | Migration mode | Owner result |
| --- | --- | --- | --- | --- | --- | --- |
| primary | `claude-code-best@c57f5a29e88e9a814bea47abeb9a0a6f725dc102` | `src/commands/context/context-noninteractive.ts`; `src/commands/context/context.tsx`; `src/commands/compact/compact.ts`; `src/commands/memory/memory.tsx`; `src/utils/sessionRestore.ts`; `src/utils/queryContext.ts`; `src/services/compact/compact.ts`; `src/services/compact/postCompactCleanup.ts`; `src/services/compact/reactiveCompact.ts`; `src/services/SessionMemory/sessionMemory.ts`; `src/screens/REPL.tsx` | TypeScript / TSX | `apps/web/src/features/session/**`; `apps/web/src/features/memory/**`; `packages/commands/src/**` | `cropped_migration` plus `retained_control_flow_adapt` | Retain compact-boundary accounting, category budgets, retained/dropped/restored explanation, session lineage/resume guard, and command-to-receipt control flow. Replace process-local state with Zyra projections and canonical backend receipts. |
| supplementary | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | `packages/app/src/context/server-session.ts`; `packages/app/src/components/session/session-context-metrics.ts`; `packages/app/src/components/session/session-context-breakdown.ts`; `packages/app/src/components/session/session-context-usage.tsx`; `packages/app/src/components/session/session-context-tab.tsx`; `packages/app/src/pages/session.tsx`; `packages/core/src/session/context-epoch.ts`; `packages/core/src/session/compaction.ts`; `packages/core/src/session/history.ts`; `packages/core/src/session/revert.ts`; `packages/session-ui/src/components/session-retry.tsx` | TypeScript / TSX | `apps/web/src/features/session/**`; `apps/web/src/features/providers/**`; `apps/web/src/features/placement/**` | `cropped_migration` | Reuse server/session scoping, parent/fork navigation, context epoch aggregation, reconnect invalidation, retry/fallback display, and capability-oriented model presentation. Do not migrate a second session cache or provider owner. |
| supplementary | `hermes-agent@44ddc552f5e054759a6970af8997ea588a9d81c9` | `agent/memory_manager.py`; `agent/curator.py`; `gateway/memory_monitor.py`; `ui-tui/src/lib/memory.ts`; `ui-tui/src/lib/memoryMonitor.ts` | Python and TypeScript | `apps/api/zyra_api/session_console_api.py`; `apps/web/src/features/memory/**` | `same_language_component_integration` for bounded Python projection logic and `cropped_migration` for TypeScript view behavior | Retain typed memory-provider status, deterministic curator phase/report presentation, interrupted-sync diagnostics, and explicit provenance. Existing Zyra `MemoryFabric` and curator remain canonical; no Hermes store or background reviewer is imported. |
| conformance only | LangGraph pinned by `source-graphs/langgraph/source-graph.md` section 12 | checkpoint identity/lineage, pending/committed writes, stable task ID, exact-resume contracts | Python | tests and view audit only | `conformance_only` | Check checkpoint identity, pending-versus-committed separation, conflict/stale flags, and exact resume correlation. No `StateGraph`, channel, reducer, Pregel, stream, Store, ToolNode, SDK, or UI owner enters production. |
| conformance only | Agent Framework / AgentScope as frozen by the parent unit | session/history/approval conformance and worker/KB lifecycle projections | Python / C# source facts | adversarial tests only | `conformance_only` | Validate lineage, history, retrieval, and worker-placement presentation without a second runtime owner. |
| reference only | browser-use / oh-my-pi | watchdog, model/provider, terminal and remote-control source graph facts | Python / TypeScript | disconnect and fallback review only | `reference_only` | Used to test reconnect, failover, and no-secret presentation. No production quota. |
| excluded | OpenClaw | none | none | none | `excluded_forward_only` | No source read, migration, adapter, test quota, dependency, path, or documentation reference is introduced. |

There is one primary implementation source and two supplementary sources. Each
supplementary contribution fills a bounded gap and cannot take canonical state
custody.

## 3. Retained mechanisms and rejected mechanisms

The implementation retains and adapts:

1. Context usage is calculated against the post-boundary model view and is
   grouped into accountable categories rather than one percentage.
2. Compact preview and compact execution are separate. Execution is successful
   only after a durable backend receipt and a later canonical projection.
3. Session resume/rewind binds task, run, session, checkpoint, compact epoch,
   and expected revision; stale or conflicting identities fail closed.
4. Parent/fork/resume lineage is navigable and cross-links checkpoint,
   pending-write, timeline, topology, worker, artifact, and control identities.
5. Memory search is scoped by working, episodic, semantic, and skill layers and
   exposes provenance, veracity, freshness, and curator decisions.
6. Provider/model presentation exposes capabilities, credential presence,
   quota, rate limits, selected route, fallbacks, usage, and cost but never a
   credential value.
7. Placement decisions explain device/edge/cloud resources, privacy,
   sensitivity, SLA, candidate rejection, model split, migration, and
   violations.
8. Reconnect discards transient optimistic presentation and reconciles from the
   typed backend plus canonical projection store.

The implementation rejects:

- localStorage, IndexedDB, React context, or feature-local stores as canonical
  session/memory/provider/placement truth;
- a compact button that only edits presentation state;
- free-form checkpoint rewind without exact identity and backend receipt;
- secret values, bearer tokens, provider API keys, or raw credential payloads;
- provider/model selection that bypasses the provider control plane;
- browser-side placement or privacy policy as the final scheduler decision;
- mock/fixed responses, replay fixtures, or static cards as completion proof;
- LangGraph generic graph/UI ownership or any OpenClaw dependency.

## 4. Target module map

Production work is constrained to:

- `packages/commands/src/registry.ts` and result modeling:
  register the seven existing backend commands with typed arguments, mutation
  classification, sealed eligibility, and receipt presentation.
- `apps/web/src/features/session/**`:
  derive lineage/checkpoint/context/compact views from the canonical selector
  pipeline; coordinate preview, compact, resume, rewind, and export through
  command receipts; reconcile reconnect and stale/conflict outcomes.
- `apps/web/src/features/memory/**`:
  derive layered search, evidence/veracity, retrieval, curator, and
  skill-memory views; execute inspect/curation commands without owning memory.
- `apps/web/src/features/providers/**`:
  normalize redacted provider catalog/route receipts, capability and quota
  decisions, fallback chain, usage, and cost; submit `/model`.
- `apps/web/src/features/placement/**`:
  derive resource, privacy, SLA, route-candidate, model split, migration, and
  violation views from canonical scheduler/worker/recovery/event projections.
- `apps/web/src/app/runtime.ts`, task-detail composition, and existing style:
  bind one session console controller per workbench runtime and render the
  feature workbenches.
- `apps/api/zyra_api/session_console_api.py` only if the current typed command
  response cannot expose an existing owner projection. Any Python addition must
  remain a redacted, testable projection facade and cannot become a store.

Tests may add TypeScript behavior suites and focused Python integration cases;
they do not count as production.

## 5. Control and reconnect contract

Every mutating control:

1. resolves the current task/run/session and canonical revision;
2. validates action-specific identity and admission locally for early feedback;
3. submits through `CommandSurfaceRuntime`;
4. displays queued/running/failed/committed receipt state;
5. binds receipt event/checkpoint/command identities to timeline and topology;
6. waits for canonical projection reconciliation before declaring the new
   context, memory, provider, or placement state current.

On disconnect, active network work is cancelled, transient previews are marked
stale, and controls become unavailable. Reconnect restores the M2-01B snapshot,
rebinds event ingress, refreshes command queue/receipts, and computes conflicts
against the restored canonical revision. No panel cache can win over the
restored projection.

## 6. Risk and validation decision

The slice adds no external dependency, process, port, MCP server, plugin,
dynamic import, Docker context, or root-repository runtime path. It does not
transfer a canonical owner or alter default permission, scheduler, recovery,
compact, or provider policy.

Focused validation must prove:

- compact preview differs from committed compact execution and later restore;
- exact resume/rewind rejects stale or cross-session checkpoint identity;
- memory query/curation changes the later backend context projection;
- provider failure produces a real fallback route receipt;
- privacy/SLA constraints change candidate admission and placement explanation;
- browser disconnect disables controls and reconnect reconciles canonical state;
- disabling the session console controller makes those real controls
  unavailable while unrelated workbench projections remain usable;
- source/dependency scans show no runtime path to sibling repositories.

## 7. Effective-code accounting

The direct slice baseline is
`af6f6dc0c66a846c739b73e8c6006c67de219ba8`; the parent baseline is the M2-04B
baseline frozen by the unit plan.

The minimum is `8,000` effective production lines after excluding tests,
documentation/comments, generated output, schemas/data, fixtures, CSS/static
presentation, type-only declarations, ledger records, forwarding adapters, and
unreachable code. Original-language TypeScript/TSX production credit must be
non-zero; supplementary same-language Python credit is reported separately.

Every production file above 500 raw added lines, every file contributing over
20% of effective code, and every file with more than 30% excluded lines needs
per-file self-review. If real responsibilities cannot meet the threshold, the
slice stops for planning correction rather than adding aliases, duplicated
branches, inert code, or data-as-code.
