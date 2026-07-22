import type { JsonObject } from "../contracts.ts";
import { digest, E03RuntimeError, type E03Clock, type E03TaskState, SystemE03Clock } from "../e03/contracts.ts";
import { parsePhysicalDispatch, type PhysicalDispatchProjection } from "./contracts.ts";
import { OmpAbortSafeSemaphore } from "./semaphore.ts";

export type OmpSessionPhase =
  | "queued"
  | "foreground"
  | "background"
  | "parked"
  | "completed"
  | "failed"
  | "cancelled";

export type OmpTypedYieldKind =
  | "result"
  | "artifact"
  | "handoff"
  | "cancelled"
  | "failed";

export interface OmpTypedYieldProjection extends JsonObject {
  yield_id: string;
  task_id: string;
  attempt_id: string;
  lease_id: string;
  sequence: 1;
  kind: OmpTypedYieldKind;
  summary: string;
  payload_digest: string;
  artifact_ids: string[];
  created_at: string;
  projection_only: true;
  canonical_owner: "python.WorkerPoolIntegrationRepository";
  digest: string;
}

export interface OmpSessionExecutionProjection extends JsonObject {
  projection_id: string;
  owner_session_id: string;
  task_id: string;
  attempt_id: string;
  lease_id: string;
  worker_id: string;
  backend_id: string;
  execution_mode: "foreground" | "background";
  phase: OmpSessionPhase;
  revision: number;
  permit_held: boolean;
  queued_at: string;
  admitted_at: string | null;
  parked_at: string | null;
  settled_at: string | null;
  promoted_at: string | null;
  cancellation_reason: string;
  typed_yield: OmpTypedYieldProjection | null;
  projection_only: true;
  canonical_owner: "python.WorkerPoolStore";
  digest: string;
}

export interface OmpSessionStateProjection extends JsonObject {
  owner_session_id: string;
  concurrency_limit: number;
  active: number;
  pending: number;
  foreground_task_ids: string[];
  background_task_ids: string[];
  parked_task_ids: string[];
}

export interface OmpSessionRuntimeSnapshot extends JsonObject {
  version: "zyra.omp-session-admission/v1";
  projection_only: true;
  canonical_owner: "python.WorkerPoolStore";
  executions: OmpSessionExecutionProjection[];
  sessions: OmpSessionStateProjection[];
  process_epoch: number;
  checksum: string;
}

export interface OmpSessionPermit {
  readonly taskId: string;
  readonly ownerSessionId: string;
  readonly signal: AbortSignal;
  release(): void;
}

interface LiveExecution {
  projection: OmpSessionExecutionProjection;
  release: (() => void) | null;
  pendingPermit: Promise<void> | null;
  controller: AbortController;
  parentAbort: (() => void) | null;
  parentSignal: AbortSignal | null;
}

interface SessionAdmission {
  limit: number;
  semaphore: OmpAbortSafeSemaphore;
}

/**
 * OMP-derived shared session admission and park/revive runtime.
 *
 * This is intentionally process-local: after restart ``rehydrate`` consumes
 * checksummed Python lease projections and recreates coordination state. It cannot
 * renew, fence, release or restore a canonical physical lease.
 */
