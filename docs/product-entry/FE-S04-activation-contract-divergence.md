# FE-S04 activation contract divergence

## Verdict

FE-S04 production implementation is stopped before listener code is added. The
activation baseline is `93dbaf9661481f255fd3eab1d322619d4f8fad97`.

The FE-S04 slice requires implementation to stop when the active HEAD no longer
supports the frozen FE-G00 lifecycle and dispatch claims without a server-side
contract change. Two such differences are present. Implementing only a
capability-prefixed HTTP listener would produce a reachable demo endpoint, but
would not satisfy the product main path or the normal-exit lifecycle contract.

## D-01: revision-fenced disable is not expressible

The frozen terminal contract requires normal shutdown to perform:

```text
drain -> bounded settle/cancel -> revision-fenced enabled=false -> close
```

The only backend definition mutation route is `POST /backends`. Its terminal
authority calls `_validate_terminal_registration` for every request and rejects
`definition.enabled == false` with `terminal_registration_disabled` before the
registry write. There is no terminal DELETE, PATCH, unregister, or disable
route.

Evidence:

- `apps/api/zyra_api/provider_backend_api.py:257-279` is the only definition
  mutation path.
- `apps/api/zyra_api/provider_backend_api.py:431-435` rejects a disabled
  terminal definition.
- `docs/product-entry/terminal-node-contract.md:265` requires the
  revision-fenced disable.

Health failure after closing the listener is only abnormal-exit isolation. It
does not satisfy the distinct normal-exit requirement and must not be reported
as an active disable.

## D-02: the product worker path is not a remote action contract

The product task graph executes `CodeWorkerRuntime.run(request)` through
`dispatch_worker_callable`. The router converts that call to operation
`worker.run`, but the remote wire payload contains only `runtime_worker` and
`m0_execution_ref`. The callable and the `WorkerRequest` are not serializable
and are not sent to an HTTP backend.

For an HTTP backend, the router therefore materializes a JSON mapping from the
remote response. The task graph immediately treats the returned value as a
`CodeWorkerRun` dataclass and calls `dataclasses.replace(...)`. A terminal
backend selected on this path cannot reproduce the in-process result or execute
the requested file/search/command/artifact actions.

Evidence:

- `packages/scheduler/zyra_scheduler/backend_registry/router.py:85-99` freezes
  `worker.run` to the two-field payload.
- `packages/scheduler/zyra_scheduler/backend_registry/router.py:413` returns a
  remote JSON result when there is no in-process native value.
- `packages/orchestration/zyra_orchestration/task_graph.py:1094-1135` consumes
  the result as `CodeWorkerRun`.
- `WorkerDispatchRouter.dispatch_payload` currently has no production caller;
  it is exercised only by integration tests.

A listener-local set of filesystem and shell handlers would therefore be a
second, test-only tool runtime unless the existing permission-approved physical
tool path delegates to it through BackendRegistry. FE-S04 explicitly forbids
that substitution.

## Required bounded remediation decision

Before FE-S04 can resume, a separately authorized remediation must decide and
implement both contracts without changing canonical owners:

1. Permit an existing terminal owner/generation to revision-fenced update only
   its own definition to `enabled=false`, with no health-ready requirement and
   no last-write-wins retry.
2. Establish the production physical-action delegation point. The recommended
   direction keeps the logical `CodeWorkerRuntime` on the runtime host and
   routes already permission-approved file/search/command/artifact effects via
   `WorkerDispatchRouter.dispatch_payload`; terminal definitions must not be
   selected for the non-serializable `worker.run` callable.
3. Preserve permission, lease, workspace attestation, artifact, cancellation,
   event, recovery, and scheduler ownership, and retain sealed
   `excluded_backend_ids` across retries.
4. Add mutation tests proving that removing either lifecycle fencing or the
   production delegation causes the terminal claim gate to fail.

No `real_terminal_dispatch_claimed` or `real_edge_dispatch_claimed` value was
changed. No listener or alternate protocol was added.
