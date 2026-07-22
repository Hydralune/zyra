import assert from "node:assert/strict";
import test from "node:test";

import {
  OmpAbortSafeSemaphore,
  OmpAsyncJobProjectionManager,
  OmpSessionAdmissionRuntime,
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
    integration_binding_id: `binding-${taskId}`,
    graph_ref: { owner: "GraphStateCustody", object_id: `graph-${taskId}`, revision: 1 },
    workspace_ref: { owner: "M1-S05A.WorkspaceManager", object_id: `workspace-${taskId}` },
    gateway_ref: { owner: "M1-S05B.SandboxGatewayRuntime", object_id: "local-sandbox-gateway" },
    route_ref: { owner: "M1-S05D.BackendRegistry", object_id: "local-sandbox-gateway" },
    ...selectedOverrides,
  };
  return {
    ...unsigned,
    projection_digest: requestedDigest ?? digest(unsigned),
  } as PhysicalDispatchProjection;
}

function physicalTask(
  taskId: string,
  dispatch = projection(taskId),
  executionMode: "foreground" | "background" = "foreground",
  parentSessionId = "parent-session",
) {
  const identity = new TaskIdentityRuntime().allocate({
    runId: "run-omp",
    sessionId: `session-${taskId}`,
    taskId,
    parentTaskId: "parent-task",
    parentSessionId,
    idempotencyKey: `identity-${taskId}`,
  });
  const capabilityScope = scope();
  return new TaskStateMachine().create({
    identity,
    definition: definition("omp-worker"),
    scope: capabilityScope,
    context: context(taskId, identity.sessionId, capabilityScope),
    prompt: "execute under canonical physical admission",
    executionMode,
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

test("OMP dispatch rejects duplicate concurrent execution for one physical attempt", async () => {
  const runtime = new OmpWorkerDispatchRuntime();
  const dispatch = projection("dispatch-single-flight", { concurrency_limit: 2 });
  const task = physicalTask(
    "dispatch-single-flight",
    dispatch,
    "background",
    "dispatch-single-flight-session",
  );
  let executions = 0;
  let finish!: () => void;
  const blocker = new Promise<void>((resolve) => { finish = resolve; });
  const first = runtime.execute(task, async () => {
    executions += 1;
    await blocker;
    return { ok: true };
  });
  await Promise.resolve();
  await assert.rejects(
    runtime.execute(task, async () => {
      executions += 1;
      return { ok: true };
    }),
    /already has an active physical execution/,
  );
  assert.equal(executions, 1);
  finish();
  await first;
});

test("OMP session admission shares capacity across foreground/background and park/revive", async () => {
  const runtime = new OmpSessionAdmissionRuntime();
  const firstDispatch = projection("session-first", { concurrency_limit: 1 });
  const secondDispatch = projection("session-second", { concurrency_limit: 4 });
  const firstTask = physicalTask("session-first", firstDispatch, "foreground", "shared-session");
  const secondTask = physicalTask("session-second", secondDispatch, "background", "shared-session");

  const firstPermit = await runtime.enter(firstTask, firstDispatch);
  let secondAdmitted = false;
  const secondPending = runtime.enter(secondTask, secondDispatch).then((permit) => {
    secondAdmitted = true;
    return permit;
  });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(secondAdmitted, false);
  assert.equal(runtime.snapshot().sessions[0]?.active, 1);
  assert.equal(runtime.snapshot().sessions[0]?.pending, 1);

  const parked = runtime.park("session-first", "wait for dependency");
  assert.equal(parked.permit_held, false);
  const secondPermit = await secondPending;
  assert.equal(secondAdmitted, true);
  assert.equal(runtime.get("session-second")?.phase, "background");

  let revived = false;
  const revivePending = runtime.revive("session-first", firstDispatch).then((value) => {
    revived = true;
    return value;
  });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(revived, false);
  runtime.yieldOnce({
    taskId: "session-second",
    kind: "result",
    summary: "background child completed",
    payload: { ok: true },
  });
  runtime.settle("session-second", "completed");
  secondPermit.release();
  const revivedProjection = await revivePending;
  assert.equal(revivedProjection.phase, "foreground");
  assert.equal(revivedProjection.permit_held, true);

  const firstYield = runtime.yieldOnce({
    taskId: "session-first",
    kind: "artifact",
    summary: "foreground child returned one artifact",
    payload: { nested: { values: [1, 2] } },
    artifactIds: ["artifact-b", "artifact-a", "artifact-a"],
  });
  assert.deepEqual(firstYield.artifact_ids, ["artifact-a", "artifact-b"]);
  assert.equal(
    runtime.yieldOnce({
      taskId: "session-first",
      kind: "artifact",
      summary: "foreground child returned one artifact",
      payload: { nested: { values: [1, 2] } },
      artifactIds: ["artifact-a", "artifact-b"],
    }).yield_id,
    firstYield.yield_id,
  );
  assert.throws(
    () => runtime.yieldOnce({
      taskId: "session-first",
      kind: "result",
      summary: "a second result",
      payload: { changed: true },
    }),
    /second typed yield/,
  );
  runtime.settle("session-first", "completed");
  firstPermit.release();
  assert.equal(runtime.snapshot().sessions[0]?.active, 0);
});

test("OMP session abort removes queued admission and cancellation yields exactly once", async () => {
  const runtime = new OmpSessionAdmissionRuntime();
  const firstDispatch = projection("abort-first", { concurrency_limit: 1 });
  const queuedDispatch = projection("abort-queued", { concurrency_limit: 1 });
  const first = await runtime.enter(
    physicalTask("abort-first", firstDispatch, "background", "abort-session"),
    firstDispatch,
  );
  const controller = new AbortController();
  const queued = runtime.enter(
    physicalTask("abort-queued", queuedDispatch, "background", "abort-session"),
    queuedDispatch,
    controller.signal,
  );
  controller.abort("operator cancelled queued admission");
  await assert.rejects(queued, /operator cancelled queued admission/);
  assert.equal(runtime.get("abort-queued"), null);
  assert.equal(runtime.snapshot().sessions[0]?.pending, 0);

  const cancelled = runtime.cancel("abort-first", "operator cancelled active task");
  assert.equal(cancelled.phase, "cancelled");
  assert.equal(cancelled.typed_yield?.kind, "cancelled");
  assert.equal(cancelled.typed_yield?.sequence, 1);
  first.release();
  assert.equal(runtime.snapshot().sessions[0]?.active, 0);
});

test("OMP session duplicate admission and revive are single-flight per physical attempt", async () => {
  const runtime = new OmpSessionAdmissionRuntime();
  const dispatch = projection("single-flight", { concurrency_limit: 2 });
  const task = physicalTask("single-flight", dispatch, "background", "single-flight-session");

  const [first, duplicate] = await Promise.all([
    runtime.enter(task, dispatch),
    runtime.enter(task, dispatch),
  ]);
  assert.equal(runtime.snapshot().sessions[0]?.active, 1);
  first.release();
  duplicate.release();
  assert.equal(runtime.snapshot().sessions[0]?.active, 0);

  const reacquired = await runtime.enter(task, dispatch);
  runtime.park("single-flight", "wait for foreground");
  const [revived, duplicateRevive] = await Promise.all([
    runtime.revive("single-flight", dispatch),
    runtime.revive("single-flight", dispatch),
  ]);
  assert.equal(revived.phase, "background");
  assert.equal(duplicateRevive.phase, "background");
  assert.equal(runtime.snapshot().sessions[0]?.active, 1);
  reacquired.release();
  assert.equal(runtime.snapshot().sessions[0]?.active, 1);
  runtime.cancel("single-flight", "single-flight test complete");
  assert.equal(runtime.snapshot().sessions[0]?.active, 0);
});

test("OMP session restart rehydrates only verified Python projections and disable mutation fails closed", async () => {
  const runtime = new OmpSessionAdmissionRuntime();
  const active = projection("rehydrate-active", { concurrency_limit: 2 });
  const draining = projection("rehydrate-draining", {
    concurrency_limit: 2,
    lease_state: "draining",
  });
  const permit = await runtime.enter(
    physicalTask("rehydrate-active", active, "background", "restart-session"),
    active,
  );
  const before = runtime.snapshot();
  const restored = runtime.rehydrate(
    [draining, active],
    {
      "rehydrate-active": "restart-session",
      "rehydrate-draining": "restart-session",
    },
    { "rehydrate-active": "background" },
  );
  permit.release();
  assert.equal(runtime.snapshot().process_epoch, before.process_epoch + 1);
  assert.deepEqual(restored.map((item) => item.task_id), ["rehydrate-active", "rehydrate-draining"]);
  assert.equal(runtime.get("rehydrate-active")?.phase, "queued");
  assert.equal(runtime.get("rehydrate-draining")?.phase, "parked");
  assert.equal(runtime.snapshot().sessions[0]?.active, 0);

  const tampered = { ...active, worker_id: "tampered-worker" } as PhysicalDispatchProjection;
  assert.throws(
    () => runtime.rehydrate([tampered], { "rehydrate-active": "restart-session" }),
    /changed after canonical lease admission/,
  );
  assert.throws(
    () => runtime.rehydrate(
      [active, active],
      { "rehydrate-active": "restart-session" },
    ),
    /multiple canonical projections/,
  );

  const previous = process.env.ZYRA_OMP_SESSION_RUNTIME_DISABLED;
  process.env.ZYRA_OMP_SESSION_RUNTIME_DISABLED = "1";
  try {
    await assert.rejects(
      runtime.enter(
        physicalTask("disabled-session", projection("disabled-session")),
        projection("disabled-session"),
      ),
      /require the OMP session runtime/,
    );
  } finally {
    if (previous === undefined)
      delete process.env.ZYRA_OMP_SESSION_RUNTIME_DISABLED;
    else process.env.ZYRA_OMP_SESSION_RUNTIME_DISABLED = previous;
  }
});
