import { randomUUID } from "node:crypto";

import type { JsonObject } from "../contracts.ts";
import type { SupplementaryObservationEmitter } from "./execution-supervisor.ts";
import type { WatchdogObservation, WatchdogRefs } from "./runtime.ts";

export type WorkerProcessPhase =
  | "starting"
  | "running"
  | "degraded"
  | "exited"
  | "restarting"
  | "stopped"
  | "quarantined";

export type DurableJobPhase =
  | "queued"
  | "claimed"
  | "running"
  | "checkpointed"
  | "requeued"
  | "completed"
  | "failed";

export interface WorkerBindingInput {
  refs: WatchdogRefs;
  generation: number;
  pid: number;
  heartbeatIntervalMs: number;
  heartbeatGraceIntervals?: number;
  maxRestarts?: number;
  restartWindowMs?: number;
  metadata?: JsonObject;
}

export interface DurableJobInput {
  jobId: string;
  attemptId: string;
  checkpointId: string;
  idempotencyKey: string;
  payloadDigest: string;
  metadata?: JsonObject;
}

export interface DurableJobReceipt {
  jobId: string;
  workerId: string;
  generation: number;
  phase: DurableJobPhase;
  attemptId: string;
  checkpointId: string;
  requeueCount: number;
  duplicate: boolean;
  revision: number;
}

interface DurableJobRecord {
  jobId: string;
  workerId: string;
  generation: number;
  phase: DurableJobPhase;
  attemptId: string;
  checkpointId: string;
  idempotencyKey: string;
  payloadDigest: string;
  resultDigest: string;
  requeueCount: number;
  revision: number;
  metadata: JsonObject;
}

interface WorkerProcessRecord {
  refs: WatchdogRefs;
  generation: number;
  pid: number;
  phase: WorkerProcessPhase;
  heartbeatIntervalMs: number;
  heartbeatGraceIntervals: number;
  heartbeatSequence: number;
  attachedAtMs: number;
  lastHeartbeatAtMs: number;
  maxRestarts: number;
  restartWindowMs: number;
  restartHistory: number[];
  exitCode: number | null;
  expectedStop: boolean;
  observationId: string;
  revision: number;
  jobs: Map<string, DurableJobRecord>;
  jobKeys: Map<string, string>;
  metadata: JsonObject;
}

function runtimeId(prefix: string): string {
  return prefix + "_" + randomUUID().replaceAll("-", "");
}

function nowIso(): string {
  return new Date().toISOString();
}

function requireIdentity(name: string, value: string): string {
  const selected = value.trim();
  if (!selected) throw new Error(name + " must not be empty");
  return selected;
}

/**
 * OMP-derived process restart and durable-job handback supervisor.
 *
 * The source robomp/worker mechanisms requeue durable work after a process
 * restart. Zyra keeps that lifecycle in TypeScript but fences it with worker
 * generation, job idempotency and checkpoint identity. The supervisor emits a
 * worker-unavailable observation; scheduler reroute and recovery strategy are
 * left to Python state owners and M1-07C.
 */
export class WorkerRestartSupervisor {
  readonly observerId = "ts-process-transport";
  readonly sourceRevision = "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca";
  readonly migrationMode = "cropped_same_language";

  #workers = new Map<string, WorkerProcessRecord>();
  #jobOwners = new Map<string, string>();
  #emit: SupplementaryObservationEmitter;
  #now: () => number;
  #attachCount = 0;
  #heartbeatCount = 0;
  #lostHeartbeatCount = 0;
  #exitCount = 0;
  #restartCount = 0;
  #quarantineCount = 0;
  #jobCount = 0;
  #requeueCount = 0;
  #duplicateJobCount = 0;

  constructor(emit: SupplementaryObservationEmitter, now: () => number = () => Date.now()) {
    this.#emit = emit;
    this.#now = now;
  }

