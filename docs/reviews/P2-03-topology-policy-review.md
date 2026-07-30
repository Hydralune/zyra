# P2-03 dynamic topology parent review

- Parent unit: `P2-03`
- Verdict: `PASS`
- Parent base: `2ab79c0a6cd862b9ddfd650d6bccd1fe610b4d60`
- Parent target before evidence: `cf2857c3d9144d818ebed741b8f66af5ec5deafa`
- Completed slices: `P2-S03-01`, `P2-S03-02`, `P2-S03-03`, `P2-S03-04`

## Cumulative mechanism

P2-03 now has one deterministic topology policy:

`ARG joint role-node-edge base -> CARD conditional residual -> AgentPrune spatial/temporal mask -> symbolic projector -> GraphStateCustody`

There is one graph owner and one commit path. No layer mutates a shared graph object, owns a scheduler placement, creates a second runtime, trains a policy, samples an edge or bypasses permission/custody.

## Cumulative readiness

| Layer | Version | Stage | Status | Default |
| --- | --- | --- | --- | --- |
| ARG | `arg_joint_v1` | `implementation_validated` | `deterministic_ready` | no |
| CARD | `card_directional_residual_v1` | `implementation_validated` | `deterministic_ready` | no |
| AgentPrune | `agentprune_spatiotemporal_v1` | `implementation_validated` | `deterministic_ready` | no |
| Composer | `topology_composer_v1` | validation path | deterministic | no |

The policy registry keeps `phase1_deterministic_baseline` active for normal runs. `phase2_strongest_v1` is reachable only through explicit isolated validation. P2-S06-01 owns any `activation_ready` transition.

## Cumulative proof

The four slices establish real role/node/edge proposal generation, environment-conditioned residuals, typed spatial/temporal communication pruning, a unified symbolic commit, replay/stale/conflict behavior, per-layer disable behavior, durable churn/oscillation protection and requirement/fault/phase scenarios. Scheduler, memory, recovery, permission, verification and commit causation are present in receipts.

The cumulative source-custody decision remains cropped deterministic adaptation. ARG, CARD and AgentPrune source executors, providers, training loops, data, checkpoints, learned parameters and direct graph mutation are excluded. There is no OpenClaw use or workspace-root runtime dependency.

## Exit

P2-03 is closed with the evidence under `docs/reviews/evidence/P2-03` and `docs/reviews/evidence/P2-S03-04`. P2-S04-01 is the next candidate and remains unauthorized.
