# M2-S05-02 Dual-domain Fault / Placement Live Scenarios Pre-implementation Decision

- slice: `M2-S05-02`
- parent: `M2-05`
- frozen baseline commit: `df5d0d4b0f4e05f2f188f58aea1ea379d9e62154`
- decision date: `2026-07-25`
- migration mode: `scenario_and_evidence_integration_only`
- minimum effective production code: `7000`
- OpenClaw: `excluded_forward_only`

## 1. Frozen implementation boundary

This slice adds two formal, new-input, clean-state live scenario implementations
to the scenario/evidence control plane created in `M2-S05-01`:

1. `live.software-delivery`: code discovery, dependency-aware plan, bounded
   patch, Git diff, executable verification, requirement change, recovery,
   re-verification and delivery artifacts.
2. `live.cross-source-research`: live source acquisition, source identity and
   checksum capture, claim extraction, cross-source agreement/contradiction
   analysis, citation verification, network/provider recovery, structured
   report and delivery artifacts.

The formal path is:

```text
ScenarioRunnerService
  -> DualDomainScenarioExecutor
  -> domain adapter and deterministic verifier
  -> existing workspace / scheduler / worker-pool / fault / recovery /
     provider / tier / artifact owners
  -> canonical owner events and receipts
  -> EffectiveStepClassifier / EvidenceCollector
  -> M2 Workbench live-scenario projection
```

The slice owns scenario definitions, scenario-local execution plans, fault and
requirement-change schedules, expected evidence, deterministic domain
verification, causal archive assembly and UI projection. It does not create a
second scheduler, worker pool, memory store, fault state machine, recovery
planner, task graph, permission engine, provider control plane, workspace
manager, artifact store or canonical event log.

The legacy `m2_scenarios` demo and `foundation.short-owner-chain` cannot satisfy
either live scenario and are not fallbacks.

## 2. Source, language and migration decisions

| capability | source role | repository / source path | `source_language` | Zyra target | `target_language` | `migration_mode` | canonical owner | predecision |
|---|---|---|---|---|---|---|---|---|
| dual-domain scenario orchestration, fault schedule and evidence expectations | `zyra_owned_primary` | no upstream production source; `M2-S05-02` product responsibility | Python | `packages/evaluation/zyra_evaluation/scenario_runner/dual_domain.py`, `fault_campaign.py`, `live_models.py` | Python | `scenario_and_evidence_integration_only` | `DualDomainScenarioExecutor` for scenario-local control only | implement |
| software-delivery adapter and deterministic code verifier | `zyra_owned_primary` | no upstream production source; existing Zyra workspace/code-index/terminal/Git surfaces are consumed | Python | `packages/evaluation/zyra_evaluation/scenario_runner/software_delivery.py`, `domain_verification.py` | Python | `scenario_and_evidence_integration_only` | scenario adapter/verifier; workspace, patch, terminal and artifact custody remain existing | implement |
| cross-source research adapter and citation/checksum verifier | `zyra_owned_primary` | no upstream production source; existing browser/provider surfaces are consumed | Python | `packages/evaluation/zyra_evaluation/scenario_runner/research_delivery.py`, `domain_verification.py` | Python | `scenario_and_evidence_integration_only` | scenario adapter/verifier; browser/provider/artifact custody remain existing | implement |
| tier, placement, SLA and provider/model evidence | `existing_owner_integration` | `packages/evaluation/zyra_evaluation/m1_hardening/execution_tiers.py`, `managed_provider.py`, scheduler/worker-pool packages | Python | `packages/evaluation/zyra_evaluation/scenario_runner/placement_evidence.py`, API composition binding | Python | `owner_port_integration` | existing `WorkerPoolFoundationRuntime`, provider control and tier owners | integrate; no migration |
| live causal archive, stability samples and evidence expectations | `zyra_owned_primary` | no upstream production source | Python | `packages/evaluation/zyra_evaluation/scenario_runner/live_archive.py` | Python | `scenario_and_evidence_integration_only` | scenario evidence owner only; referenced canonical bytes remain existing owner data | implement |
| registry/API composition | `existing_owner_integration` | `packages/evaluation/zyra_evaluation/scenario_runner/registry.py`, `api.py`, `runtime.py`; `apps/api/zyra_api/scenario_api.py` | Python | same paths | Python | `owner_port_integration` | existing `ScenarioRunnerService` and API composition root | extend |
| live scenario evidence projection and fault/placement panels | `zyra_owned_primary` | no upstream production source; existing Scenario Workbench is extended | TypeScript / TSX | `apps/web/src/features/scenarios/live-scenarios.ts`, `view/scenario-workbench.tsx` | TypeScript / TSX | `scenario_and_evidence_integration_only` | Python evidence is canonical; browser projection is disposable | implement |
| task, event, scheduler, memory, permission, fault, recovery, workspace, provider and artifact execution | `existing_owner_integration` | existing Zyra M1/M2 packages and API composition root | Python / TypeScript / Rust | unchanged | unchanged | `owner_port_integration` | existing documented owners | consume only |
| LangGraph checkpoint/exact-resume semantics | `conformance_only` | narrow checkpoint identity/lineage and pending/committed write contracts | Python | no production migration | none | `conformance_only` | existing Zyra graph/recovery owners | conformance only |
| Agent Framework workflow/checkpoint and AG-UI approval/history | `conformance_only` | documented conformance surface | Python / C# | no production migration | none | `conformance_only` | existing Zyra owners | conformance only |
| AgentScope lifecycle/inbox/wakeup | `conformance_only` | documented conformance surface | Python | no production migration | none | `conformance_only` | existing Zyra owners | conformance only |
| browser-use close/restore/watchdog | `reference_only` | documented reference surface | Python / TypeScript | no production migration | none | `reference_only` | existing Zyra browser/watchdog owners | reference only |
| OpenClaw | `excluded_forward_only` | none | none | none | none | `excluded_forward_only` | none | do not read, restore, migrate or test |

