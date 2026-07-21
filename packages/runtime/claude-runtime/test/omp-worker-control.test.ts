import assert from "node:assert/strict";
import test from "node:test";

import {
  OmpAbortSafeSemaphore,
  OmpAsyncJobProjectionManager,
  OmpWorkerDispatchRuntime,
  mapWithConcurrencyLimit,
  type PhysicalDispatchProjection,
} from "../src/omp-worker-control/index.ts";
import { TaskIdentityRuntime } from "../src/tasks/identity-runtime.ts";
import { TaskStateMachine } from "../src/tasks/state-machine.ts";
import { digest } from "../src/e03/contracts.ts";
import { context, definition, scope } from "./e03/fixtures.ts";

function projection(
  taskId: string,
  overrides: Partial<PhysicalDispatchProjection> = {},
): PhysicalDispatchProjection {
  const { projection_digest: requestedDigest, ...selectedOverrides } = overrides;
  const unsigned = {
    schema: "zyra.worker-pool-dispatch/v1",
    required: true,
    canonical_owner: "python.WorkerPoolStore",
    projection_owner: "typescript.OmpWorkerDispatchRuntime",
    task_id: taskId,
    attempt_id: `attempt-${taskId}`,
    attempt: 1,
    lease_id: `lease-${taskId}`,
    worker_id: "local-code-worker",
    backend_id: "local-sandbox-gateway",
    fence_epoch: 1,
    manifest_digest: "manifest-digest",
    concurrency_limit: 1,
    lease_state: "active",
    logical_task_not_duplicated: true,
    ...selectedOverrides,
  };
  return {
    ...unsigned,
    projection_digest: requestedDigest ?? digest(unsigned),
  } as PhysicalDispatchProjection;
}

function physicalTask(taskId: string, dispatch = projection(taskId)) {
  const identity = new TaskIdentityRuntime().allocate({
    runId: "run-omp",
    sessionId: `session-${taskId}`,
    taskId,
    parentTaskId: "parent-task",
    parentSessionId: "parent-session",
    idempotencyKey: `identity-${taskId}`,
  });
  const capabilityScope = scope();
  return new TaskStateMachine().create({
    identity,
    definition: definition("omp-worker"),
    scope: capabilityScope,
    context: context(taskId, identity.sessionId, capabilityScope),
    prompt: "execute under canonical physical admission",
    executionMode: "foreground",
    physicalDispatch: dispatch,
  });
}

test("OMP semaphore aborts a queued waiter without leaking the next permit", async () => {
  const semaphore = new OmpAbortSafeSemaphore(1);
  const releaseFirst = await semaphore.acquire();
  const aborted = new AbortController();
  const blocked = semaphore.acquire(aborted.signal);
  aborted.abort("cancel queued child");
  await assert.rejects(blocked, (error: Error) => error.name === "AbortError");
  assert.deepEqual(semaphore.snapshot(), {
    capacity: 1,
    active: 1,
    pending: 0,
    available: 0,
  });
  releaseFirst();
  const releaseNext = await semaphore.acquire();
  releaseNext();
  assert.equal(semaphore.snapshot().active, 0);
});

test("OMP bounded map preserves input order while enforcing concurrency", async () => {
  let active = 0;
  let maximum = 0;
  const result = await mapWithConcurrencyLimit(
    [30, 5, 10, 1],
    2,
    async (delay, index) => {
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise((resolve) => setTimeout(resolve, delay));
      active -= 1;
      return `result-${index}`;
    },
  );
  assert.deepEqual(result, ["result-0", "result-1", "result-2", "result-3"]);
  assert.equal(maximum, 2);
});

test("OMP AsyncJob projection supports queued, park, revive, progress and owner cancellation", async () => {
  const jobs = new OmpAsyncJobProjectionManager();
  const first = jobs.create({
    jobId: "job-first",
    ownerSessionId: "owner-one",
    dispatch: projection("task-first"),
  });
  assert.equal(first.phase, "queued");
  assert.equal(jobs.activeCount(), 0, "queued jobs must not consume active capacity");
  jobs.start(first.job_id);
  jobs.progress(first.job_id, {
    message: "halfway",
    completedUnits: 1,
    totalUnits: 2,
    usage: { tool_calls: 1 },
  });
  jobs.park(first.job_id, "waiting for dependency");
  jobs.revive(first.job_id);
  const waiting = jobs.wait(first.job_id, 1000);
  jobs.complete(first.job_id, { ok: true }, { turns: 1 });
  assert.equal((await waiting).phase, "completed");

  jobs.create({
    jobId: "job-second",
    ownerSessionId: "owner-one",
    dispatch: projection("task-second"),
  });
  assert.equal(jobs.cancelOwner("owner-one", "parent cancelled")[0]?.phase, "cancelled");
  assert.equal(jobs.list({ ownerSessionId: "owner-one" }).length, 2);
});

test("OMP dispatch gate changes real execution and fails closed when disabled", async () => {
  const runtime = new OmpWorkerDispatchRuntime();
  let executions = 0;
  const task = physicalTask("physical-child");
  const result = await runtime.execute(task, async () => {
    executions += 1;
    return { ok: true };
  });
  assert.deepEqual(result, { ok: true });
  assert.equal(executions, 1);
  assert.equal(runtime.snapshot().jobs[0]?.phase, "completed");
  assert.equal(runtime.snapshot().canonical_state_owner, "python.WorkerPoolStore");

  const previous = process.env.ZYRA_OMP_WORKER_CONTROL_DISABLED;
  process.env.ZYRA_OMP_WORKER_CONTROL_DISABLED = "1";
  try {
    await assert.rejects(
      new OmpWorkerDispatchRuntime().execute(
        physicalTask("disabled-child"),
        async () => {
          executions += 1;
          return { ok: true };
        },
      ),
      /requires the OMP TypeScript worker-control runtime/,
    );
  } finally {
    if (previous === undefined)
      delete process.env.ZYRA_OMP_WORKER_CONTROL_DISABLED;
    else process.env.ZYRA_OMP_WORKER_CONTROL_DISABLED = previous;
  }
  assert.equal(executions, 1, "disable mutation must prevent child execution");
});

test("OMP dispatch rejects a physical lease bound to another logical task", async () => {
  const runtime = new OmpWorkerDispatchRuntime();
  const task = physicalTask("expected-child", projection("other-child"));
  await assert.rejects(
    runtime.execute(task, async () => ({ ok: true })),
    /not expected-child/,
  );
  assert.equal(runtime.snapshot().jobs.length, 0);
});

test("OMP dispatch rejects a tampered canonical lease projection", async () => {
  const runtime = new OmpWorkerDispatchRuntime();
  const task = physicalTask(
    "tampered-child",
    projection("tampered-child", {
      concurrency_limit: 12,
      projection_digest: "stale-canonical-digest",
    }),
  );
  await assert.rejects(
    runtime.execute(task, async () => ({ ok: true })),
    /changed after canonical lease admission/,
  );
  assert.equal(runtime.snapshot().jobs.length, 0);
});