export class OmpSessionAdmissionRuntime {
  private readonly executions = new Map<string, LiveExecution>();
  private readonly sessions = new Map<string, SessionAdmission>();
  private processEpoch = 1;

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  async enter(
    task: E03TaskState,
    dispatch: PhysicalDispatchProjection,
    signal?: AbortSignal,
  ): Promise<OmpSessionPermit> {
    this.assertEnabled();
    const taskId = task.identity.taskId;
    const ownerSessionId = required(task.identity.parentSessionId, "owner session id");
    const prior = this.executions.get(taskId);
    if (prior) {
      assertExecution(prior.projection);
      if (
        prior.projection.attempt_id !== dispatch.attempt_id ||
        prior.projection.lease_id !== dispatch.lease_id ||
        prior.projection.owner_session_id !== ownerSessionId
      )
        throw new E03RuntimeError(
          "omp_session_task_conflict",
          `task ${taskId} is already projected onto another physical attempt`,
        );
      if (terminal(prior.projection.phase))
        throw new E03RuntimeError(
          "omp_session_task_terminal",
          `task ${taskId} process projection is already ${prior.projection.phase}`,
        );
      if (prior.release)
        return this.permitFor(taskId, prior);
      if (prior.projection.phase === "parked")
        throw new E03RuntimeError(
          "omp_session_revive_required",
          `task ${taskId} is parked and must use revive`,
        );
      if (prior.pendingPermit) {
        const pending = prior.pendingPermit;
        await pending;
        if (!prior.release)
          throw new E03RuntimeError(
            "omp_session_permit_missing",
            `task ${taskId} admission completed without a session permit`,
          );
        return this.permitFor(taskId, prior);
      }
    }
    const controller = prior?.controller ?? new AbortController();
    const live = prior ?? {
      projection: sealExecution({
        projection_id: `omp-session:${dispatch.lease_id}:${dispatch.attempt}`,
        owner_session_id: ownerSessionId,
        task_id: taskId,
        attempt_id: dispatch.attempt_id,
        lease_id: dispatch.lease_id,
        worker_id: dispatch.worker_id,
        backend_id: dispatch.backend_id,
        execution_mode: task.executionMode,
        phase: "queued",
        revision: 1,
        permit_held: false,
        queued_at: this.clock.now(),
        admitted_at: null,
        parked_at: null,
        settled_at: null,
        promoted_at: null,
        cancellation_reason: "",
        typed_yield: null,
        projection_only: true,
        canonical_owner: "python.WorkerPoolStore",
      }),
      release: null,
      pendingPermit: null,
      controller,
      parentAbort: null,
      parentSignal: null,
    } satisfies LiveExecution;
    this.detachParentAbort(live);
    this.attachParentAbort(live, signal);
    if (controller.signal.aborted) {
      this.detachParentAbort(live);
      throw abortError(controller.signal.reason);
    }
    this.executions.set(taskId, live);
    const session = this.session(ownerSessionId, dispatch.concurrency_limit);
    const pending = session.semaphore.acquire(controller.signal).then((release) => {
      live.release = release;
      live.projection = reviseExecution(live.projection, {
        phase: task.executionMode,
        execution_mode: task.executionMode,
        permit_held: true,
        admitted_at: live.projection.admitted_at ?? this.clock.now(),
        parked_at: null,
        cancellation_reason: "",
      });
    });
    live.pendingPermit = pending;
    try {
      await pending;
    } catch (error) {
      this.detachParentAbort(live);
      if (!prior && !terminal(live.projection.phase)) this.executions.delete(taskId);
      throw error;
    } finally {
      if (live.pendingPermit === pending) live.pendingPermit = null;
    }
    return this.permitFor(taskId, live);
  }

  promoteToBackground(taskId: string): OmpSessionExecutionProjection {
    const live = this.requireLive(taskId);
    if (live.projection.phase === "background") return clone(live.projection);
    if (live.projection.phase !== "foreground")
      throw new E03RuntimeError(
        "omp_session_promotion_rejected",
        `task ${taskId} cannot promote from ${live.projection.phase}`,
      );
    live.projection = reviseExecution(live.projection, {
      phase: "background",
      execution_mode: "background",
      promoted_at: this.clock.now(),
    });
    return clone(live.projection);
  }

  park(taskId: string, reason: string): OmpSessionExecutionProjection {
    const live = this.requireLive(taskId);
    if (live.projection.phase === "parked") return clone(live.projection);
    if (live.projection.phase !== "foreground" && live.projection.phase !== "background")
      throw new E03RuntimeError(
        "omp_session_park_rejected",
        `task ${taskId} cannot park from ${live.projection.phase}`,
      );
    live.release?.();
    live.release = null;
    live.projection = reviseExecution(live.projection, {
      phase: "parked",
      permit_held: false,
      parked_at: this.clock.now(),
      cancellation_reason: required(reason, "park reason"),
    });
    return clone(live.projection);
  }

  async revive(
    taskId: string,
    dispatch: PhysicalDispatchProjection,
    signal?: AbortSignal,
  ): Promise<OmpSessionExecutionProjection> {
    this.assertEnabled();
    const live = this.requireLive(taskId);
    if (live.projection.phase !== "parked")
      throw new E03RuntimeError(
        "omp_session_revive_rejected",
        `task ${taskId} cannot revive from ${live.projection.phase}`,
      );
    if (dispatch.lease_id !== live.projection.lease_id || dispatch.attempt_id !== live.projection.attempt_id)
      throw new E03RuntimeError(
        "omp_session_revive_binding",
        "parked task cannot revive onto another physical lease",
      );
    if (live.pendingPermit) {
      const pending = live.pendingPermit;
      await pending;
      return clone(live.projection);
    }
    if (signal) {
      this.detachParentAbort(live);
      this.attachParentAbort(live, signal);
    }
    const session = this.session(live.projection.owner_session_id, dispatch.concurrency_limit);
    const pending = session.semaphore.acquire(live.controller.signal).then((release) => {
      live.release = release;
      live.projection = reviseExecution(live.projection, {
        phase: live.projection.execution_mode,
        permit_held: true,
        parked_at: null,
        cancellation_reason: "",
      });
    });
    live.pendingPermit = pending;
    try {
      await pending;
    } finally {
      if (live.pendingPermit === pending) live.pendingPermit = null;
    }
    return clone(live.projection);
  }

