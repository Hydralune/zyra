# P2-S03-04 topology composer and default-path review

- Slice: `P2-S03-04`
- Verdict: `PASS`
- Base commit: `d0afb45ea26f817258d3a56a3b61690f22fc1362`
- Implementation commit: `a07fec0ae539f5da9b258ac6ea7ca70b7237820e`
- Independent risk-fix target: `cf2857c3d9144d818ebed741b8f66af5ec5deafa`
- Composer version: `topology_composer_v1`
- Composer configuration digest: `9a39bcb82f31ec2efbee972cf07aec5bb8001cd29c7555d1647ab304eb465d5b`

## Scope and ownership

The slice closes the P2-03 topology chain as `ARG -> CARD -> AgentPrune -> TopologyConstraintProjector -> PolicyDeltaBuilder -> GraphStateCustody`. ARG remains the role-node-edge base owner, CARD only overlays or forbids ARG edges, and AgentPrune only applies deterministic spatial/temporal keep/drop decisions. The composer owns one immutable combined proposal; it cannot mutate the canonical graph.

`GraphStateCustody` remains the sole graph commit owner. `ResourceScheduler` still owns physical worker selection and placement. The topology hook runs before the existing scheduler route but never selects or leases a physical worker. Memory, recovery, scheduler telemetry, permission decisions, verification and graph commit references are carried into the decision/outcome receipts.

## Default, validation and diagnostic behavior

Normal resolution remains pinned to `phase1_deterministic_baseline`; it does not invoke or commit the Phase 2 composer. `phase2_strongest_v1` is exposed only through an explicit isolated validation manifest while the three topology layers remain `implementation_validated/deterministic_ready`. Default activation remains closed until P2-S06-01 advances every required layer to `activation_ready`.

The diagnostic record runs the three proposal layers read-only, discards the combined mutation before projection, preserves the baseline as the actual outcome and records `diagnostic_canonical_mutation_forbidden`. No diagnostic route, lease or graph side effect is attributed as actual.

## Composition and symbolic precedence

The composer verifies a single policy input digest, graph revision/signature, ARG-to-CARD lineage and CARD-to-AgentPrune lineage. ARG node/edge operations form the base. CARD reweights ARG `ADD_EDGE` operations by overlay rather than emitting an invalid replacement, constrained CARD additions remain separate, and CARD forbidden/drop decisions override a downstream AgentPrune keep. AgentPrune cannot materialize a protected-edge drop because its typed contract rejects it before composition.

All effective operations pass through the existing projector and delta builder. Permission, placement, privacy, readiness, registry, expiry, pending side effects, budget, capacity, fan-out, graph cycles, stale snapshots and conflicts are checked before custody. Mixed layer snapshots are rejected with replan required. Identical stale input is an idempotent replay; changed stale input is rejected and cannot become a second commit.

## Real behavior and causation

The isolated validation scenario creates two role nodes plus spatial and temporal edges and advances the real graph from revision `0` to `1`. The decision receipt names all three layer digests and `affected_commit=true`; the outcome names scheduler telemetry events, `memory-continuity-r1`, the permission result and `GraphStateCustody.validate_graph`.

Phase-specific scenarios produce different committed role and edge sets: execution selects the execution topology while recovery selects a recovery topology. A same-run execution-worker fault plus requirement revision produces replace/remove operations; the symbolic preview detects a dependency cycle, records `graph_constraint_rejected`, keeps revision `1`, and explicitly executes the baseline. This is a fail-closed recovery result, not a claimed successful topology change.

## Independent risk review and fixes

The implementation commit was frozen before review. Three material findings were fixed in `cf2857c3d9144d818ebed741b8f66af5ec5deafa`:

1. Layer disable failures could collapse into the generic `strongest_required_layer_not_ready`. Layer records now preserve `arg_disabled`, `card_disabled` and `agentprune_disabled`, so fallback attribution is specific.
2. Run-level churn and oscillation history was initially process-local. The guard now reconstructs prior signatures from durable `GraphStateStore` snapshots, combines them with current-process history, and fail-closes if durable history is unavailable. A restart test proves oscillation protection survives composer reconstruction.
3. The first validation fixture had scheduler causation but no real memory ref. The receipt test now carries and asserts a stable memory continuity ref, scheduler event refs and verification ref through the committed outcome.

The review also added composite-path coverage for unknown capabilities, mixed snapshots, AgentPrune budget overflow and fan-out overflow.

## Disable and stability evidence

- ARG disable: no ARG proposal; explicit baseline, no graph mutation.
- CARD disable: `card_disabled`; explicit baseline, no graph mutation.
- AgentPrune disable: `agentprune_disabled`; explicit baseline, no graph mutation.
- Whole policy disable: `phase2_topology_policy_disabled`; explicit baseline.
- Same composite replay: no duplicate revision.
- Changed stale composite: rejected, revision unchanged.
- Maximum commits per window: `composer_churn_window_limit`.
- Return to a recent signature: `topology_oscillation_detected`.
- Restarted composer: durable history still rejects the oscillation.

## Implementation accounting

From the base through the risk-fix target:

- production: 2,328 added lines
- tests/support: 1,530 added lines
- configuration/data: 57 added lines
- runtime assets, generated code, source pool, adapter-only and mock/fixture: 0
- production share of implementation additions: 0.59463602

Tests and configuration are not counted as production. The three prior mechanism source-to-target decisions are carried forward; this slice introduces no new external source copy or runtime dependency.

## Verification

- composer/default/fault scenarios plus adjacent projector, symbolic, task-graph, registry and three mechanism regressions: `81 passed`
- mandated focused suites after final evidence update: recorded in `verification-summary.json`
- Python compile check: passed
- internalization ledger: `audit_ok=True`, `blockers=0`
- release-path and OpenClaw scan: passed
- no-policy-training audit: passed
- staged diff check: passed

The first combined invocation encountered an inaccessible user-level pytest temp directory. The identical tests passed with a workspace-local `--basetemp`; this is an environment limitation. Ruff is not installed in the repository virtual environment.

## Handoff

P2-S03-04 and parent P2-03 are complete at `implementation_validated`. Normal routing remains the frozen Phase 1 baseline. P2-S04-01 is the next candidate and is not authorized by this completion.
