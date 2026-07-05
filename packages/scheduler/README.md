# Scheduler

M5 scheduler, worker-pool, backend-gateway, watchdog, and recovery planning package.

This package internalizes patterns from OpenHands, openclaw, AgentScope, browser-use, Agent Framework, and claude-code-best into Zyra's own runtime boundary:

- `WorkerManifest` and `WorkerPool` describe local code, simulated edge browser, cloud planner/verifier, and memory curator workers.
- `ResourceScheduler` scores workers by tools, capabilities, privacy, latency, cost, model pressure, failure history, requirement changes, compact history, trajectory, and memory records.
- `WorkerBackendGateway` creates dispatch envelopes for the execute path.
- `RuntimeWatchdog` classifies runtime failures.
- `RecoveryPlanner` turns failures into reroute, checkpoint-resume, fallback-model, or local-replan actions.

The scheduler is used by `TopologyRouter`, task graph execution, `/inject`, `/change`, API endpoints, slash command results, and the Web Scheduler panel.