  cancel(taskId: string, reason: string): OmpSessionExecutionProjection {
    const live = this.requireLive(taskId);
    if (terminal(live.projection.phase)) return clone(live.projection);
    if (!live.projection.typed_yield)
      this.yieldOnce({
        taskId,
        kind: "cancelled",
        summary: required(reason, "cancellation reason"),
        payload: { reason },
      });
    if (!live.controller.signal.aborted) live.controller.abort(reason);
    live.release?.();
    live.release = null;
    live.projection = reviseExecution(live.projection, {
      phase: "cancelled",
      permit_held: false,
      settled_at: this.clock.now(),
      cancellation_reason: required(reason, "cancellation reason"),
    });
    this.detachParentAbort(live);
    return clone(live.projection);
  }

  yieldOnce(input: {
    taskId: string;
    kind: OmpTypedYieldKind;
    summary: string;
    payload: unknown;
    artifactIds?: readonly string[];
  }): OmpTypedYieldProjection {
    const live = this.requireLive(input.taskId);
    const artifactIds = [...new Set(input.artifactIds ?? [])].sort();
    const unsigned = {
      yield_id: `omp-yield:${digest({
        task_id: input.taskId,
        attempt_id: live.projection.attempt_id,
        kind: input.kind,
        summary: input.summary,
        payload: input.payload ?? null,
        artifact_ids: artifactIds,
      }).slice(0, 32)}`,
      task_id: input.taskId,
      attempt_id: live.projection.attempt_id,
      lease_id: live.projection.lease_id,
      sequence: 1 as const,
      kind: input.kind,
      summary: required(input.summary, "typed yield summary"),
      payload_digest: digest(input.payload ?? null),
      artifact_ids: artifactIds,
      created_at: this.clock.now(),
      projection_only: true as const,
      canonical_owner: "python.WorkerPoolIntegrationRepository" as const,
    };
    const candidate = { ...unsigned, digest: digest(unsigned) } satisfies OmpTypedYieldProjection;
    const prior = live.projection.typed_yield;
    if (prior) {
      assertYield(prior);
      const comparablePrior = {
        kind: prior.kind,
        summary: prior.summary,
        payload_digest: prior.payload_digest,
        artifact_ids: prior.artifact_ids,
      };
      const comparableCandidate = {
        kind: candidate.kind,
        summary: candidate.summary,
        payload_digest: candidate.payload_digest,
        artifact_ids: candidate.artifact_ids,
      };
      if (digest(comparablePrior) !== digest(comparableCandidate))
        throw new E03RuntimeError(
          "omp_typed_yield_conflict",
          `task ${input.taskId} attempted to commit a second typed yield`,
        );
      return clone(prior);
    }
    live.projection = reviseExecution(live.projection, { typed_yield: candidate });
    return clone(candidate);
  }

  settle(
    taskId: string,
    phase: "completed" | "failed" | "cancelled",
  ): OmpSessionExecutionProjection {
    const live = this.requireLive(taskId);
    if (terminal(live.projection.phase)) {
      if (live.projection.phase !== phase)
        throw new E03RuntimeError(
          "omp_session_terminal_conflict",
          `task ${taskId} is already ${live.projection.phase}, not ${phase}`,
        );
      return clone(live.projection);
    }
    if (!live.projection.typed_yield)
      throw new E03RuntimeError(
        "omp_typed_yield_required",
        `task ${taskId} cannot settle before one typed yield is projected`,
      );
    live.release?.();
    live.release = null;
    live.projection = reviseExecution(live.projection, {
      phase,
      permit_held: false,
      settled_at: this.clock.now(),
    });
    this.detachParentAbort(live);
    return clone(live.projection);
  }

  get(taskId: string): OmpSessionExecutionProjection | null {
    const live = this.executions.get(taskId);
    if (!live) return null;
    assertExecution(live.projection);
    return clone(live.projection);
  }