No upstream primary or supplementary runtime is selected, so there is no
upstream-language quota. Python is the frozen scenario/backend language and
TypeScript/TSX is the frozen console projection language. A post-hoc
cross-language migration or source-role change is forbidden.

## 3. State and authority custody

The new scenario implementation may persist only:

- the domain plan and its input/checksum identities;
- the current scenario-local phase and completed action identities;
- scheduled and observed fault/change identities;
- expected versus observed route/placement/provider/tier evidence;
- deterministic verifier findings and uncertainty records;
- raw-sample references and causal-archive manifests.

Existing owners retain:

- task/checkpoint and topology state;
- runtime event order, revisions, causation and idempotency;
- scheduler routes, worker leases and settlement;
- memory curation/retrieval state;
- permission decisions and tool execution authority;
- fault signal, recovery plan, checkpoint and continuation state;
- workspace bytes, patch application and terminal processes;
- provider credentials, provider/model dispatch and wire traces;
- artifact bytes and checksums.

Constraints are verified before effects. A scenario action cannot claim a route,
tool result, recovery, provider turn or artifact until the corresponding owner
receipt is present and identity-bound. Missing owner bindings fail closed.

## 4. Formal live gates

Both domains must prove:

- clean preflight and a previously unseen input digest;
- sealed autonomy with `human_intervention_count == 0`;
- actual input-dependent work and output, never a fixture or replay;
- at least 1,000 admitted effective canonical transitions per domain and at
  least 2,000 within a formal combined run;
- running-time requirement change and representative faults;
- checkpoint/restore, route or placement migration, and re-verification;
- local, isolated edge and cloud evidence with real endpoint/process/model
  identity when the profile claims those tiers;
- at least two real provider/model capabilities, credential-custody evidence,
  provider failure/failover and disconnected degradation;
- privacy, latency and cost policy evaluation before dispatch;
- deterministic code/citation/schema/checksum/artifact verification;
- raw samples, configuration, commit, environment identity and complete causal
  archive.

The software scenario must produce a real patch and run real verification
commands in a scenario-owned scratch copy. The research scenario must acquire
live sources at execution time and verify citations against the acquired bytes.
Pre-recorded pages, generated citations and fixed success receipts are invalid.

## 5. Fault and change matrix

The admitted matrix covers:

- mid-run requirement change;
- tool/process exception;
- worker/node loss;
- tool timeout;
- provider rate limit or provider failure;
- edge/network loss;
- privacy-denied cloud placement;
- checkpoint corruption or restore mismatch as a fail-closed negative path.

Each injected item must be linked to an owner-observed fault signal, a recovery
or deterministic rejection, a checkpoint/restore decision where applicable, a
route/placement effect, and a later verifier result. Merely appending a fault
label is not an observed recovery.

## 6. Disable and fallback boundaries

- scheduler disabled: formal placement fails; no direct-execution fallback;
- memory disabled: cross-stage context continuity and final verification fail;
- recovery disabled: injected fault remains unresolved and the run fails;
- low-entropy targeted communication disabled: targeted evidence routing is
  absent and formal verification fails;
- domain verifier disabled: final completion fails;
- live source acquisition disabled: research completion fails;
- real tier/provider evidence absent while claimed: formal completion fails;
- browser close: backend work continues and no cancel is emitted;
- credentials missing/disconnected: record deterministic degradation; do not
  relabel local work as cloud or a model stub as a provider.

## 7. Planned implementation paths

Production:

- `packages/evaluation/zyra_evaluation/scenario_runner/live_models.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/fault_campaign.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/software_delivery.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/research_delivery.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/domain_verification.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/placement_evidence.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/live_archive.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/dual_domain.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/registry.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/api.py`
- `packages/evaluation/zyra_evaluation/scenario_runner/__init__.py`
- `apps/api/zyra_api/scenario_api.py`
- `apps/web/src/features/scenarios/live-scenarios.ts`
- `apps/web/src/features/scenarios/view/scenario-workbench.tsx`
- `apps/web/src/features/scenarios/index.ts`
- `scripts/run_first_stage_scenarios.py`

Direct tests:

- `tests/scenarios/test_dual_domain_live_scenarios.py`
- existing scenario, API and Web suites affected by registry and projection
  changes.

Composition-only edits are accounted as adapter-only. Test, documentation,
schema-only and generated lines are excluded from effective production.

## 8. Commit and evidence boundary

- baseline commit: `df5d0d4b0f4e05f2f188f58aea1ea379d9e62154`
- pre-implementation decision commit: committed before production edits
- implementation commit: the final production/direct-test commit before review
  evidence
- evidence commit: self-review, exact line audit, verification receipts and
  execution-state update after implementation is immutable

Effective-code accounting uses the exact
`baseline_commit..implementation_commit` interval and performs per-file bucket
classification plus individual review for every large or high-contribution
file.