  attach(input: WorkerBindingInput): void {
    const workerId = requireIdentity("workerId", input.refs.workerId);
    requireIdentity("runId", input.refs.runId);
    requireIdentity("taskId", input.refs.taskId);
    if (!Number.isSafeInteger(input.generation) || input.generation < 0) {
      throw new Error("worker generation must be non-negative");
    }
    if (!Number.isSafeInteger(input.pid) || input.pid < 1) throw new Error("worker pid must be positive");
    const current = this.#workers.get(workerId);
    if (current !== undefined) {
      if (input.generation < current.generation) throw new Error("stale worker generation");
      if (input.generation === current.generation && current.phase !== "stopped") {
        if (current.pid !== input.pid) throw new Error("worker generation cannot change pid");
        return;
      }
      if (!this.#terminal(current.phase) && current.phase !== "restarting") {
        throw new Error("new worker generation requires prior exit/restart transition");
      }
    }
    const now = this.#now();
    const record: WorkerProcessRecord = {
      refs: structuredClone(input.refs),
      generation: input.generation,
      pid: input.pid,
      phase: "running",
      heartbeatIntervalMs: this.#positive(input.heartbeatIntervalMs, "heartbeatIntervalMs"),
      heartbeatGraceIntervals: this.#positive(input.heartbeatGraceIntervals ?? 3, "heartbeatGraceIntervals"),
      heartbeatSequence: 0,
      attachedAtMs: now,
      lastHeartbeatAtMs: now,
      maxRestarts: this.#positive(input.maxRestarts ?? 4, "maxRestarts"),
      restartWindowMs: this.#positive(input.restartWindowMs ?? 60_000, "restartWindowMs"),
      restartHistory: current ? [...current.restartHistory] : [],
      exitCode: null,
      expectedStop: false,
      observationId: "",
      revision: 1,
      jobs: new Map(),
      jobKeys: new Map(),
      metadata: structuredClone(input.metadata ?? {}),
    };
    if (current !== undefined) {
      for (const job of current.jobs.values()) {
        record.jobs.set(job.jobId, structuredClone(job));
        record.jobKeys.set(job.idempotencyKey, job.jobId);
      }
    }
    this.#workers.set(workerId, record);
    this.#attachCount += 1;
  }

  heartbeat(workerId: string, generation: number, sequence: number, atMs: number = this.#now()): boolean {
    const record = this.#workers.get(workerId);
    if (record === undefined || record.generation !== generation) return false;
    if (record.phase !== "running" && record.phase !== "degraded") return false;
    if (!Number.isSafeInteger(sequence) || sequence <= record.heartbeatSequence) return false;
    if (atMs < record.lastHeartbeatAtMs) return false;
    record.heartbeatSequence = sequence;
    record.lastHeartbeatAtMs = atMs;
    record.phase = "running";
    record.revision += 1;
    this.#heartbeatCount += 1;
    return true;
  }