  list(ownerSessionId?: string): OmpSessionExecutionProjection[] {
    return [...this.executions.values()]
      .map((live) => live.projection)
      .filter((projection) => !ownerSessionId || projection.owner_session_id === ownerSessionId)
      .sort((left, right) =>
        left.queued_at.localeCompare(right.queued_at) || left.task_id.localeCompare(right.task_id)
      )
      .map((projection) => {
        assertExecution(projection);
        return clone(projection);
      });
  }

  clearProcessState(): void {
    for (const live of this.executions.values()) {
      if (!live.controller.signal.aborted)
        live.controller.abort("OMP process-local session state cleared");
      live.release?.();
      this.detachParentAbort(live);
    }
    this.executions.clear();
    this.sessions.clear();
    this.processEpoch += 1;
  }

  rehydrate(
    projections: readonly PhysicalDispatchProjection[],
    ownerSessionByTask: Readonly<Record<string, string>>,
    modeByTask: Readonly<Record<string, "foreground" | "background">> = {},
  ): OmpSessionExecutionProjection[] {
    this.assertEnabled();
    this.clearProcessState();
    for (const candidate of [...projections].sort((left, right) =>
      left.task_id.localeCompare(right.task_id) || left.attempt_id.localeCompare(right.attempt_id)
    )) {
      const dispatch = parsePhysicalDispatch(candidate, candidate.task_id);
      if (!dispatch)
        throw new E03RuntimeError(
          "omp_session_projection_required",
          `task ${candidate.task_id} has no canonical physical projection`,
        );
      const ownerSessionId = required(ownerSessionByTask[dispatch.task_id] ?? "", "owner session id");
      if (this.executions.has(dispatch.task_id))
        throw new E03RuntimeError(
          "omp_session_projection_duplicate",
          `multiple canonical projections target task ${dispatch.task_id}`,
        );
      const mode = modeByTask[dispatch.task_id] ?? "background";
      const projection = sealExecution({
        projection_id: `omp-session:${dispatch.lease_id}:${dispatch.attempt}`,
        owner_session_id: ownerSessionId,
        task_id: dispatch.task_id,
        attempt_id: dispatch.attempt_id,
        lease_id: dispatch.lease_id,
        worker_id: dispatch.worker_id,
        backend_id: dispatch.backend_id,
        execution_mode: mode,
        phase: dispatch.lease_state === "draining" ? "parked" : "queued",
        revision: 1,
        permit_held: false,
        queued_at: this.clock.now(),
        admitted_at: null,
        parked_at: dispatch.lease_state === "draining" ? this.clock.now() : null,
        settled_at: null,
        promoted_at: null,
        cancellation_reason: dispatch.lease_state === "draining" ? "rehydrated draining lease" : "",
        typed_yield: null,
        projection_only: true,
        canonical_owner: "python.WorkerPoolStore",
      });
      this.executions.set(dispatch.task_id, {
        projection,
        release: null,
        pendingPermit: null,
        controller: new AbortController(),
        parentAbort: null,
        parentSignal: null,
      });
      this.session(ownerSessionId, dispatch.concurrency_limit);
    }
    return this.list();
  }

