# Source-to-target and state custody

| Mechanism | Target runtime | Role | Canonical authority |
| --- | --- | --- | --- |
| LoopX | `packages/integrations/loopx_runtime/**` plus Zyra bridge | continuation proposal and private goal/todo/claim/history | LoopX private state only |
| ARG | `topology_policy/arg/**` | joint role-node-edge base proposal | none |
| CARD | `topology_policy/condition/**` | environment residual proposal | none |
| AgentPrune | `topology_policy/pruning/**` | spatial/temporal pruning proposal | none |
| MaAS | `zyra_scheduler/operator_policy/**` | operator/skill/worker/tool/model proposal | none |

Canonical owners remain:

- graph/delta/conflict/recovery: `GraphStateCustody`,
  `GraphDeltaBuilder`, and `DynamicTopologyRuntime`;
- long-term facts: `MemoryFabric`;
- physical placement and dispatch: `ResourceScheduler`;
- permission, lease, budgets, events, and artifacts: their existing
  Zyra-owned runtimes;
- LoopX cross-system mapping and idempotent replay: the Zyra LoopX bridge.

No mechanism may mutate a shared graph in place. Proposal snapshots pass
through the symbolic projector before canonical commit.
