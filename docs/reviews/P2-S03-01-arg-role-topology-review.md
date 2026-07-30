# P2-S03-01 ARG Role-Node-Edge Joint Topology Review

## Verdict

`PASS`.

P2-S03-01 has a real deterministic ARG proposal path at implementation commit
`2f84c9fcc62f85e9837fbeb58b82c6bdd8c22795`. It constructs one hypothesis
containing roles, capability-bound nodes, incident edges and an explicit END.
It is available only as an `implementation_validated` validation proposal:
canonical mutation remains closed until a later `activation_ready` composer.

## Frozen boundary

- slice base: `2ab79c0a6cd862b9ddfd650d6bccd1fe610b4d60`
- implementation commit:
  `2f84c9fcc62f85e9837fbeb58b82c6bdd8c22795`
- mechanism version: `arg_joint_v1`
- configuration digest:
  `6af122c68fbdb7e9bb4be6101bcac805f668c3c18b2476eb219af76a560edf92`
- input-precheck predecessor report:
  `82ad9a84a55506223fce2133168b7df2f0ae487aa7288fe5ede5c58869c1bbde`
- implementation-readiness report:
  `124bc3d59f9770f3600d23a1d3b72c4115baa0f1d076da16d237a7ac27fded28`

No LoopX file, OpenClaw path, vendor/source-pool path, first-stage evidence or
canonical owner implementation changed.

## Implementation review

The versioned role catalog consumes worker capability manifests and
environment observations, then enriches profiles with tool, skill and model
registry projections. A role is not eligible unless it has a concrete worker
binding, capability set, permission set, allowed placement, healthy/fresh
observation, capacity and lease availability. Unknown and capability-free
roles fail closed. Registry versioning is based on immutable manifest
digests, not volatile registration timestamps.

The encoder binds task, phase, requirement revision, unresolved obligations,
the exact canonical graph signature/revision/commit, optional branch-local
delta digest, catalog version/digest, permissions, placements, current
health, continuity memory references, readiness report, mechanism version and
configuration digest. Optional model observations must cite the frozen input
digest; a mismatch takes the explicit baseline path.

The builder performs bounded deterministic beam expansion. Each step jointly
chooses a role, a capability-manifest binding, a node and incident edges.
Predecessors are scored over all eligible canonical and branch-local nodes
using dependency hints, capability complement, critical-path value and
communication/fan-out budgets. It does not use the ARG source's fixed recent
node window. The seven recorded score terms are obligation coverage,
capability fit, dependency reachability, critical path, communication cost,
switch cost and recovery value. Stable sorting, fixed caps and versioned
`ARG_START_V1` / `ARG_END_V1` tokens replace sampling.

The result is a complete `TopologyProposalArtifact` with node/edge operations,
per-role scores, alternative hypothesis references, readable reasons and an
END reason. Requirement changes can terminate obsolete ARG-owned state and
produce different additions/replacements. The runtime publishes evidence and
events, but never calls `GraphStateCustody.commit`; the canonical revision is
unchanged in validation, diagnostic, unavailable and degraded paths.

## Readiness enforcement

The installed P2-S03-01 report declares only ARG
`deterministic_ready`. CARD, AgentPrune and MaAS remain `unavailable`.
`activation_allowed` is false.

- `implementation_validated + deterministic_ready` -> `validation`
- `evidence_only` -> `diagnostic`, proposal allowed, mutation forbidden
- `unavailable`, missing, corrupt, unknown or digest mismatch -> explicit
  `phase1_deterministic_baseline`
- `activation_ready` is not claimed by this slice

The report is accepted by the shared
`validate_readiness_report` contract and is loaded by the default
`ARGTopologyRuntime.from_repository` path.

## Behavior evidence

The default runtime replay produced four different leading roles and proposal
digests from the same catalog:

| Phase | Requirement | Leading role | Proposal digest prefix |
|---|---|---|---|
| planning | requirement-r1 | planning_role | `ec291957f5cee703` |
| execution | requirement-r1 | execution_role | `4cbaa981fc436e27` |
| verification | requirement-r1 | verification_role | `aac583bbe6835c3e` |
| recovery | requirement-r2 | recovery_role | `f36f247e857a8041` |

Every role step has an incident START or dependency edge, every hypothesis has
an explicit END edge/reason, and the complete score breakdown is recorded in
`behavior-verification.json`. Replaying the execution input produced an
identical proposal digest and hypothesis. Disabling ARG removed the joint
proposal, selected `phase1_deterministic_baseline`, and produced a measurable
semantic difference while the graph stayed at revision zero.

## Source custody and no-training review

ARG-Designer is treated as `primary_implementation` for this proposal domain,
with a cropped Python-to-Python migration. The retained ideas are the
stepwise joint role/node/edge state and explicit END. The target corrects
static roles, recent-node predecessor limits and random fallback with
capability registries, all-node/branch-local scoring and fail-closed
validation.

The GRU/MLP decoders, torch dependency, curriculum learning, training
dataset/runner, checkpoint, random BFS, random role fallback, sampling loop
and full experiment runtime were rejected. The standard no-policy-training
audit passed with zero entry points, datasets, checkpoints, samples or mutable
learned parameters. Source-language custody reports 3,034 added Python
production lines and zero violations.

## Line accounting

- production: 3,034
- tests: 1,206
- data/config: 81
- adapter-only: 0
- runtime assets: 0
- source pool: 0
- generated content counted as implementation: 0

The 4,321-line implementation total exactly matches the frozen Git numstat.
Tests, configuration, evidence and source material are not counted as Zyra
production.

## Verification

- unit slice tests: `6 passed`
- integration slice tests: `4 passed`
- adjacent graph custody, projector, policy contract, registry and memory
  continuity bundle: `68 passed`
- Python compileall: passed
- readiness contract validation: passed
- source-language custody: passed, zero violations
- no-policy-training audit: passed, zero findings
- `git diff --check`: passed

The environment's system Python lacks pytest, so all authoritative test
results use the repository `.venv`, as required by the slice.

## Handoff

P2-S03-02 receives `TopologyProposalArtifact`, catalog version/digest,
`arg_joint_v1`, readiness status, score breakdown, alternatives and explicit
degraded semantics. CARD must treat this as the sole ARG base proposal and
must not create a second graph owner. Final canonical mutation remains owned
by the symbolic projector and `GraphStateCustody`.

Nonblocking scope boundary: activation and CARD/AgentPrune/MaAS composition
are deliberately not implemented or claimed in P2-S03-01.
