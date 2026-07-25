# M2-S05-03 Ablation, Metrics, Evidence Bundle and Exit Pre-implementation Decision

- slice: `M2-S05-03`
- parent: `M2-05`
- milestone: `M2`
- frozen baseline commit: `b3387c626fc587936c24777ce108eac6f962283e`
- decision date: `2026-07-26`
- migration mode: `scenario_and_evidence_integration_only`
- minimum effective production code: `6500`
- parent minimum effective production code: `20000`
- M2 minimum effective production code: `135000`
- OpenClaw: `excluded_forward_only`

## 1. Frozen implementation boundary

This slice adds the experiment, statistics, immutable evidence-bundle and
reviewer-navigation state domain required to close `M2-05` and the M2
milestone. It composes the live scenario/evidence owner delivered by
`M2-S05-01` and `M2-S05-02`; it does not create a second task, scheduler,
memory, compact, recovery, communication, provider, topology, artifact, event
or scenario execution owner.

The production path is:

```text
Scenario experiment API / Workbench
  -> ExperimentMatrixRuntime
  -> ScenarioRunnerService and canonical live owner receipts
  -> AblationContractVerifier
  -> MetricAggregationRuntime
  -> EvidenceBundleBuilder / EvidenceBundleVerifier
  -> durable experiment store
  -> report, raw-sample, bundle and reviewer-navigation API
  -> M2 Scenario Workbench experiment/report/evidence panels
```

All baselines and ablations use an immutable comparison envelope containing
the task input digest, scenario definition, provider policy, environment,
hardware profile, budget, verifier revision, failure schedule and seed plan.
The dynamic-swarm variant and each ablation must produce owner-bound scenario
receipts. Recorded evidence from `M2-S05-02` and the frozen M1 exit may be
imported only through digest-verified evidence references; a fixture, replay
flag, sample payload or manually authored metric is never an executable run.

The user boundary from `M2-S05-02` remains in force: this slice must not
discover, start or use an installed/authenticated Claude CLI, Codex CLI or
other provider/model CLI, must not consume user credentials, and must not
initiate an external model request. The M2 exit bundle may reference and
verify already frozen M1 real local/edge/cloud and multi-provider/model
receipts, but it must not relabel M2 local deterministic execution as new
provider or cloud evidence.

## 2. Source, language and migration decisions

| capability | source role | repository / source path | `source_language` | Zyra target | `target_language` | `migration_mode` | canonical owner | predecision |
|---|---|---|---|---|---|---|---|---|
| experiment matrix, comparison envelope, repetition plan and run lifecycle | `zyra_owned_primary` | no upstream production runtime; `M2-S05-03` product responsibility | Python | `packages/evaluation/zyra_evaluation/experiment_runtime/**` | Python | `scenario_and_evidence_integration_only` | `ExperimentMatrixRuntime` owns experiment-local state; `ScenarioRunnerService` retains scenario state | implement |
| baseline and ablation execution contracts | `zyra_owned_primary` | no upstream production runtime; existing M1/M2 canonical owner ports are consumed | Python | `packages/evaluation/zyra_evaluation/experiment_runtime/variants.py`, `execution.py`, `verification.py` | Python | `scenario_and_evidence_integration_only` | `AblationExecutionRuntime` owns only variant orchestration and receipts | implement |
| raw samples, P50/P95, dispersion, confidence and anomaly policy | `zyra_owned_primary` | no upstream production runtime | Python | `packages/evaluation/zyra_evaluation/experiment_runtime/statistics.py`, `metrics.py`, `reporting.py` | Python | `scenario_and_evidence_integration_only` | `MetricAggregationRuntime` | implement |
| tamper-evident bundle, manifest, checksum tree and completeness verification | `zyra_owned_primary` | no upstream production runtime; existing Zyra artifact/evidence receipts are consumed | Python | `packages/evaluation/zyra_evaluation/experiment_runtime/bundle.py`, `integrity.py`, `requirements.py` | Python | `scenario_and_evidence_integration_only` | `EvidenceBundleBuilder` and `EvidenceBundleVerifier`; referenced artifact bytes retain existing custody | implement |
| durable experiment/API composition | `existing_owner_integration` | existing Zyra API and evaluation packages | Python | `apps/api/zyra_api/experiment_api.py`, `apps/api/zyra_api/main.py` and experiment package composition | Python | `owner_port_integration` | experiment store plus API composition root | integrate; adapter-only composition excluded from production quota |
| experiment/report/evidence navigation | `zyra_owned_primary` | no upstream production source; existing Scenario Workbench is extended | TypeScript / TSX | `apps/web/src/features/experiments/**`, `apps/web/src/api/experiment-api.ts`, Scenario Workbench integration | TypeScript / TSX | `scenario_and_evidence_integration_only` | Python experiment evidence is canonical; browser store is disposable projection | implement |
| task, topology, scheduler, memory/compact, recovery, communication, placement, provider, artifact and event execution | `existing_owner_integration` | existing Zyra M1/M2 packages and frozen evidence | Python / TypeScript / Rust | unchanged | unchanged | `owner_port_integration` | existing documented owners | consume only |
| LangGraph checkpoint identity, pending/committed writes and exact-resume | `conformance_only` | narrow contracts in the forward role decision | Python | no production migration | none | `conformance_only` | existing Zyra graph/recovery owners | verify evidence references only |
| Agent Framework workflow/checkpoint and AG-UI approval/history | `conformance_only` | documented contract surface | Python / C# | no production migration | none | `conformance_only` | existing Zyra owners | role audit only |
| AgentScope lifecycle/inbox/wakeup | `conformance_only` | documented contract surface | Python | no production migration | none | `conformance_only` | existing Zyra owners | role audit only |
| opencode, Claude Code, Oh My Pi, OpenHands, browser-use and Hermes categories | `reference_only` | M2 source-role matrix and existing M2 implementations | TypeScript / TSX / Python / native | no new production migration | none | `reference_only` | active M2 owners remain unchanged | role-aware exit audit only |
| OpenClaw | `excluded_forward_only` | none | none | none | none | `excluded_forward_only` | none | do not read, restore, migrate or test |