  async sweep(atMs: number = this.#now()): Promise<string[]> {
    const lost: string[] = [];
    for (const [workerId, record] of this.#workers) {
      if (record.phase !== "running" && record.phase !== "degraded") continue;
      const deadlineMs = record.heartbeatIntervalMs * record.heartbeatGraceIntervals;
      const elapsedMs = Math.max(0, atMs - record.lastHeartbeatAtMs);
      if (elapsedMs < record.heartbeatIntervalMs) continue;
      if (elapsedMs < deadlineMs) {
        record.phase = "degraded";
        record.revision += 1;
        continue;
      }
      record.phase = "exited";
      record.observationId = runtimeId("ts_worker_heartbeat_observation");
      record.revision += 1;
      this.#lostHeartbeatCount += 1;
      lost.push(workerId);
      await this.#emit(this.observerId, this.#observation(record, {
        code: "heartbeat_lost",
        reasonCode: "heartbeat_deadline_elapsed",
        elapsedMs,
        deadlineMs,
      }));
    }
    return lost.sort();
  }

  registerJob(workerId: string, generation: number, input: DurableJobInput): DurableJobReceipt {
    const record = this.#requireActive(workerId, generation);
    requireIdentity("jobId", input.jobId);
    requireIdentity("attemptId", input.attemptId);
    const idempotencyKey = requireIdentity("idempotencyKey", input.idempotencyKey);
    requireIdentity("payloadDigest", input.payloadDigest);
    const owner = this.#jobOwners.get(idempotencyKey);
    if (owner !== undefined) {
      const ownerWorker = this.#workers.get(owner);
      const priorId = ownerWorker?.jobKeys.get(idempotencyKey);
      const prior = priorId ? ownerWorker?.jobs.get(priorId) : undefined;
      if (prior === undefined) throw new Error("durable job key index is corrupt");
      if (prior.payloadDigest !== input.payloadDigest) {
        throw new Error("durable job replay has a different payload digest");
      }
      this.#duplicateJobCount += 1;
      return this.#jobReceipt(prior, true);
    }
    const job: DurableJobRecord = {
      jobId: input.jobId,
      workerId,
      generation,
      phase: "claimed",
      attemptId: input.attemptId,
      checkpointId: input.checkpointId,
      idempotencyKey,
      payloadDigest: input.payloadDigest,
      resultDigest: "",
      requeueCount: 0,
      revision: 1,
      metadata: structuredClone(input.metadata ?? {}),
    };
    record.jobs.set(job.jobId, job);
    record.jobKeys.set(idempotencyKey, job.jobId);
    this.#jobOwners.set(idempotencyKey, workerId);
    record.revision += 1;
    this.#jobCount += 1;
    return this.#jobReceipt(job, false);
  }

  startJob(workerId: string, generation: number, jobId: string): DurableJobReceipt {
    const record = this.#requireActive(workerId, generation);
    const job = this.#requireJob(record, jobId);
    if (job.phase !== "claimed" && job.phase !== "requeued") {
      throw new Error("durable job cannot start from " + job.phase);
    }
    job.phase = "running";
    job.generation = generation;
    job.revision += 1;
    record.revision += 1;
    return this.#jobReceipt(job, false);
  }

  checkpointJob(
    workerId: string,
    generation: number,
    jobId: string,
    checkpointId: string,
  ): DurableJobReceipt {
    const record = this.#requireActive(workerId, generation);
    const job = this.#requireJob(record, jobId);
    if (job.phase !== "running") throw new Error("only running durable job can checkpoint");
    job.checkpointId = requireIdentity("checkpointId", checkpointId);
    job.phase = "checkpointed";
    job.revision += 1;
    record.revision += 1;
    return this.#jobReceipt(job, false);
  }

  completeJob(
    workerId: string,
    generation: number,
    jobId: string,
    resultDigest: string,
  ): DurableJobReceipt {
    const record = this.#requireActive(workerId, generation);
    const job = this.#requireJob(record, jobId);
    if (job.phase === "completed") {
      if (job.resultDigest !== resultDigest) throw new Error("durable job result digest changed on replay");
      return this.#jobReceipt(job, true);
    }
    if (!["running", "checkpointed"].includes(job.phase)) {
      throw new Error("durable job cannot complete from " + job.phase);
    }
    job.resultDigest = requireIdentity("resultDigest", resultDigest);
    job.phase = "completed";
    job.revision += 1;
    record.revision += 1;
    return this.#jobReceipt(job, false);
  }

  async processExited(
    workerId: string,
    generation: number,
    exitCode: number,
    reasonCode: string,
  ): Promise<DurableJobReceipt[]> {
    const record = this.#require(workerId, generation);
    if (record.expectedStop || record.phase === "stopped") return [];
    record.phase = "exited";
    record.exitCode = exitCode;
    record.observationId = runtimeId("ts_worker_exit_observation");
    record.revision += 1;
    this.#exitCount += 1;
    const requeued = this.#requeueJobs(record);
    await this.#emit(this.observerId, this.#observation(record, {
      code: "worker_lost",
      reasonCode: requireIdentity("reasonCode", reasonCode),
      elapsedMs: null,
      deadlineMs: null,
    }));
    return requeued;
  }

  beginRestart(workerId: string, generation: number, atMs: number = this.#now()): boolean {
    const record = this.#require(workerId, generation);
    if (record.phase !== "exited") throw new Error("worker restart requires exited phase");
    const cutoff = atMs - record.restartWindowMs;
    record.restartHistory = record.restartHistory.filter((value) => value >= cutoff);
    if (record.restartHistory.length >= record.maxRestarts) {
      record.phase = "quarantined";
      record.revision += 1;
      this.#quarantineCount += 1;
      return false;
    }
    record.restartHistory.push(atMs);
    record.phase = "restarting";
    record.revision += 1;
    this.#restartCount += 1;
    return true;
  }

  finishRestart(
    workerId: string,
    priorGeneration: number,
    nextGeneration: number,
    pid: number,
  ): DurableJobReceipt[] {
    const prior = this.#require(workerId, priorGeneration);
    if (prior.phase !== "restarting") throw new Error("worker is not restarting");
    if (nextGeneration !== priorGeneration + 1) throw new Error("worker restart generation must be contiguous");
    const nextRefs = { ...structuredClone(prior.refs), sourceStateRevision: nextGeneration };
    this.attach({
      refs: nextRefs,
      generation: nextGeneration,
      pid,
      heartbeatIntervalMs: prior.heartbeatIntervalMs,
      heartbeatGraceIntervals: prior.heartbeatGraceIntervals,
      maxRestarts: prior.maxRestarts,
      restartWindowMs: prior.restartWindowMs,
      metadata: prior.metadata,
    });
    const next = this.#require(workerId, nextGeneration);
    for (const job of next.jobs.values()) {
      if (job.phase === "requeued") job.generation = nextGeneration;
    }
    next.revision += 1;
    return [...next.jobs.values()]
      .filter((job) => job.phase === "requeued")
      .map((job) => this.#jobReceipt(job, false));
  }

  stop(workerId: string, generation: number): void {
    const record = this.#require(workerId, generation);
    record.expectedStop = true;
    record.phase = "stopped";
    record.revision += 1;
  }

  snapshot(): JsonObject {
    const workers: JsonObject = {};
    for (const [workerId, record] of [...this.#workers.entries()].sort(([left], [right]) => left.localeCompare(right))) {
      workers[workerId] = {
        run_id: record.refs.runId,
        task_id: record.refs.taskId,
        generation: record.generation,
        pid: record.pid,
        phase: record.phase,
        heartbeat_sequence: record.heartbeatSequence,
        heartbeat_interval_ms: record.heartbeatIntervalMs,
        heartbeat_grace_intervals: record.heartbeatGraceIntervals,
        last_heartbeat_at_ms: record.lastHeartbeatAtMs,
        restart_history: [...record.restartHistory],
        max_restarts: record.maxRestarts,
        exit_code: record.exitCode,
        expected_stop: record.expectedStop,
        observation_id: record.observationId,
        jobs: [...record.jobs.values()].map((job) => ({
          job_id: job.jobId,
          generation: job.generation,
          phase: job.phase,
          attempt_id: job.attemptId,
          checkpoint_id: job.checkpointId,
          requeue_count: job.requeueCount,
          revision: job.revision,
        })),
        revision: record.revision,
      };
    }
    return {
      schema: "zyra.typescript-worker-restart-supervisor/v1",
      source_repo: "oh-my-pi",
      source_revision: this.sourceRevision,
      migration_mode: this.migrationMode,
      workers,
      counts: {
        attaches: this.#attachCount,
        heartbeats: this.#heartbeatCount,
        heartbeat_losses: this.#lostHeartbeatCount,
        exits: this.#exitCount,
        restarts: this.#restartCount,
        quarantines: this.#quarantineCount,
        jobs: this.#jobCount,
        requeues: this.#requeueCount,
        duplicate_jobs: this.#duplicateJobCount,
      },
      durable_job_requeue_generation_fenced: true,
      completed_job_requeued: false,
      canonical_worker_lease_owner: "python.WorkerPoolStore",
      canonical_signal_owner: "python.FaultStateStore",
      recovery_plan_owner: "M1-S07C",
    };
  }

  #requeueJobs(record: WorkerProcessRecord): DurableJobReceipt[] {
    const receipts: DurableJobReceipt[] = [];
    for (const job of record.jobs.values()) {
      if (["completed", "failed", "queued", "requeued"].includes(job.phase)) continue;
      job.phase = "requeued";
      job.requeueCount += 1;
      job.metadata = {
        ...job.metadata,
        requeued_from_generation: record.generation,
        resume_checkpoint_id: job.checkpointId,
      };
      job.revision += 1;
      this.#requeueCount += 1;
      receipts.push(this.#jobReceipt(job, false));
    }
    return receipts;
  }

  #observation(
    record: WorkerProcessRecord,
    input: { code: "worker_lost" | "heartbeat_lost"; reasonCode: string; elapsedMs: number | null; deadlineMs: number | null },
  ): WatchdogObservation {
    return {
      observationId: record.observationId,
      observerId: this.observerId,
      category: "worker",
      code: input.code,
      status: "unavailable",
      summary: "Worker process crossed a generation-fenced heartbeat or exit boundary.",
      errorType: input.code === "heartbeat_lost" ? "WorkerHeartbeatLost" : "WorkerProcessExited",
      retryable: record.phase !== "quarantined",
      terminal: true,
      elapsedMs: input.elapsedMs,
      deadlineMs: input.deadlineMs,
      statusCode: null,
      refs: {
        ...structuredClone(record.refs),
        observationId: record.observationId,
        sourceStateRevision: record.revision,
      },
      details: {
        ...structuredClone(record.metadata),
        generation: record.generation,
        pid: record.pid,
        exit_code: record.exitCode,
        reason_code: input.reasonCode,
        heartbeat_sequence: record.heartbeatSequence,
        restart_count_in_window: record.restartHistory.length,
        max_restarts: record.maxRestarts,
        requeued_job_ids: [...record.jobs.values()]
          .filter((job) => job.phase === "requeued")
          .map((job) => job.jobId)
          .sort(),
        checkpointed_requeue: true,
        reroute_selected: false,
      },
      observedAt: nowIso(),
    };
  }

  #jobReceipt(job: DurableJobRecord, duplicate: boolean): DurableJobReceipt {
    return {
      jobId: job.jobId,
      workerId: job.workerId,
      generation: job.generation,
      phase: job.phase,
      attemptId: job.attemptId,
      checkpointId: job.checkpointId,
      requeueCount: job.requeueCount,
      duplicate,
      revision: job.revision,
    };
  }

  #require(workerId: string, generation: number): WorkerProcessRecord {
    const record = this.#workers.get(workerId);
    if (record === undefined) throw new Error("worker is not attached: " + workerId);
    if (record.generation !== generation) throw new Error("stale worker generation");
    return record;
  }

  #requireActive(workerId: string, generation: number): WorkerProcessRecord {
    const record = this.#require(workerId, generation);
    if (record.phase !== "running" && record.phase !== "degraded") {
      throw new Error("worker does not accept jobs in phase " + record.phase);
    }
    return record;
  }

  #requireJob(record: WorkerProcessRecord, jobId: string): DurableJobRecord {
    const job = record.jobs.get(jobId);
    if (job === undefined) throw new Error("durable job does not exist: " + jobId);
    return job;
  }

  #terminal(phase: WorkerProcessPhase): boolean {
    return ["exited", "stopped", "quarantined"].includes(phase);
  }

  #positive(value: number, name: string): number {
    if (!Number.isSafeInteger(value) || value < 1) throw new Error(name + " must be a positive integer");
    return value;
  }
}

export function workerRestartSupervisorContract(): JsonObject {
  return {
    schema: "zyra.typescript-worker-restart-supervisor-contract/v1",
    source_repo: "oh-my-pi",
    source_revision: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
    migration_mode: "cropped_same_language",
    source_mechanisms: [
      "worker process restart signal",
      "durable job requeue",
      "bounded restart window",
    ],
    generation_fenced: true,
    heartbeat_deadline_active: true,
    checkpoint_preserved_on_requeue: true,
    completed_job_requeued: false,
    canonical_worker_lease_owner: "python.WorkerPoolStore",
    recovery_plan_owner: "M1-S07C",
  };
}
