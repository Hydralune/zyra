import type { JsonObject } from "../contracts.ts";
import { E03RuntimeError, type E03TaskState } from "../e03/contracts.ts";
import {
  parsePhysicalDispatch,
  type OmpDispatchSnapshot,
  type PhysicalDispatchProjection,
} from "./contracts.ts";
import { OmpAsyncJobProjectionManager } from "./job-manager.ts";
import { OmpAbortSafeSemaphore } from "./semaphore.ts";

export interface OmpDispatchExecutionOptions {
  signal?: AbortSignal;
  usage?: (result: unknown) => JsonObject;
}

/**
 * TypeScript admission/runtime supplement. It validates and enforces the
 * canonical Python lease projection but never renews, releases, or persists
 * that lease. This keeps one physical state owner while retaining OMP's
 * concurrency and AsyncJob execution control in its original language.
 */
export class OmpWorkerDispatchRuntime {
  readonly jobs = new OmpAsyncJobProjectionManager();
  private readonly semaphores = new Map<string, OmpAbortSafeSemaphore>();

  async execute<T>(
    task: E03TaskState,
    operation: () => Promise<T>,
    options: OmpDispatchExecutionOptions = {},
  ): Promise<T> {
    const dispatch = parsePhysicalDispatch(
      task.physicalDispatch,
      task.identity.taskId,
    );
    if (!dispatch) return operation();
    if (process.env.ZYRA_OMP_WORKER_CONTROL_DISABLED === "1")
      throw new E03RuntimeError(
        "omp_worker_control_disabled",
        "physical dispatch requires the OMP TypeScript worker-control runtime",
      );
    this.assertAttemptBinding(task, dispatch);
    const jobId = `omp-job:${dispatch.lease_id}:${dispatch.attempt}`;
    this.jobs.create({
      jobId,
      ownerSessionId: task.identity.parentSessionId,
      dispatch,
    });
    const semaphore = this.semaphore(dispatch);
    let release: (() => void) | null = null;
    try {
      release = await semaphore.acquire(options.signal);
      this.jobs.start(jobId);
      this.jobs.progress(jobId, {
        message: `dispatching through ${dispatch.backend_id}`,
        completedUnits: 0,
        totalUnits: 1,
        usage: {
          worker_id: dispatch.worker_id,
          backend_id: dispatch.backend_id,
          fence_epoch: dispatch.fence_epoch,
        },
      });
      const result = await operation();
      this.jobs.complete(jobId, result, options.usage?.(result) ?? {});
      return result;
    } catch (error) {
      const current = this.jobs.require(jobId);
      if (error instanceof Error && error.name === "AbortError")
        this.jobs.cancel(jobId, error.message);
      else if (current.phase !== "cancelled") this.jobs.fail(jobId, error);
      throw error;
    } finally {
      release?.();
    }
  }

  parkTask(taskId: string, reason: string): void {
    for (const job of this.jobs.list({ taskId, phases: ["running"] }))
      this.jobs.park(job.job_id, reason);
  }

  reviveTask(taskId: string): void {
    for (const job of this.jobs.list({ taskId, phases: ["parked"] }))
      this.jobs.revive(job.job_id);
  }

  cancelTask(taskId: string, reason: string): void {
    for (const job of this.jobs.list({ taskId }))
      if (!["completed", "failed", "cancelled"].includes(job.phase))
        this.jobs.cancel(job.job_id, reason);
  }

  snapshot(): OmpDispatchSnapshot {
    return {
      version: "zyra.omp-worker-dispatch/v1",
      canonical_state_owner: "python.WorkerPoolStore",
      projection_only: true,
      jobs: this.jobs.list(),
      semaphores: [...this.semaphores.entries()]
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, semaphore]) => ({ key, ...semaphore.snapshot() })),
    };
  }

  private semaphore(dispatch: PhysicalDispatchProjection): OmpAbortSafeSemaphore {
    const key = `${dispatch.worker_id}:${dispatch.backend_id}`;
    const prior = this.semaphores.get(key);
    if (prior) {
      prior.resize(dispatch.concurrency_limit);
      return prior;
    }
    const created = new OmpAbortSafeSemaphore(dispatch.concurrency_limit);
    this.semaphores.set(key, created);
    return created;
  }

  private assertAttemptBinding(
    task: E03TaskState,
    dispatch: PhysicalDispatchProjection,
  ): void {
    if (dispatch.attempt !== task.identity.attempt)
      throw new E03RuntimeError(
        "physical_dispatch_attempt_mismatch",
        `worker attempt ${dispatch.attempt} differs from logical attempt ${task.identity.attempt}`,
      );
    if (task.status !== "created" && task.status !== "queued" && task.status !== "running")
      throw new E03RuntimeError(
        "physical_dispatch_task_not_executable",
        `cannot dispatch E03 task in ${task.status}`,
      );
  }
}
