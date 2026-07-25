# M2-S05-01 Scenario Runner / Sealed Evidence Foundation Pre-implementation Decision

- slice: `M2-S05-01`
- parent: `M2-05`
- frozen baseline commit: `92537deca86376e147feb6c85248b0d3ff2298a6`
- decision date: `2026-07-25`
- migration mode: `scenario_and_evidence_integration_only`
- minimum effective production code: `6500`
- OpenClaw: `excluded_forward_only`

## 1. Decision

This slice creates one Zyra-owned scenario and evidence control plane. It does
not migrate another upstream runtime and it does not take custody from the M1
task, event, scheduler, memory, permission, recovery, worker, provider,
workspace, or artifact owners.

The formal path is:

```text
Workbench / API / CLI
  -> ScenarioRunnerService
  -> durable scenario definition, sealed configuration and injection schedule
  -> existing M1/M2 canonical owners
  -> canonical events and artifacts
  -> EffectiveStepClassifier
  -> EvidenceCollector
  -> immutable evidence manifest and verification receipt
```

The legacy `zyra_evaluation.m2_scenarios` module remains a review-only demo
harness. It is not admitted as a formal scenario, is not a replay fallback, and
cannot produce a sealed evidence receipt.

## 2. Source, language, and migration decisions

| capability | source role | source language | target language | migration mode | decision |
|---|---|---:|---:|---|---|
| scenario registry, lifecycle, preflight, sealed policy, effective-step admission, evidence manifest | `zyra_owned_primary` | Python | Python | `scenario_and_evidence_integration_only` | Implement in `packages/evaluation/zyra_evaluation/scenario_runner/**`; no upstream production runtime is selected. |
| typed scenario transport, durable UI lifecycle, close/reconnect and evidence projection | `zyra_owned_primary` | TypeScript / TSX | TypeScript / TSX | `scenario_and_evidence_integration_only` | Implement in `apps/web/src/api/scenario-api.ts` and `apps/web/src/features/scenarios/**`; the browser does not own scenario execution. |
| task, graph, event, worker-pool and scheduler execution | `existing_owner_integration` | Python / TypeScript | unchanged | `owner_port_integration` | Reuse the existing API composition root and canonical owner stores through an explicit execution port. |
| memory curator/index evidence | `existing_owner_integration` | Python / TypeScript | unchanged | `owner_port_integration` | Read canonical task events and state; do not create another memory store. |
| permission and sealed execution enforcement | `existing_owner_integration` plus `zyra_owned_policy_envelope` | Python / TypeScript | unchanged | `owner_port_integration` | Existing permission runtime keeps tool custody; the runner owns the immutable formal policy envelope and fails if it requests human approval. |
| recovery and fault injection | `existing_owner_integration` | Python | unchanged | `owner_port_integration` | Use the existing fault/recovery paths and collect their canonical effects. |
| artifact content and checksums | `existing_owner_integration` | Python | unchanged | `owner_port_integration` | Existing artifact store keeps bytes; runner owns evidence references, digests and admission receipts. |
| OpenCode console/session/terminal/review/diff patterns | `role_aware_audit_only` | TypeScript / TSX | none | `audit_only` | Audit split protocol/app/TUI/web/desktop roles and Zyra landing paths; no code migration. |
| `claude-code-best` session/permission/REPL/PromptInput/command-queue/local-JSX/dialog/Ink patterns | `role_aware_audit_only` | TypeScript / TSX | none | `audit_only` | Audit backend and CLI/TUI roles separately; no production owner or migration quota. |
| Oh My Pi AgentLoop/TaskTool/PAL/Mnemopi/provider/RPC/ACP/Hashline/RoboOmp/TUI/native/Snapcompact patterns | `role_aware_audit_only` | TypeScript / Rust | none | `audit_only` | Audit capability categories and landing status; no production owner or migration quota. |
| Agent Framework workflow/checkpoint and AG-UI approval/history | `conformance_only` | Python / C# | none | `conformance_only` | Challenge lifecycle and approval evidence only. |
| AgentScope lifecycle/inbox/wakeup | `conformance_only` | Python | none | `conformance_only` | Challenge restart and worker-lifecycle evidence only. |
| LangGraph narrow checkpoint/exact-resume contract | `conformance_only` | Python | none | `conformance_only` | No StateGraph, Pregel, channel, reducer, Store, ToolNode, SDK or server owner. |
| browser-use close/restore/watchdog behavior | `reference_only` | Python / TypeScript | none | `reference_only` | Challenge browser-close independence and recovery evidence; no runtime migration. |
| OpenClaw | `excluded_forward_only` | none | none | `excluded_forward_only` | Do not read, restore, migrate, test, or add a runtime dependency. |

There is no cross-language port of an upstream implementation in this slice.
Python remains the evaluation/backend owner language and TypeScript/TSX remains
the existing console owner language.

## 3. State custody

The new runner owns only:

- versioned scenario definitions and input contracts;
- sealed configuration, policy digest, seed and execution profile;
- deterministic fault-injection schedule;
- lifecycle state for scenario create/start/cancel/archive;
- clean-state and new-input proofs;
- operator-attempt / human-intervention ledgers for the scenario envelope;
- effective-step admission decisions and causal validation;
- evidence manifests, metric samples and verification receipts;
- source-role/landing audit records.

Existing owners retain:

- `SQLiteStore` task and checkpoint state;
- runtime-event-spine canonical event order and idempotency;
- `WorkerPool` leases, routes and settlements;
- memory curator, retrieval index and procedure state;
- permission rules, requests, grants, denials and tool execution;
- recovery signals, plans, checkpoints, routes and continuations;
- workspace state and artifact bytes;
- provider and backend dispatch state;
- `CanonicalProjectionStore` browser projections.

The UI may cache view preferences and the last selected scenario. It must not
store an executable scenario, synthesize completion, or cancel backend work
when the tab or component closes.

## 4. Planned production paths

### Python owner

- `packages/evaluation/zyra_evaluation/scenario_runner/canonical.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/errors.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/models.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/registry.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/preflight.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/sealed_policy.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/effective_steps.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/metrics.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/evidence.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/source_audit.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/store.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/runtime.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/api.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/__init__.py`
- `apps/api/zyra_api/scenario_api.py`
- `apps/api/zyra_api/main.py`
- `scripts/run_first_stage_scenarios.py`

### TypeScript / TSX console

- `apps/web/src/api/scenario-api.ts`
- `apps/web/src/api/index.ts`
- `apps/web/src/features/scenarios/admission.ts`
- `apps/web/src/features/scenarios/evidence.ts`
- `apps/web/src/features/scenarios/projection.ts`
- `apps/web/src/features/scenarios/runtime.ts`
- `apps/web/src/features/scenarios/source-audit.ts`
- `apps/web/src/features/scenarios/index.ts`
- `apps/web/src/features/scenarios/view/scenario-workbench.tsx`
- `apps/web/src/app/runtime.ts`
- `apps/web/src/app/workbench-app.tsx`

Composition edits in `main.py`, `app/runtime.ts`, and `workbench-app.tsx` are
adapter-only for effective-line accounting.

## 5. Formal admission invariants

A formal start fails closed unless all of these are true:

1. the definition/version digest exists in the registry;
2. the input is newly supplied and its digest is not a replay digest;
3. database, cache, index, artifact and build roots pass their declared
   clean-state policy;
4. the sealed policy digest matches the canonical normalized policy;
5. `human_intervention_count` is zero;
6. every `ask` action is converted into deterministic deny plus recovery/replan;
7. policy, effective-step classifier and evidence collector are enabled;
8. the execution port is bound to real canonical owners;
9. all admitted effective steps have identity, causation and a semantic effect;
10. artifact evidence has a readable owner path and matching checksum;
11. the final manifest digest and verification receipt can be recomputed.

Heartbeat, log, token, UI repaint, polling, cursor movement, replay, duplicate,
no-op and transport-only activity never count as effective steps.

## 6. Failure and disable boundaries

- `ZYRA_SCENARIO_RUNNER_DISABLED=1`: create/start fails.
- `ZYRA_SCENARIO_SEALED_POLICY_DISABLED=1`: formal start fails.
- `ZYRA_SCENARIO_EFFECTIVE_STEP_DISABLED=1`: evidence admission fails.
- `ZYRA_SCENARIO_EVIDENCE_COLLECTOR_DISABLED=1`: evidence collection and
  verification fail.
- policy digest mismatch: start fails before canonical execution.
- dirty declared root: start fails with a preflight receipt.
- manual/operator mutation attempt: append a counted attempt; keep
  `human_intervention_count=0`; fail the formal run.
- missing causation or invalid effective step: exclude the step and fail formal
  verification.
- process restart: durable queued/running records are reconciled; browser close
  never issues cancel.
- runner disable has no legacy replay or demo fallback.

## 7. Planned behavior evidence

- registry/config/profile/seed/fault normalization and digest stability;
- clean-root admission, dirty-root rejection and new-input/replay separation;
- sealed allow/deny/ask-to-replan behavior and intervention accounting;
- effective-step inclusion/exclusion, deduplication and causal-chain checks;
- grouped metric samples by run/stage/worker/profile/provider;
- canonical span and artifact checksum verification;
- durable create/start/status/cancel/archive and restart reconciliation;
- API and CLI main-path parity;
- Workbench lifecycle, reconnect, browser-close independence and disable;
- real short scenario through API, scheduler/worker pool, memory curator,
  permission envelope, recovery/fault path, canonical events and evidence;
- role-aware M2 source audit with inactive role distinguished from missing
  capability;
- exact implementation-interval line audit, dependency/path audit and
  large-file critical review.

This slice does not claim the two long live scenarios, the `>=2000` effective
transition run, the sparse-topology ablation, real edge/cloud dispatch, or M2
exit. Those remain assigned to `M2-S05-02` and `M2-S05-03`.
