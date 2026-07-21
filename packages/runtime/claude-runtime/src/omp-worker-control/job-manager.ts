import { digest, E03RuntimeError, type E03Clock, SystemE03Clock } from "../e03/contracts.ts";
import type { JsonObject } from "../contracts.ts";
import {
  assertProjectionJob,
  sealProjectionJob,
  terminalProjectionPhase,
  type OmpProjectionJob,
  type OmpProjectionJobPhase,
  type OmpProjectionProgress,
  type PhysicalDispatchProjection,
} from "./contracts.ts";

export interface CreateProjectionJobInput {
  jobId: string;
  ownerSessionId: string;
  dispatch: PhysicalDispatchProjection;
}

export interface ProjectionJobFilter {
  ownerSessionId?: string;
  taskId?: string;
  phases?: readonly OmpProjectionJobPhase[];
}

/**
 * Process-local execution projection cropped from OMP AsyncJobManager.
 * Queued work is visible but does not count as active; every mutation is
 * monotonic and owner-scoped. Canonical attempt/lease state remains in the
 * Python WorkerPoolStore and this class intentionally has no persistence port.
 */
export class OmpAsyncJobProjectionManager {
  private readonly jobs = new Map<string, OmpProjectionJob>();
  private readonly listeners = new Set<(job: OmpProjectionJob) => void>();
  private readonly waiters = new Map<string, Set<Waiter>>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  create(input: CreateProjectionJobInput): OmpProjectionJob {
    const prior = this.jobs.get(input.jobId);
    if (prior) {
      assertProjectionJob(prior);
      if (
        prior.task_id !== input.dispatch.task_id ||
        prior.attempt_id !== input.dispatch.attempt_id ||
        prior.lease_id !== input.dispatch.lease_id ||
        prior.owner_session_id !== input.ownerSessionId
      )
        throw new E03RuntimeError(
          "omp_job_idempotency_conflict",
          `OMP job ${input.jobId} was reused with another physical attempt`,
        );
      return structuredClone(prior);
    }
    const now = this.clock.now();
    const progress = progressValue({
      sequence: 0,
      message: "queued for physical worker admission",
      completed_units: 0,
      total_units: 1,
      usage: {},
      observed_at: now,
    });
    const created = sealProjectionJob({
      job_id: required(input.jobId, "job id"),
      owner_session_id: required(input.ownerSessionId, "owner session id"),
      task_id: input.dispatch.task_id,
      attempt_id: input.dispatch.attempt_id,
      lease_id: input.dispatch.lease_id,
      worker_id: input.dispatch.worker_id,
      backend_id: input.dispatch.backend_id,
      phase: "queued",
      revision: 1,
      progress,
      queued_at: now,
      started_at: null,
      parked_at: null,
      settled_at: null,
      error: "",
      result_digest: "",
      projection_only: true,
      canonical_state_owner: "python.WorkerPoolStore",
    });
    this.jobs.set(created.job_id, created);
    this.publish(created);
    return structuredClone(created);
  }

  get(jobId: string): OmpProjectionJob | null {
    const value = this.jobs.get(jobId);
    if (!value) return null;
    assertProjectionJob(value);
    return structuredClone(value);
  }

  require(jobId: string): OmpProjectionJob {
    const value = this.get(jobId);
    if (!value)
      throw new E03RuntimeError("omp_job_not_found", `OMP job ${jobId} was not found`);
    return value;
  }

  list(filter: ProjectionJobFilter = {}): OmpProjectionJob[] {
    const phases = filter.phases ? new Set(filter.phases) : null;
    return [...this.jobs.values()]
      .filter((job) => !filter.ownerSessionId || job.owner_session_id === filter.ownerSessionId)
      .filter((job) => !filter.taskId || job.task_id === filter.taskId)
      .filter((job) => !phases || phases.has(job.phase))
      .sort((left, right) =>
        left.queued_at.localeCompare(right.queued_at) || left.job_id.localeCompare(right.job_id)
      )
      .map((job) => {
        assertProjectionJob(job);
        return structuredClone(job);
      });
  }

  activeCount(ownerSessionId?: string): number {
    return this.list({ ownerSessionId, phases: ["running"] }).length;
  }

  queuedCount(ownerSessionId?: string): number {
    return this.list({ ownerSessionId, phases: ["queued"] }).length;
  }