  snapshot(): OmpSessionRuntimeSnapshot {
    const executions = this.list();
    const sessions = [...this.sessions.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([ownerSessionId, session]) => {
        const projections = executions.filter((item) => item.owner_session_id === ownerSessionId);
        return {
          owner_session_id: ownerSessionId,
          concurrency_limit: session.limit,
          active: session.semaphore.active,
          pending: session.semaphore.pending,
          foreground_task_ids: projections.filter((item) => item.phase === "foreground").map((item) => item.task_id),
          background_task_ids: projections.filter((item) => item.phase === "background").map((item) => item.task_id),
          parked_task_ids: projections.filter((item) => item.phase === "parked").map((item) => item.task_id),
        } satisfies OmpSessionStateProjection;
      });
    const unsigned = {
      version: "zyra.omp-session-admission/v1" as const,
      projection_only: true as const,
      canonical_owner: "python.WorkerPoolStore" as const,
      executions,
      sessions,
      process_epoch: this.processEpoch,
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  private session(ownerSessionId: string, requestedLimit: number): SessionAdmission {
    const limit = Math.max(1, Math.min(128, requestedLimit));
    const prior = this.sessions.get(ownerSessionId);
    if (prior) {
      // Every projection is checksummed, but a task-local value must not inflate a
      // shared owner-session policy established by another active task.
      prior.limit = Math.min(prior.limit, limit);
      prior.semaphore.resize(prior.limit);
      return prior;
    }
    const created = { limit, semaphore: new OmpAbortSafeSemaphore(limit) };
    this.sessions.set(ownerSessionId, created);
    return created;
  }

  private permitFor(taskId: string, live: LiveExecution): OmpSessionPermit {
    let released = false;
    const ownedRelease = live.release;
    return {
      taskId,
      ownerSessionId: live.projection.owner_session_id,
      signal: live.controller.signal,
      release: () => {
        if (released) return;
        released = true;
        // A permit returned before park/revive must not release the newer
        // semaphore generation.  Release only the exact token this handle owns.
        if (!ownedRelease || live.release !== ownedRelease) return;
        ownedRelease();
        live.release = null;
        if (!terminal(live.projection.phase) && live.projection.phase !== "parked")
          live.projection = reviseExecution(live.projection, { permit_held: false });
      },
    };
  }

  private requireLive(taskId: string): LiveExecution {
    const live = this.executions.get(taskId);
    if (!live)
      throw new E03RuntimeError(
        "omp_session_task_not_found",
        `OMP session projection for task ${taskId} was not found`,
      );
    assertExecution(live.projection);
    return live;
  }

  private detachParentAbort(live: LiveExecution): void {
    if (live.parentSignal && live.parentAbort)
      live.parentSignal.removeEventListener("abort", live.parentAbort);
    live.parentAbort = null;
    live.parentSignal = null;
  }

  private attachParentAbort(live: LiveExecution, signal?: AbortSignal): void {
    if (!signal) return;
    const relay = () => {
      if (!live.controller.signal.aborted) live.controller.abort(signal.reason);
    };
    live.parentAbort = relay;
    live.parentSignal = signal;
    if (signal.aborted) relay();
    else signal.addEventListener("abort", relay, { once: true });
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_OMP_SESSION_RUNTIME_DISABLED === "1")
      throw new E03RuntimeError(
        "omp_session_runtime_disabled",
        "background promotion and park/revive require the OMP session runtime",
      );
  }
}

function sealExecution(
  value: Omit<OmpSessionExecutionProjection, "digest">,
): OmpSessionExecutionProjection {
  return { ...value, digest: digest(value) } as OmpSessionExecutionProjection;
}

function reviseExecution(
  current: OmpSessionExecutionProjection,
  patch: Partial<Omit<OmpSessionExecutionProjection, "projection_id" | "task_id" | "digest">>,
): OmpSessionExecutionProjection {
  assertExecution(current);
  const { digest: _checksum, ...prior } = current;
  return sealExecution({ ...prior, ...patch, revision: current.revision + 1 });
}

function assertExecution(value: OmpSessionExecutionProjection): void {
  const { digest: checksum, ...payload } = value;
  if (checksum !== digest(payload))
    throw new E03RuntimeError(
      "omp_session_projection_checksum",
      `OMP session projection ${value.projection_id} checksum mismatch`,
    );
  if (!value.projection_only || value.canonical_owner !== "python.WorkerPoolStore")
    throw new E03RuntimeError(
      "omp_session_projection_owner",
      "OMP session projection attempted to own canonical physical state",
    );
  if (value.revision < 1 || !value.task_id || !value.attempt_id || !value.lease_id)
    throw new E03RuntimeError(
      "omp_session_projection_identity",
      `OMP session projection ${value.projection_id} has invalid identity or revision`,
    );
  if (value.typed_yield) assertYield(value.typed_yield);
}

function assertYield(value: OmpTypedYieldProjection): void {
  const { digest: checksum, ...payload } = value;
  if (checksum !== digest(payload))
    throw new E03RuntimeError(
      "omp_typed_yield_checksum",
      `OMP typed yield ${value.yield_id} checksum mismatch`,
    );
  if (!value.projection_only || value.canonical_owner !== "python.WorkerPoolIntegrationRepository")
    throw new E03RuntimeError(
      "omp_typed_yield_owner",
      "OMP typed yield is only a projection of the canonical Python receipt",
    );
}

function terminal(phase: OmpSessionPhase): boolean {
  return phase === "completed" || phase === "failed" || phase === "cancelled";
}

function required(value: string, label: string): string {
  const selected = value.trim();
  if (!selected)
    throw new E03RuntimeError("omp_session_identity", `${label} is empty`);
  return selected;
}

function abortError(reason: unknown): Error {
  const error = new Error(
    reason instanceof Error ? reason.message : String(reason || "session admission aborted"),
  );
  error.name = "AbortError";
  return error;
}

function clone<T>(value: T): T {
  return structuredClone(value);
}
