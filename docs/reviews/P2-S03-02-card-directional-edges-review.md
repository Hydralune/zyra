# P2-S03-02 CARD Directional Edge Residual Review

## Verdict

`PASS`.

P2-S03-02 has a deterministic CARD residual path at implementation commit
`a8977c1e2cdee1f44705710e73d7523e3687ada9`. It consumes the typed ARG joint
base proposal and real, versioned `EnvironmentSnapshot` observations, then
produces source-to-target add/drop/reweight evidence without constructing a
second topology or committing graph state.

The mechanism is available only as
`implementation_validated + deterministic_ready -> validation`.
`activation_ready` and default composition remain closed for P2-S03-04.

## Frozen boundary

- slice base: `b4ee7819acaebab4e79ad25c93bc7c0520343b5e`
- implementation commit:
  `a8977c1e2cdee1f44705710e73d7523e3687ada9`
- mechanism version: `card_directional_residual_v1`
- configuration digest:
  `fc0d699fead07217b6b24c557b183c5d6a0542d4dab2d590b20c0b2e3d3c815e`
- input-precheck predecessor report:
  `82ad9a84a55506223fce2133168b7df2f0ae487aa7288fe5ede5c58869c1bbde`
- prior ARG implementation report:
  `124bc3d59f9770f3600d23a1d3b72c4115baa0f1d076da16d237a7ac27fded28`
- CARD implementation-readiness report:
  `6ad61b0f1c5f749ec1dcf5fd30dbec21d50369a707c82e4675ef4f2bddc0e244`

No ARG implementation, GraphStateCustody owner, default topology policy,
LoopX file, OpenClaw path, legacy source pool, provider runtime or external
dependency changed.

## Environment feature and lineage review

`CARDEnvironmentEncoder` validates that the input is an ARG proposal for the
same immutable policy snapshot and graph signature. It extracts role-node,
worker binding and persisted incident-edge structure from the ARG joint
hypothesis. An empty or unbound ARG hypothesis fails closed; constrained
replacement candidates must connect existing ARG role nodes and cite an
upstream constraint.

Worker observations are required. Provider, model and tool observations are
encoded when linked and otherwise recorded as explicit optional-category
missingness. Each observation retains owner source, source event, physical
runtime identity, location, availability, health, success/failure, p50/p95
latency, queue/load/capacity, prompt/completion tokens, cost, privacy,
allowed placement, network state, freshness and confidence. Fault,
requirement-change, compact and recovery flags are preserved as trigger
features when owners supply them.

Missing health/telemetry/physical identity/privacy/location required fields
fail closed. Stale observations receive the configured confidence multiplier;
below-threshold stale data cannot change the effective candidate. Optional
missing fields remain visible in feature lineage and are not silently
fabricated.

## Directional residual review

The target does not migrate CARD's GCN, MLP, learned feature-fusion weights or
symmetric `Z·Zᵀ` edge score. Seven fixed, versioned terms contribute to each
edge:

- target capability fit and source/target capability complement;
- source and target health, weighted by direction;
- source p50 and target p95 latency, weighted by direction;
- target queue/load/capacity;
- target real observed cost;
- source and target privacy/allowed-placement legality;
- observation freshness and confidence.

Normal controlled evidence produced:

| Edge | Type | Residual | Action |
|---|---|---:|---|
| `node-a -> node-b` | spatial | `0.443125` | reweight |
| `node-b -> node-a` | spatial | `0.910375` | reweight |
| `node-b -> node-c` | temporal | `0.853750` | reweight |

Thus reciprocal edges are not treated as a symmetric matrix. Every decision
records the seven raw terms, weighted contributions, feature lineage,
confidence, rejection reasons, hysteresis verdict and expected effect.
Spatial and temporal residual sets remain separate.

## Failure, privacy and stability evidence

When `worker-b` becomes unavailable, all affected incoming, outgoing and
temporal edges change to `drop` with
`target_unavailable`, `target_capacity_or_lease_unavailable` or
`source_unavailable`. A network disconnect produces the corresponding hard
drop. Removing cloud from allowed placements drops the three cloud-touching
edges with `privacy_or_placement_forbidden`; no forbidden cloud edge remains
in the effective validation candidate.

