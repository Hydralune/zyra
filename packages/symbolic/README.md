# Symbolic

Neural-symbolic constraint checks, topology routing, typed task facts, and control-event state transitions live here.

M3 components:

- `ConstraintKeeper`: validates task/node schema, dependencies, worker allow-lists, budget limits, low-entropy message budgets, forbidden terms, state transitions, and terminal criteria. It emits `constraint_check` event payloads for replay and evaluation.
- `TopologyRouter`: ranks heterogeneous workers from task profile, current graph state, runtime hints, worker capabilities, allowed workers, and failure history. It writes replayable `DecisionRecord` objects and `topology_route` events.
- `apply_requirement_change`: handles `/change` by associating the injected requirement with affected `PlanNode` ids, superseding stale nodes, creating a local replan node, and routing that node.
- `apply_failure_injection`: handles `/inject` or node failure events by preserving a `node_failed` event, creating a recovery node, and routing around recent failure history.

The package is intentionally tied into `zyra_orchestration.run_task_graph` and the API control-command path. It should not become a parallel demo-only abstraction.