  start(jobId: string): OmpProjectionJob {
    const current = this.require(jobId);
    if (current.phase === "running") return current;
    if (current.phase !== "queued")
      throw new E03RuntimeError(
        "omp_job_not_startable",
        `OMP job ${jobId} cannot start from ${current.phase}`,
      );
    return this.transition(current, "running", {
      started_at: this.clock.now(),
      parked_at: null,
      error: "",
      progress: progressValue({
        ...current.progress,
        sequence: current.progress.sequence + 1,
        message: "physical worker permit acquired",
        observed_at: this.clock.now(),
      }),
    });
  }

  progress(
    jobId: string,
    update: {
      message: string;
      completedUnits?: number;
      totalUnits?: number;
      usage?: JsonObject;
    },
  ): OmpProjectionJob {
    const current = this.require(jobId);
    if (current.phase !== "running" && current.phase !== "parked")
      throw new E03RuntimeError(
        "omp_job_progress_rejected",
        `OMP job ${jobId} cannot record progress in ${current.phase}`,
      );
    const total = update.totalUnits ?? current.progress.total_units;
    const completed = update.completedUnits ?? current.progress.completed_units;
    if (!Number.isSafeInteger(total) || total < 1)
      throw new E03RuntimeError("omp_job_invalid_progress", "progress total must be positive");
    if (!Number.isSafeInteger(completed) || completed < 0 || completed > total)
      throw new E03RuntimeError(
        "omp_job_invalid_progress",
        "progress completion must be within the total",
      );
    return this.revise(current, {
      progress: progressValue({
        sequence: current.progress.sequence + 1,
        message: required(update.message, "progress message"),
        completed_units: completed,
        total_units: total,
        usage: { ...current.progress.usage, ...(update.usage ?? {}) },
        observed_at: this.clock.now(),
      }),
    });
  }

  park(jobId: string, reason = "execution projection parked"): OmpProjectionJob {
    const current = this.require(jobId);
    if (current.phase === "parked") return current;
    if (current.phase !== "running")
      throw new E03RuntimeError(
        "omp_job_not_parkable",
        `OMP job ${jobId} cannot park from ${current.phase}`,
      );
    return this.transition(current, "parked", {
      parked_at: this.clock.now(),
      progress: progressValue({
        ...current.progress,
        sequence: current.progress.sequence + 1,
        message: required(reason, "park reason"),
        observed_at: this.clock.now(),
      }),
    });
  }

  revive(jobId: string): OmpProjectionJob {
    const current = this.require(jobId);
    if (current.phase === "running") return current;
    if (current.phase !== "parked")
      throw new E03RuntimeError(
        "omp_job_not_revivable",
        `OMP job ${jobId} cannot revive from ${current.phase}`,
      );
    return this.transition(current, "running", {
      parked_at: null,
      progress: progressValue({
        ...current.progress,
        sequence: current.progress.sequence + 1,
        message: "execution projection revived",
        observed_at: this.clock.now(),
      }),
    });
  }

  complete(jobId: string, result: unknown, usage: JsonObject = {}): OmpProjectionJob {
    const current = this.require(jobId);
    if (current.phase === "completed") return current;
    this.assertSettleable(current);
    return this.transition(current, "completed", {
      settled_at: this.clock.now(),
      result_digest: digest(result ?? null),
      error: "",
      progress: progressValue({
        sequence: current.progress.sequence + 1,
        message: "physical dispatch completed",
        completed_units: current.progress.total_units,
        total_units: current.progress.total_units,
        usage: { ...current.progress.usage, ...usage },
        observed_at: this.clock.now(),
      }),
    });
  }

  fail(jobId: string, error: unknown): OmpProjectionJob {
    const current = this.require(jobId);
    if (current.phase === "failed") return current;
    this.assertSettleable(current);
    const message = error instanceof Error ? error.message : String(error || "dispatch failed");
    return this.transition(current, "failed", {
      settled_at: this.clock.now(),
      error: message,
      progress: progressValue({
        ...current.progress,
        sequence: current.progress.sequence + 1,
        message: `physical dispatch failed: ${message}`,
        observed_at: this.clock.now(),
      }),
    });
  }