Stale `worker-b` telemetry lowers effective confidence to `0.25` and holds
the ARG base edges with an explicit `stale_telemetry:worker-b` reason. A
30-second minimum dwell blocks non-hard switching; two consistent
observations are required before adding a constrained replacement edge.
Stable score noise below `0.025` produces no add/drop oscillation. Hard
availability, privacy and disconnect constraints may drop immediately.

The hysteresis API is pure: callers pass immutable edge state and receive a
new state contract. CARD owns no canonical graph or scheduler state.

## Readiness and owner enforcement

The installed P2-S03-02 report carries ARG
`deterministic_ready`, declares CARD `deterministic_ready`, and leaves
AgentPrune and MaAS `unavailable`. `activation_allowed` is false.

- `implementation_validated + deterministic_ready` -> isolated `validation`;
  correction is eligible for the future composer but canonical mutation is
  false.
- `evidence_only` -> `diagnostic`; the correction diff remains observable,
  while the ARG proposal digest remains the effective commit input.
- `unavailable`, disabled, missing, corrupt or digest mismatch -> explicit
  `unmodified_arg_base`.
- `activation_ready` is not claimed.

The default `CARDTopologyRuntime.from_repository` path loads the report as
validation. Runtime and integration evidence show graph revision `0 -> 0`;
the condition package contains no `GraphDeltaBuilder` or custody commit call.

## Source custody and no-training review

CARD is a `supplementary_implementation` source with a cropped
Python-to-Python migration. The retained mechanisms are condition-feature
combination, role-edge prior consumption, correction ordering and separate
spatial/temporal edge control. They are restricted to an ARG-base residual.

GCN/MLP weights, torch/torch-geometric, symmetric score, random graph
combination, random edge sampling, trainable logits, training data,
checkpoint, graph executor, Agent runtime, provider, tool registry, decision
node and memory loop are rejected. The no-policy-training audit contains zero
entry points, datasets, checkpoints, samples, learned parameters or random
sampling controls. Source-language custody reports 2,586 added Python
production lines and zero violations.

## Line accounting

- production: 2,586
- tests: 1,328
- data/config: 61
- adapter-only: 0
- runtime assets: 0
- source pool: 0
- generated content counted as implementation: 0
- mock/fixture counted as implementation: 0

The 3,975-line implementation total exactly matches Git numstat from the
slice base to implementation commit. Tests, configuration, evidence and
source material are not counted as production.

## Verification

- focused CARD tests: `8 passed`
- focused plus ARG/policy-contract/neuro-symbolic adjacent tests:
  `49 passed`
- Python compile: passed
- readiness contract validation: passed
- default CARD readiness resolution: passed, `validation`
- source-language custody: passed, zero violations
- no-policy-training audit: passed, zero findings
- `git diff --check`: passed
- Ruff: not run because the repository virtual environment has no Ruff module

One broader inherited readiness-index command reports `33 passed, 1 failed,
6 errors` because the frozen Phase 2 evidence index expects an old
`package.json` digest. `package.json` is identical at the P2-S03-02 base and
implementation commits, and this slice changes neither it nor the baseline
manifest. This is recorded as a non-slice, nonblocking inherited check rather
than rewritten inside CARD.

## Handoff

P2-S03-03 and P2-S03-04 receive:

- `TopologyProposalArtifact` for the unchanged ARG base;
- `CARDResidualCorrection` with separate spatial/temporal decisions;
- `TopologyProposalArtifact` carrying residual operations and full feature
  lineage;
- immutable hysteresis next-state contracts;
- `card_directional_residual_v1`, configuration and readiness digests;
- explicit stale/missing/diagnostic/unavailable semantics.

AgentPrune may consume the effective residual edge sets, but the final
composer must still revalidate endpoints, DAG, capability, permission,
privacy, budget, capacity and readiness before `GraphStateCustody` can
commit. Default activation is deliberately not implemented or claimed here.
