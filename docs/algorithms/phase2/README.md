# Phase 2 deterministic mechanism specification

This document freezes the algorithmic meaning of `phase2_strongest_v1`.
External mechanisms produce proposals only. Zyra's symbolic projector,
canonical state owners, permission runtime, scheduler, leases, budgets, and
verifiers retain final authority.

## Frozen composition

1. LoopX advances a durable goal/todo/claim state machine and emits a bounded
   continuation proposal.
2. `MemoryContinuityVerifier` rejects stale or unprovenanced critical facts
   after checkpoint, compact/restore, restart, or requirement revision.
3. ARG deterministically ranks role-node-edge candidates from the immutable
   policy input and produces the joint base topology proposal.
4. CARD applies a bounded environment-conditioned residual to the ARG graph.
5. AgentPrune removes spatial and temporal communication edges while
   preserving protected dependencies and communication budgets.
6. The symbolic projector validates capability, registry, privacy,
   permission, placement, readiness, and graph invariants and emits a delta.
7. `GraphStateCustody` performs the only canonical topology commit.
8. MaAS ranks operator, skill, worker, tool, model, breadth, and depth
   candidates. It does not perform physical placement.
9. `ResourceScheduler` owns placement, permission, lease, physical dispatch,
   and local/edge/cloud receipts.
10. Independent verifiers bind the final artifact and causal evidence.

## Pseudocode

```text
input := immutable_policy_snapshot()
continuation := loopx.propose(input.goal_state)
continuity := memory_continuity.verify(input, continuation)
if not continuity.valid: return explicit_baseline("memory_discontinuity")

base := arg.propose(input, continuity)
residual := card.correct(base, input.environment)
pruned := agentprune.prune(residual, input.communication_budget)
delta := symbolic_projector.project(pruned, input.constraints)
if not delta.allowed: return explicit_baseline(delta.reason)

commit := graph_state_custody.commit(delta, input.graph_revision)
selection := maas.select(input.operator_catalog, commit)
dispatch := resource_scheduler.dispatch(selection, permission, lease, budget)
return verifier.verify(commit, dispatch, final_artifact)
```

## Complexity

Let `R` be roles, `N` nodes, `E` candidate edges, `C` constraints, `O`
operator candidates, and `M` delivered messages.

- LoopX state transition: `O(T log T)` for `T` indexed todos/claims.
- Memory continuity: `O(F + P)` for critical facts and provenance links.
- ARG joint proposal: `O(RN + E log E)`.
- CARD residual correction: `O(E)`.
- AgentPrune: `O(E log E + M)`.
- Symbolic projection and graph validation: `O(N + E + C)`.
- MaAS deterministic selection: `O(O log O)`.
- Scheduler placement: `O(W + Q)` for workers/resources and candidate queues.

The implementation does not train, fine-tune, run policy gradients, mutate
learned weights, or search the full mechanism-combination space.