No upstream primary or supplementary runtime is selected for this slice, so
there is no upstream-language quota or copied control flow. Python is the
frozen experiment/evidence backend language and TypeScript/TSX is the frozen
reviewer workbench language. A post-hoc cross-language change or source-role
downgrade is forbidden.

## 3. State and authority custody

The experiment domain may persist:

- the matrix definition, immutable comparison envelope and repetition plan;
- variant and repetition lifecycle, canonical scenario/evidence references and
  failure/anomaly classifications;
- raw metric samples derived from verified owner receipts;
- aggregate statistics and baseline-versus-ablation comparisons;
- requirement/evidence coverage, source-role disposition and non-claim
  declarations;
- evidence bundle manifests, checksum/Merkle bindings, verification receipts
  and reviewer navigation indexes.

Existing owners retain:

- scenario run lifecycle, effective step classification and causal archives;
- task/checkpoint/topology state and canonical state transitions;
- scheduler decisions, worker routes/leases and placement settlement;
- memory/compact/restore state and measurements;
- permission and sealed-policy decisions;
- fault signals, recovery plans and exact-resume state;
- workspace, terminal, browser, diff, artifact and event bytes;
- provider credentials and actual provider/model dispatch receipts.

The experiment runtime validates the comparison envelope before starting a
variant, validates all owner receipts before admitting a sample, and commits
statistics only after all included samples pass identity and checksum checks.
Bundle export is a content-addressed snapshot: a missing, altered, duplicated,
unbound or post-manifest item makes verification fail closed.

## 4. Frozen experiment matrix

Required baselines:

1. `single_agent`
2. `static_full_connect_multi_agent`
3. `dynamic_heterogeneous_swarm`

Required ablations:

1. `no_scheduler`
2. `no_memory_compact`
3. `no_recovery`
4. `no_low_entropy_communication`

Every comparison uses the same task/input, budget, provider policy, hardware
profile, deterministic verifier, environment identity, failure schedule and
seed plan. A capability ablation changes exactly one declared capability.
Required reports include raw samples, sample count, P50, P95, mean, standard
deviation, median absolute deviation, interquartile range, confidence
interval metadata and anomaly reasons.

The comparison covers quality/success, artifact drift, memory/compact/restore,
communication entropy/useful ratio, topology sparsity/churn, token/time/cost,
placement/privacy, recovery MTTR, provider/model mix and human intervention.
Metrics unsupported by a specific evidence source are explicit `unavailable`
observations with a reason; they are never silently zero-filled.

## 5. Disable and failure boundaries

- experiment runtime disabled: matrix creation/start fails; no report fallback;
- ablation verifier disabled: no ablation may be compared or exported;
- metric aggregator disabled: raw receipts remain but no aggregate/report is
  committed;
- evidence verifier disabled: bundle export and M2 exit readiness fail;
- missing baseline or ablation: experiment completion and exit readiness fail;
- inconsistent comparison envelope: affected repetition is rejected;
- missing raw sample or P50/P95/dispersion: report and bundle fail;
- altered bundle member or manifest: verification fails with the exact path;
- reviewer navigation missing a required requirement/evidence edge: exit
  readiness fails;
- browser close: backend experiment work continues; no cancel is sent;
- authenticated provider/model CLI invocation: fail the slice.

## 6. Planned implementation paths

Production:

- `packages/evaluation/zyra_evaluation/experiment_runtime/**`
- `apps/api/zyra_api/experiment_api.py`
- `apps/api/zyra_api/main.py`
- `apps/web/src/api/experiment-api.ts`
- `apps/web/src/features/experiments/**`
- `apps/web/src/features/scenarios/view/scenario-workbench.tsx`
- `apps/web/src/app/workbench-app.tsx`

Direct tests:

- `tests/scenarios/test_m2_s05_03_experiment_matrix.py`
- `tests/integration/test_experiment_evidence_api_main_path.py`
- `apps/web/test/experiment-evidence-workbench.test.ts`
- existing scenario, API, workbench, submission-boundary and ledger suites
  affected by composition.

Audit/evidence scripts and documents are separate zero-production-credit
buckets. Composition-only edits, declarations/types, JSX/CSS/static
presentation, tests, raw samples, screenshots, reports and evidence bundle
bytes are excluded from the effective-production minimum.

## 7. Commit and review boundary

- slice baseline: `b3387c626fc587936c24777ce108eac6f962283e`
- M2-05 parent baseline: `92537deca86376e147feb6c85248b0d3ff2298a6`
- M2 milestone baseline: `f53bf782ce64390e0da01281ad7b311881080db7`
- pre-implementation decision commit: committed before production edits
- implementation commit: the final production/direct-test commit before exit
  review
- review-fix commit(s): only if the combined numeric-stage/M2 exit review finds
  defects
- review/evidence commit: self-review, exact line audits, complete verification
  receipts and final evidence indexes after the implementation target is
  immutable

Effective-code accounting uses exact
`baseline_commit..implementation_commit`,
`M2-05-parent-baseline..implementation_commit` and
`M2-milestone-baseline..implementation_commit` ranges. The M2-05 numeric-stage
aggregate and M2 milestone exit must use the same final target and the same
cleanroom containing every review fix.