  cancel(jobId: string, reason = "dispatch cancelled"): OmpProjectionJob {
    const current = this.require(jobId);
    if (current.phase === "cancelled") return current;
    if (terminalProjectionPhase(current.phase))
      throw new E03RuntimeError(
        "omp_job_terminal",
        `OMP job ${jobId} is already ${current.phase}`,
      );
    return this.transition(current, "cancelled", {
      settled_at: this.clock.now(),
      error: required(reason, "cancellation reason"),
      progress: progressValue({
        ...current.progress,
        sequence: current.progress.sequence + 1,
        message: reason,
        observed_at: this.clock.now(),
      }),
    });
  }

  cancelOwner(ownerSessionId: string, reason: string): OmpProjectionJob[] {
    const cancelled: OmpProjectionJob[] = [];
    for (const job of this.list({ ownerSessionId }))
      if (!terminalProjectionPhase(job.phase)) cancelled.push(this.cancel(job.job_id, reason));
    return cancelled;
  }

  wait(jobId: string, timeoutMs = 0): Promise<OmpProjectionJob> {
    const current = this.require(jobId);
    if (terminalProjectionPhase(current.phase)) return Promise.resolve(current);
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 0)
      throw new E03RuntimeError("omp_job_invalid_timeout", "wait timeout must be non-negative");
    return new Promise<OmpProjectionJob>((resolve, reject) => {
      const waiter: Waiter = { resolve, reject, timer: null };
      if (timeoutMs)
        waiter.timer = setTimeout(() => {
          this.removeWaiter(jobId, waiter);
          reject(
            new E03RuntimeError(
              "omp_job_wait_timeout",
              `OMP job ${jobId} did not settle within ${timeoutMs}ms`,
            ),
          );
        }, timeoutMs);
      const group = this.waiters.get(jobId) ?? new Set<Waiter>();
      group.add(waiter);
      this.waiters.set(jobId, group);
    });
  }

  async drain(ownerSessionId: string, timeoutMs = 30_000): Promise<OmpProjectionJob[]> {
    const pending = this.list({ ownerSessionId }).filter(
      (job) => !terminalProjectionPhase(job.phase),
    );
    if (!pending.length) return this.list({ ownerSessionId });
    await Promise.all(pending.map((job) => this.wait(job.job_id, timeoutMs)));
    return this.list({ ownerSessionId });
  }

  subscribe(listener: (job: OmpProjectionJob) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private assertSettleable(job: OmpProjectionJob): void {
    if (job.phase !== "running" && job.phase !== "parked")
      throw new E03RuntimeError(
        "omp_job_not_settleable",
        `OMP job ${job.job_id} cannot settle from ${job.phase}`,
      );
  }

  private revise(
    current: OmpProjectionJob,
    changes: Partial<OmpProjectionJob>,
  ): OmpProjectionJob {
    const next = sealProjectionJob({
      ...current,
      ...changes,
      revision: current.revision + 1,
      digest: "",
    });
    this.jobs.set(next.job_id, next);
    this.publish(next);
    return structuredClone(next);
  }

  private transition(
    current: OmpProjectionJob,
    phase: OmpProjectionJobPhase,
    changes: Partial<OmpProjectionJob>,
  ): OmpProjectionJob {
    return this.revise(current, { ...changes, phase });
  }

  private publish(job: OmpProjectionJob): void {
    assertProjectionJob(job);
    for (const listener of this.listeners) listener(structuredClone(job));
    if (!terminalProjectionPhase(job.phase)) return;
    const group = this.waiters.get(job.job_id);
    if (!group) return;
    this.waiters.delete(job.job_id);
    for (const waiter of group) {
      if (waiter.timer) clearTimeout(waiter.timer);
      waiter.resolve(structuredClone(job));
    }
  }

  private removeWaiter(jobId: string, waiter: Waiter): void {
    const group = this.waiters.get(jobId);
    if (!group) return;
    group.delete(waiter);
    if (!group.size) this.waiters.delete(jobId);
  }
}

interface Waiter {
  resolve(job: OmpProjectionJob): void;
  reject(error: Error): void;
  timer: ReturnType<typeof setTimeout> | null;
}

function progressValue(value: OmpProjectionProgress): OmpProjectionProgress {
  return structuredClone(value);
}

function required(value: string, field: string): string {
  const selected = value.trim();
  if (!selected)
    throw new E03RuntimeError("omp_job_invalid_identity", `${field} is empty`);
  return selected;
}
