import type { JsonObject } from "../contracts.ts";
import { E03RuntimeError, type E03TaskState } from "../e03/contracts.ts";
import {
  parsePhysicalDispatch,
  type OmpDispatchSnapshot,
  type PhysicalDispatchProjection,
} from "./contracts.ts";
import { OmpAsyncJobProjectionManager } from "./job-manager.ts";
import { OmpAbortSafeSemaphore } from "./semaphore.ts";
import { OmpSessionAdmissionRuntime, type OmpSessionPermit } from "./session-runtime.ts";

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
  readonly sessions = new OmpSessionAdmissionRuntime();
  private readonly semaphores = new Map<string, OmpAbortSafeSemaphore>();
  private readonly dispatches = new Map<string, PhysicalDispatchProjection>();
  private readonly activeTasks = new Set<string>();

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
    if (this.activeTasks.has(task.identity.taskId))
      throw new E03RuntimeError(
        "omp_dispatch_single_flight",
        `task ${task.identity.taskId} already has an active physical execution`,
      );
    this.dispatches.set(task.identity.taskId, dispatch);
    const jobId = `omp-job:${dispatch.lease_id}:${dispatch.attempt}`;
    this.jobs.create({
      jobId,
      ownerSessionId: task.identity.parentSessionId,
      dispatch,
    });
    const semaphore = this.semaphore(dispatch);
    let sessionPermit: OmpSessionPermit | null = null;
    let release: (() => void) | null = null;
    this.activeTasks.add(task.identity.taskId);
    try {
      sessionPermit = await this.sessions.enter(task, dispatch, options.signal);
      release = await semaphore.acquire(sessionPermit.signal);
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
      const succeeded = operationSucceeded(result);
      this.sessions.yieldOnce({
        taskId: task.identity.taskId,
        kind: succeeded ? "result" : "failed",
        summary: succeeded ? "physical dispatch completed" : "physical dispatch returned a failed result",
        payload: result,
        artifactIds: artifactIds(result),
      });
      this.sessions.settle(task.identity.taskId, succeeded ? "completed" : "failed");
      if (succeeded) this.jobs.complete(jobId, result, options.usage?.(result) ?? {});
      else this.jobs.fail(jobId, new Error("physical dispatch returned a failed result"));
      return result;
    } catch (error) {
      const current = this.jobs.require(jobId);
      const cancelled = error instanceof Error && error.name === "AbortError";
      const session = this.sessions.get(task.identity.taskId);
      if (session && !["completed", "failed", "cancelled"].includes(session.phase)) {
        this.sessions.yieldOnce({
          taskId: task.identity.taskId,
          kind: cancelled ? "cancelled" : "failed",
          summary: error instanceof Error ? error.message : String(error || "physical dispatch failed"),
          payload: { error: error instanceof Error ? error.message : String(error) },
        });
        this.sessions.settle(task.identity.taskId, cancelled ? "cancelled" : "failed");
      }
      if (cancelled)
        this.jobs.cancel(jobId, error instanceof Error ? error.message : String(error));
      else if (current.phase !== "cancelled") this.jobs.fail(jobId, error);
      throw error;
    } finally {
      release?.();
      sessionPermit?.release();
      const session = this.sessions.get(task.identity.taskId);
      if (session && ["completed", "failed", "cancelled"].includes(session.phase))
        this.dispatches.delete(task.identity.taskId);
      this.activeTasks.delete(task.identity.taskId);
    }
  }

  parkTask(taskId: string, reason: string): void {
    for (const job of this.jobs.list({ taskId, phases: ["running"] }))
      this.jobs.park(job.job_id, reason);
    if (this.sessions.get(taskId)) this.sessions.park(taskId, reason);
  }

  async reviveTask(taskId: string): Promise<void> {
    const dispatch = this.dispatches.get(taskId);
    if (!dispatch)
      throw new E03RuntimeError("omp_dispatch_projection_missing", `task ${taskId} has no physical dispatch projection`);
    await this.sessions.revive(taskId, dispatch);
    for (const job of this.jobs.list({ taskId, phases: ["parked"] }))
      this.jobs.revive(job.job_id);
  }

  cancelTask(taskId: string, reason: string): void {
    for (const job of this.jobs.list({ taskId }))
      if (!["completed", "failed", "cancelled"].includes(job.phase))
        this.jobs.cancel(job.job_id, reason);
    if (this.sessions.get(taskId)) this.sessions.cancel(taskId, reason);
    this.dispatches.delete(taskId);
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
      session_runtime: this.sessions.snapshot(),
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

function operationSucceeded(value: unknown): boolean {
  if (!value || typeof value !== "object") return true;
  const selected = value as Record<string, unknown>;
  return selected.ok === undefined || selected.ok === true;
}

function artifactIds(value: unknown): string[] {
  if (!value || typeof value !== "object") return [];
  const selected = value as Record<string, unknown>;
  if (!Array.isArray(selected.artifacts)) return [];
  return selected.artifacts
    .map((artifact) => {
      if (!artifact || typeof artifact !== "object") return "";
      const record = artifact as Record<string, unknown>;
      return String(record.artifact_id ?? record.artifactId ?? "");
    })
    .filter(Boolean)
    .sort();
}
