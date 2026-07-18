import { createId, type E03Clock, SystemE03Clock } from "../e03/contracts.ts";
import type { JsonObject, RuntimeRunInput } from "../contracts.ts";
import {
  digest,
  E03RuntimeError,
  isTerminal,
  sealTask,
  type E03AgentDefinition,
  type E03CapabilityScope,
  type E03ContextSnapshot,
  type E03TaskState,
} from "../e03/contracts.ts";
import { TaskExecutor, type TaskExecutionHost } from "../tasks/executor.ts";
import { TaskIdentityRuntime } from "../tasks/identity-runtime.ts";
import { DurableTaskRegistry } from "../tasks/registry.ts";
import { TaskStateMachine } from "../tasks/state-machine.ts";

export interface AgentCreateRequest {
  runId: string;
  sessionId: string;
  parentTaskId: string;
  parentSessionId: string;
  taskId?: string;
  idempotencyKey: string;
  definition: E03AgentDefinition;
  scope: E03CapabilityScope;
  context: E03ContextSnapshot;
  prompt: string;
  executionMode: "foreground" | "background";
  parentLineage?: readonly string[];
}

export class AgentExecutionRuntime {
  private readonly identity = new TaskIdentityRuntime();
  private readonly machine = new TaskStateMachine();
  private readonly executor: TaskExecutor;

  constructor(
    private readonly registry: DurableTaskRegistry,
    host: TaskExecutionHost,
  ) {
    this.executor = new TaskExecutor(registry, this.machine, host);
  }

  async create(request: AgentCreateRequest): Promise<E03TaskState> {
    const existingByIdempotency = this.registry.recoverLostAck(
      request.idempotencyKey,
    );
    if (
      existingByIdempotency?.state &&
      typeof existingByIdempotency.state.task_id === "string"
    )
      return this.registry.require(existingByIdempotency.state.task_id);
    const identity = this.identity.allocate({
      runId: request.runId,
      sessionId: request.sessionId,
      taskId: request.taskId,
      parentTaskId: request.parentTaskId,
      parentSessionId: request.parentSessionId,
      parentLineage: request.parentLineage,
      idempotencyKey: request.idempotencyKey,
    });
    const { checksum: _checksum, ...parentContext } = request.context;
    const unsignedContext = {
      ...parentContext,
      taskId: identity.taskId,
      sessionId: identity.sessionId,
    };
    const context = { ...unsignedContext, checksum: digest(unsignedContext) };
    const task = this.machine.create({
      identity,
      definition: request.definition,
      scope: request.scope,
      context,
      prompt: request.prompt,
      executionMode: request.executionMode,
    });
    const prepared = this.registry.prepare({
      requestId: `create:${identity.taskId}`,
      idempotencyKey: request.idempotencyKey,
      writerId: "typescript.E03AgentControlCoordinator",
      taskId: identity.taskId,
      expectedRevision: 0,
      proposed: task,
      effectKind: "persist",
      effectOperation: "persist_agent_task_create",
      effectPayload: {
        parent_task_id: identity.parentTaskId,
        definition_digest: request.definition.digest,
        scope_digest: request.scope.digest,
      },
    });
    const receipt = await this.registry.recordReceipt(request.idempotencyKey);
    return (await this.registry.commit(request.idempotencyKey, receipt)).state;
  }

  async run(
    taskId: string,
    parentInput: RuntimeRunInput,
    argumentsValue: JsonObject,
    requestId: string,
    idempotencyKey: string,
  ): Promise<E03TaskState> {
    const task = this.registry.require(taskId);
    if (isTerminal(task.status)) return task;
    return this.executor.dispatch({
      requestId,
      idempotencyKey,
      writerId: "typescript.E03AgentControlCoordinator",
      task,
      parentInput,
      argumentsValue,
    });
  }

  async resume(
    taskId: string,
    expectedRevision: number,
    parentInput: RuntimeRunInput,
    argumentsValue: JsonObject,
    requestId: string,
    idempotencyKey: string,
  ): Promise<E03TaskState> {
    const task = this.registry.require(taskId);
    if (task.revision !== expectedRevision)
      throw new E03RuntimeError(
        "stale_revision",
        `resume expected ${expectedRevision}, current ${task.revision}`,
      );
    if (task.status !== "waiting" && task.status !== "failed")
      throw new E03RuntimeError(
        "task_not_resumable",
        `task in ${task.status} cannot resume`,
      );
    const identity = this.identity.nextAttempt(task.identity);
    const resumable = sealTask({
      ...task,
      identity,
      status: "queued",
      revision: task.revision + 1,
      sequence: task.sequence + 1,
      error: "",
      terminalAt: null,
      checksum: "",
    });
    const prepared = this.registry.prepare({
      requestId,
      idempotencyKey,
      writerId: "typescript.E03AgentControlCoordinator",
      taskId,
      expectedRevision,
      proposed: resumable,
      effectKind: "persist",
      effectOperation: "persist_agent_task_resume",
      effectPayload: { attempt: identity.attempt, lease_id: identity.leaseId },
    });
    const receipt = await this.registry.recordReceipt(idempotencyKey);
    const committed = await this.registry.commit(idempotencyKey, receipt);
    return this.executor.dispatch({
      requestId,
      idempotencyKey: `${idempotencyKey}:execute`,
      writerId: "typescript.E03AgentControlCoordinator",
      task: committed.state,
      parentInput,
      argumentsValue,
    });
  }

  async abort(
    taskId: string,
    expectedRevision: number,
    reason: string,
    mode: "cancel" | "kill",
    requestId: string,
    idempotencyKey: string,
  ): Promise<E03TaskState> {
    const task = this.registry.require(taskId);
    const transitioned =
      mode === "kill"
        ? this.machine.kill(task, {
            requestId,
            idempotencyKey,
            writerId: "typescript.E03AgentControlCoordinator",
            expectedRevision,
            reason,
          })
        : this.machine.cancel(task, {
            requestId,
            idempotencyKey,
            writerId: "typescript.E03AgentControlCoordinator",
            expectedRevision,
            reason,
          });
    const prepared = this.registry.prepare({
      requestId,
      idempotencyKey,
      writerId: "typescript.E03AgentControlCoordinator",
      taskId,
      expectedRevision,
      proposed: transitioned.state,
      effectKind: "process",
      effectOperation:
        mode === "kill" ? "kill_child_process" : "cancel_child_process",
      effectPayload: { reason },
    });
    const receipt = await this.registry.recordReceipt(idempotencyKey);
    return (await this.registry.commit(idempotencyKey, receipt)).state;
  }

  wait(taskId: string, timeoutMs?: number): Promise<E03TaskState> {
    return this.executor.wait(taskId, timeoutMs);
  }

  result(taskId: string) {
    return this.executor.result(taskId);
  }
}

export type BackgroundClaimPhase =
  | "available"
  | "claimed"
  | "running"
  | "settled"
  | "abandoned";

export interface BackgroundClaim {
  claimId: string;
  taskId: string;
  runId: string;
  parentTaskId: string;
  parentSessionId: string;
  taskLeaseId: string;
  taskRevision: number;
  attempt: number;
  phase: BackgroundClaimPhase;
  workerId: string;
  claimedAt: string | null;
  heartbeatAt: string | null;
  expiresAt: string | null;
  settledAt: string | null;
  error: string;
  digest: string;
}

export interface BackgroundDrainResult {
  selected: string[];
  completed: string[];
  failed: string[];
  skipped: string[];
  claims: BackgroundClaim[];
}

export class AgentBackgroundSupervisor {
  private readonly claims = new Map<string, BackgroundClaim>();

  constructor(
    private readonly clock: E03Clock = new SystemE03Clock(),
    private readonly claimTtlMs = 60_000,
  ) {
    if (
      !Number.isSafeInteger(claimTtlMs) ||
      claimTtlMs < 1000 ||
      claimTtlMs > 3_600_000
    )
      throw new E03RuntimeError(
        "invalid_background_claim_ttl",
        "background claim TTL must be 1s..1h",
      );
  }

  discover(tasks: readonly E03TaskState[]): BackgroundClaim[] {
    const discovered: BackgroundClaim[] = [];
    for (const task of tasks) {
      if (task.executionMode !== "background" || isTerminal(task.status))
        continue;
      const prior = this.claims.get(task.identity.taskId);
      if (prior) {
        if (prior.taskLeaseId !== task.identity.leaseId)
          this.abandon(prior.taskId, "task_lease_changed");
        else {
          discovered.push(structuredClone(prior));
          continue;
        }
      }
      const claim = sealClaim({
        claimId: `background-claim-${digest({ taskId: task.identity.taskId, leaseId: task.identity.leaseId }).slice(0, 32)}`,
        taskId: task.identity.taskId,
        runId: task.identity.runId,
        parentTaskId: task.identity.parentTaskId,
        parentSessionId: task.identity.parentSessionId,
        taskLeaseId: task.identity.leaseId,
        taskRevision: task.revision,
        attempt: task.identity.attempt,
        phase: "available",
        workerId: "",
        claimedAt: null,
        heartbeatAt: null,
        expiresAt: null,
        settledAt: null,
        error: "",
      });
      this.claims.set(task.identity.taskId, claim);
      discovered.push(structuredClone(claim));
    }
    return discovered.sort(compareClaims);
  }

  claim(task: E03TaskState, workerId: string): BackgroundClaim {
    if (task.executionMode !== "background")
      throw new E03RuntimeError(
        "not_background_task",
        `task ${task.identity.taskId} is foreground`,
      );
    if (isTerminal(task.status))
      throw new E03RuntimeError(
        "terminal_task",
        `cannot claim ${task.status} background task`,
      );
    const current =
      this.claims.get(task.identity.taskId) ?? this.discover([task])[0]!;
    this.assertClaim(current);
    const now = this.clock.now();
    if (
      (current.phase === "claimed" || current.phase === "running") &&
      current.expiresAt &&
      current.expiresAt > now
    ) {
      if (current.workerId === workerId) return structuredClone(current);
      throw new E03RuntimeError(
        "background_task_claimed",
        `task ${task.identity.taskId} is claimed by ${current.workerId}`,
        { expiresAt: current.expiresAt },
      );
    }
    if (
      current.taskLeaseId !== task.identity.leaseId ||
      current.attempt !== task.identity.attempt
    )
      throw new E03RuntimeError(
        "stale_background_claim",
        "background claim uses a stale task attempt",
      );
    const next = sealClaim({
      ...current,
      phase: "claimed",
      workerId: workerId.trim(),
      claimedAt: now,
      heartbeatAt: now,
      expiresAt: new Date(Date.parse(now) + this.claimTtlMs).toISOString(),
      error: "",
    });
    if (!next.workerId)
      throw new E03RuntimeError(
        "invalid_background_worker",
        "background worker id is empty",
      );
    this.claims.set(task.identity.taskId, next);
    return structuredClone(next);
  }

  running(taskId: string, workerId: string): BackgroundClaim {
    const current = this.require(taskId);
    this.assertOwner(current, workerId);
    if (current.phase !== "claimed" && current.phase !== "running")
      throw new E03RuntimeError(
        "background_not_claimed",
        `cannot run claim in ${current.phase}`,
      );
    const now = this.clock.now();
    const next = sealClaim({
      ...current,
      phase: "running",
      heartbeatAt: now,
      expiresAt: new Date(Date.parse(now) + this.claimTtlMs).toISOString(),
    });
    this.claims.set(taskId, next);
    return structuredClone(next);
  }

  heartbeat(taskId: string, workerId: string): BackgroundClaim {
    const current = this.require(taskId);
    this.assertOwner(current, workerId);
    if (current.phase !== "running")
      throw new E03RuntimeError(
        "background_not_running",
        `cannot heartbeat claim in ${current.phase}`,
      );
    const now = this.clock.now();
    const next = sealClaim({
      ...current,
      heartbeatAt: now,
      expiresAt: new Date(Date.parse(now) + this.claimTtlMs).toISOString(),
    });
    this.claims.set(taskId, next);
    return structuredClone(next);
  }

  settle(task: E03TaskState, workerId: string): BackgroundClaim {
    const current = this.require(task.identity.taskId);
    this.assertOwner(current, workerId);
    if (!isTerminal(task.status))
      throw new E03RuntimeError(
        "background_not_terminal",
        "background claim cannot settle before task terminal state",
      );
    if (task.identity.leaseId !== current.taskLeaseId)
      throw new E03RuntimeError(
        "stale_background_claim",
        "settlement uses a stale task lease",
      );
    const next = sealClaim({
      ...current,
      phase: "settled",
      taskRevision: task.revision,
      heartbeatAt: this.clock.now(),
      expiresAt: null,
      settledAt: this.clock.now(),
      error: task.error,
    });
    this.claims.set(task.identity.taskId, next);
    return structuredClone(next);
  }

  abandon(taskId: string, error: string): BackgroundClaim {
    const current = this.require(taskId);
    if (current.phase === "settled" || current.phase === "abandoned")
      return structuredClone(current);
    const next = sealClaim({
      ...current,
      phase: "abandoned",
      expiresAt: null,
      settledAt: this.clock.now(),
      error: error.trim() || "background_claim_abandoned",
    });
    this.claims.set(taskId, next);
    return structuredClone(next);
  }

  recoverExpired(now = this.clock.now()): BackgroundClaim[] {
    const recovered: BackgroundClaim[] = [];
    for (const claim of this.claims.values()) {
      if (
        (claim.phase !== "claimed" && claim.phase !== "running") ||
        !claim.expiresAt ||
        claim.expiresAt > now
      )
        continue;
      const next = sealClaim({
        ...claim,
        phase: "available",
        workerId: "",
        claimedAt: null,
        heartbeatAt: null,
        expiresAt: null,
        error: "claim_expired",
      });
      this.claims.set(claim.taskId, next);
      recovered.push(structuredClone(next));
    }
    return recovered.sort(compareClaims);
  }

  async drain(
    tasks: readonly E03TaskState[],
    input: {
      workerId: string;
      maximumConcurrency: number;
      run: (task: E03TaskState) => Promise<E03TaskState>;
    },
  ): Promise<BackgroundDrainResult> {
    if (
      !Number.isSafeInteger(input.maximumConcurrency) ||
      input.maximumConcurrency < 1 ||
      input.maximumConcurrency > 128
    )
      throw new E03RuntimeError(
        "invalid_background_concurrency",
        "background concurrency must be 1..128",
      );
    this.recoverExpired();
    const candidates = this.discover(tasks).filter(
      (claim) => claim.phase === "available",
    );
    const selected: string[] = [];
    const completed: string[] = [];
    const failed: string[] = [];
    const skipped: string[] = [];
    let cursor = 0;
    const worker = async (): Promise<void> => {
      while (cursor < candidates.length) {
        const claim = candidates[cursor++]!;
        const task = tasks.find(
          (value) => value.identity.taskId === claim.taskId,
        );
        if (!task || isTerminal(task.status)) {
          skipped.push(claim.taskId);
          continue;
        }
        selected.push(task.identity.taskId);
        try {
          this.claim(task, input.workerId);
          this.running(task.identity.taskId, input.workerId);
          const settled = await input.run(task);
          this.settle(settled, input.workerId);
          if (settled.status === "completed")
            completed.push(settled.identity.taskId);
          else failed.push(settled.identity.taskId);
        } catch (error) {
          this.abandon(
            task.identity.taskId,
            error instanceof Error ? error.message : String(error),
          );
          failed.push(task.identity.taskId);
        }
      }
    };
    await Promise.all(
      Array.from(
        { length: Math.min(input.maximumConcurrency, candidates.length || 1) },
        worker,
      ),
    );
    return { selected, completed, failed, skipped, claims: this.snapshot() };
  }

  restore(claims: readonly BackgroundClaim[]): void {
    const next = new Map<string, BackgroundClaim>();
    for (const claim of claims) {
      this.assertClaim(claim);
      if (next.has(claim.taskId))
        throw new E03RuntimeError(
          "duplicate_background_claim",
          `duplicate claim for ${claim.taskId}`,
        );
      next.set(claim.taskId, structuredClone(claim));
    }
    this.claims.clear();
    for (const [taskId, claim] of next) this.claims.set(taskId, claim);
  }

  snapshot(): BackgroundClaim[] {
    return [...this.claims.values()]
      .sort(compareClaims)
      .map((claim) => structuredClone(claim));
  }

  private require(taskId: string): BackgroundClaim {
    const claim = this.claims.get(taskId);
    if (!claim)
      throw new E03RuntimeError(
        "unknown_background_claim",
        `unknown background claim for ${taskId}`,
      );
    this.assertClaim(claim);
    return claim;
  }

  private assertOwner(claim: BackgroundClaim, workerId: string): void {
    if (claim.workerId !== workerId)
      throw new E03RuntimeError(
        "background_claim_owner_mismatch",
        `claim belongs to ${claim.workerId}`,
      );
    if (claim.expiresAt && claim.expiresAt <= this.clock.now())
      throw new E03RuntimeError(
        "background_claim_expired",
        "background claim expired",
      );
  }

  private assertClaim(claim: BackgroundClaim): void {
    const { digest: checksum, ...payload } = claim;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "background_claim_digest",
        `claim ${claim.claimId} digest is invalid`,
      );
    if (
      !claim.claimId ||
      !claim.taskId ||
      !claim.runId ||
      !claim.parentTaskId ||
      !claim.parentSessionId ||
      !claim.taskLeaseId ||
      claim.attempt < 1
    )
      throw new E03RuntimeError(
        "invalid_background_claim",
        "background claim identity is incomplete",
      );
    if (
      (claim.phase === "claimed" || claim.phase === "running") &&
      (!claim.workerId || !claim.claimedAt || !claim.expiresAt)
    )
      throw new E03RuntimeError(
        "invalid_background_claim_phase",
        `${claim.phase} claim lacks worker/lease timestamps`,
      );
    if (
      (claim.phase === "settled" || claim.phase === "abandoned") &&
      !claim.settledAt
    )
      throw new E03RuntimeError(
        "invalid_background_claim_phase",
        `${claim.phase} claim lacks settlement timestamp`,
      );
  }
}

function sealClaim(
  value: Omit<BackgroundClaim, "digest"> | BackgroundClaim,
): BackgroundClaim {
  const { digest: _digest, ...payload } = value as BackgroundClaim;
  return { ...payload, digest: digest(payload) };
}

function compareClaims(left: BackgroundClaim, right: BackgroundClaim): number {
  return (
    left.parentTaskId.localeCompare(right.parentTaskId) ||
    left.taskId.localeCompare(right.taskId) ||
    left.attempt - right.attempt
  );
}

export type AgentRunLeaseState =
  | "offered"
  | "claimed"
  | "running"
  | "waiting"
  | "releasing"
  | "released"
  | "expired"
  | "revoked";

export interface AgentRunLease {
  leaseId: string;
  taskId: string;
  sessionId: string;
  attempt: number;
  holderId: string | null;
  executionMode: "foreground" | "background";
  state: AgentRunLeaseState;
  offeredAt: string;
  claimedAt: string | null;
  startedAt: string | null;
  lastHeartbeatAt: string | null;
  releaseRequestedAt: string | null;
  releasedAt: string | null;
  expiresAt: string;
  heartbeatIntervalMs: number;
  missedHeartbeatLimit: number;
  revision: number;
  digest: string;
}

export interface AgentRunHeartbeat {
  heartbeatId: string;
  leaseId: string;
  taskId: string;
  holderId: string;
  leaseRevision: number;
  phase:
    | "starting"
    | "reasoning"
    | "tool"
    | "observing"
    | "waiting"
    | "settling";
  progress: number | null;
  detail: string;
  emittedAt: string;
  digest: string;
}

export interface AgentRunLeaseProjection {
  taskId: string;
  activeLeaseId: string | null;
  activeHolderId: string | null;
  attempt: number;
  state: AgentRunLeaseState | "unassigned";
  heartbeatCount: number;
  latestHeartbeatAt: string | null;
  expiredLeaseIds: string[];
  releasedLeaseIds: string[];
  digest: string;
}

function assertAgentRunLease(lease: AgentRunLease): void {
  const { digest: checksum, ...payload } = lease;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "agent_run_lease_digest",
      `agent run lease ${lease.leaseId} digest is invalid`,
    );
  if (!lease.leaseId || !lease.taskId || !lease.sessionId)
    throw new E03RuntimeError(
      "agent_run_lease_identity",
      "agent run lease identity is incomplete",
    );
  if (!Number.isSafeInteger(lease.attempt) || lease.attempt < 1)
    throw new E03RuntimeError(
      "agent_run_lease_attempt",
      "agent run lease attempt is invalid",
    );
  if (
    !Number.isSafeInteger(lease.heartbeatIntervalMs) ||
    lease.heartbeatIntervalMs < 100
  )
    throw new E03RuntimeError(
      "agent_run_lease_heartbeat_interval",
      "agent run lease heartbeat interval is invalid",
    );
  if (
    !Number.isSafeInteger(lease.missedHeartbeatLimit) ||
    lease.missedHeartbeatLimit < 1
  )
    throw new E03RuntimeError(
      "agent_run_lease_heartbeat_limit",
      "agent run lease missed heartbeat limit is invalid",
    );
  if (!Number.isSafeInteger(lease.revision) || lease.revision < 1)
    throw new E03RuntimeError(
      "agent_run_lease_revision",
      "agent run lease revision is invalid",
    );
  if (Date.parse(lease.expiresAt) <= Date.parse(lease.offeredAt))
    throw new E03RuntimeError(
      "agent_run_lease_expiry",
      "agent run lease expiry must follow offer time",
    );
  if (lease.state === "offered" && lease.holderId !== null)
    throw new E03RuntimeError(
      "agent_run_lease_holder",
      "offered agent run lease cannot have a holder",
    );
  if (lease.state !== "offered" && lease.state !== "expired" && !lease.holderId)
    throw new E03RuntimeError(
      "agent_run_lease_holder",
      "claimed agent run lease requires a holder",
    );
}

function assertAgentRunHeartbeat(heartbeat: AgentRunHeartbeat): void {
  const { digest: checksum, ...payload } = heartbeat;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "agent_run_heartbeat_digest",
      `agent run heartbeat ${heartbeat.heartbeatId} digest is invalid`,
    );
  if (
    !heartbeat.heartbeatId ||
    !heartbeat.leaseId ||
    !heartbeat.taskId ||
    !heartbeat.holderId
  )
    throw new E03RuntimeError(
      "agent_run_heartbeat_identity",
      "agent run heartbeat identity is incomplete",
    );
  if (
    !Number.isSafeInteger(heartbeat.leaseRevision) ||
    heartbeat.leaseRevision < 1
  )
    throw new E03RuntimeError(
      "agent_run_heartbeat_revision",
      "agent run heartbeat lease revision is invalid",
    );
  if (
    heartbeat.progress !== null &&
    (!Number.isFinite(heartbeat.progress) ||
      heartbeat.progress < 0 ||
      heartbeat.progress > 1)
  )
    throw new E03RuntimeError(
      "agent_run_heartbeat_progress",
      "agent run heartbeat progress is invalid",
    );
}

export class AgentRunLeaseRuntime {
  private leases = new Map<string, AgentRunLease>();
  private heartbeats = new Map<string, AgentRunHeartbeat>();
  private byTask = new Map<string, string[]>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  offer(input: {
    task: E03TaskState;
    executionMode: "foreground" | "background";
    leaseDurationMs: number;
    heartbeatIntervalMs: number;
    missedHeartbeatLimit?: number;
  }): AgentRunLease {
    if (isTerminal(input.task.status))
      throw new E03RuntimeError(
        "agent_run_lease_terminal_task",
        `cannot offer a run lease for terminal task ${input.task.identity.taskId}`,
      );
    if (
      !Number.isSafeInteger(input.leaseDurationMs) ||
      input.leaseDurationMs < 1_000
    )
      throw new E03RuntimeError(
        "agent_run_lease_duration",
        "agent run lease duration is invalid",
      );
    const existing = this.activeForTask(input.task.identity.taskId);
    if (existing)
      throw new E03RuntimeError(
        "agent_run_lease_active",
        `task ${input.task.identity.taskId} already has active lease ${existing.leaseId}`,
      );
    const offeredAt = this.clock.now();
    const payload = {
      leaseId: createId("agent-run-lease"),
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      attempt: input.task.identity.attempt,
      holderId: null,
      executionMode: input.executionMode,
      state: "offered" as const,
      offeredAt,
      claimedAt: null,
      startedAt: null,
      lastHeartbeatAt: null,
      releaseRequestedAt: null,
      releasedAt: null,
      expiresAt: new Date(
        Date.parse(offeredAt) + input.leaseDurationMs,
      ).toISOString(),
      heartbeatIntervalMs: input.heartbeatIntervalMs,
      missedHeartbeatLimit: input.missedHeartbeatLimit ?? 3,
      revision: 1,
    };
    const lease = { ...payload, digest: digest(payload) };
    assertAgentRunLease(lease);
    this.leases.set(lease.leaseId, lease);
    const taskLeases = this.byTask.get(lease.taskId) ?? [];
    this.byTask.set(lease.taskId, [...taskLeases, lease.leaseId]);
    return structuredClone(lease);
  }

  claim(
    leaseId: string,
    holderId: string,
    expectedRevision: number,
  ): AgentRunLease {
    const lease = this.requireLease(leaseId);
    this.assertRevision(lease, expectedRevision);
    if (lease.state !== "offered")
      throw new E03RuntimeError(
        "agent_run_lease_claim_state",
        `agent run lease ${leaseId} is ${lease.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(lease.expiresAt))
      return this.transition(lease, { state: "expired" });
    if (!holderId.trim())
      throw new E03RuntimeError(
        "agent_run_lease_holder",
        "agent run lease holder is required",
      );
    return this.transition(lease, {
      holderId: holderId.trim(),
      state: "claimed",
      claimedAt: now,
    });
  }

  start(
    leaseId: string,
    holderId: string,
    expectedRevision: number,
  ): AgentRunLease {
    const lease = this.requireHolder(leaseId, holderId);
    this.assertRevision(lease, expectedRevision);
    if (lease.state !== "claimed")
      throw new E03RuntimeError(
        "agent_run_lease_start_state",
        `agent run lease ${leaseId} is ${lease.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(lease.expiresAt))
      return this.transition(lease, { state: "expired" });
    return this.transition(lease, {
      state: "running",
      startedAt: now,
      lastHeartbeatAt: now,
    });
  }

  heartbeat(input: {
    leaseId: string;
    holderId: string;
    expectedRevision: number;
    phase: AgentRunHeartbeat["phase"];
    progress?: number | null;
    detail?: string;
  }): AgentRunHeartbeat {
    let lease = this.requireHolder(input.leaseId, input.holderId);
    this.assertRevision(lease, input.expectedRevision);
    if (lease.state !== "running" && lease.state !== "waiting")
      throw new E03RuntimeError(
        "agent_run_heartbeat_state",
        `agent run lease ${lease.leaseId} is ${lease.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(lease.expiresAt)) {
      this.transition(lease, { state: "expired" });
      throw new E03RuntimeError(
        "agent_run_lease_expired",
        `agent run lease ${lease.leaseId} expired before heartbeat`,
      );
    }
    lease = this.transition(lease, {
      state: input.phase === "waiting" ? "waiting" : "running",
      lastHeartbeatAt: now,
    });
    const payload = {
      heartbeatId: createId("agent-run-heartbeat"),
      leaseId: lease.leaseId,
      taskId: lease.taskId,
      holderId: input.holderId,
      leaseRevision: lease.revision,
      phase: input.phase,
      progress: input.progress ?? null,
      detail: input.detail?.trim() ?? "",
      emittedAt: now,
    };
    const heartbeat = { ...payload, digest: digest(payload) };
    assertAgentRunHeartbeat(heartbeat);
    this.heartbeats.set(heartbeat.heartbeatId, heartbeat);
    return structuredClone(heartbeat);
  }

  requestRelease(
    leaseId: string,
    holderId: string,
    expectedRevision: number,
  ): AgentRunLease {
    const lease = this.requireHolder(leaseId, holderId);
    this.assertRevision(lease, expectedRevision);
    if (lease.state !== "running" && lease.state !== "waiting")
      throw new E03RuntimeError(
        "agent_run_lease_release_state",
        `agent run lease ${leaseId} cannot release from ${lease.state}`,
      );
    return this.transition(lease, {
      state: "releasing",
      releaseRequestedAt: this.clock.now(),
    });
  }

  release(
    leaseId: string,
    holderId: string,
    expectedRevision: number,
  ): AgentRunLease {
    const lease = this.requireHolder(leaseId, holderId);
    this.assertRevision(lease, expectedRevision);
    if (lease.state !== "releasing")
      throw new E03RuntimeError(
        "agent_run_lease_release_state",
        `agent run lease ${leaseId} is not releasing`,
      );
    return this.transition(lease, {
      state: "released",
      releasedAt: this.clock.now(),
    });
  }

  revoke(leaseId: string, reason: string): AgentRunLease {
    const lease = this.requireLease(leaseId);
    if (lease.state === "released" || lease.state === "expired")
      throw new E03RuntimeError(
        "agent_run_lease_revoke_state",
        `agent run lease ${leaseId} is already ${lease.state}`,
      );
    if (!reason.trim())
      throw new E03RuntimeError(
        "agent_run_lease_revoke_reason",
        "agent run lease revocation reason is required",
      );
    return this.transition(lease, {
      state: "revoked",
      releaseRequestedAt: lease.releaseRequestedAt ?? this.clock.now(),
      releasedAt: this.clock.now(),
    });
  }

  sweep(now = this.clock.now()): AgentRunLease[] {
    const expired: AgentRunLease[] = [];
    for (const current of this.leases.values()) {
      if (["released", "expired", "revoked"].includes(current.state)) continue;
      const heartbeatDeadline = current.lastHeartbeatAt
        ? Date.parse(current.lastHeartbeatAt) +
          current.heartbeatIntervalMs * current.missedHeartbeatLimit
        : Number.POSITIVE_INFINITY;
      if (
        Date.parse(now) >= Date.parse(current.expiresAt) ||
        Date.parse(now) >= heartbeatDeadline
      )
        expired.push(this.transition(current, { state: "expired" }));
    }
    return expired;
  }

  project(taskId: string): AgentRunLeaseProjection {
    const leases = (this.byTask.get(taskId) ?? []).map((leaseId) =>
      this.requireLease(leaseId),
    );
    const active = [...leases]
      .reverse()
      .find(
        (lease) => !["released", "expired", "revoked"].includes(lease.state),
      );
    const heartbeats = [...this.heartbeats.values()].filter(
      (heartbeat) => heartbeat.taskId === taskId,
    );
    const latestHeartbeat = [...heartbeats].sort((left, right) =>
      right.emittedAt.localeCompare(left.emittedAt),
    )[0];
    const payload = {
      taskId,
      activeLeaseId: active?.leaseId ?? null,
      activeHolderId: active?.holderId ?? null,
      attempt: leases.reduce(
        (maximum, lease) => Math.max(maximum, lease.attempt),
        0,
      ),
      state: active?.state ?? ("unassigned" as const),
      heartbeatCount: heartbeats.length,
      latestHeartbeatAt: latestHeartbeat?.emittedAt ?? null,
      expiredLeaseIds: leases
        .filter((lease) => lease.state === "expired")
        .map((lease) => lease.leaseId),
      releasedLeaseIds: leases
        .filter((lease) => lease.state === "released")
        .map((lease) => lease.leaseId),
    };
    return { ...payload, digest: digest(payload) };
  }

  snapshot(): { leases: AgentRunLease[]; heartbeats: AgentRunHeartbeat[] } {
    return {
      leases: [...this.leases.values()].map((lease) => structuredClone(lease)),
      heartbeats: [...this.heartbeats.values()].map((heartbeat) =>
        structuredClone(heartbeat),
      ),
    };
  }

  restore(input: {
    leases: readonly AgentRunLease[];
    heartbeats: readonly AgentRunHeartbeat[];
  }): void {
    const leases = new Map<string, AgentRunLease>();
    const heartbeats = new Map<string, AgentRunHeartbeat>();
    const byTask = new Map<string, string[]>();
    for (const raw of input.leases) {
      const lease = structuredClone(raw);
      assertAgentRunLease(lease);
      if (leases.has(lease.leaseId))
        throw new E03RuntimeError(
          "agent_run_lease_restore_duplicate",
          `duplicate agent run lease ${lease.leaseId}`,
        );
      leases.set(lease.leaseId, lease);
      byTask.set(lease.taskId, [
        ...(byTask.get(lease.taskId) ?? []),
        lease.leaseId,
      ]);
    }
    for (const taskLeaseIds of byTask.values()) {
      const active = taskLeaseIds
        .map((leaseId) => leases.get(leaseId)!)
        .filter(
          (lease) => !["released", "expired", "revoked"].includes(lease.state),
        );
      if (active.length > 1)
        throw new E03RuntimeError(
          "agent_run_lease_restore_multiple_active",
          `task ${active[0]!.taskId} has multiple active run leases`,
        );
    }
    for (const raw of input.heartbeats) {
      const heartbeat = structuredClone(raw);
      assertAgentRunHeartbeat(heartbeat);
      const lease = leases.get(heartbeat.leaseId);
      if (!lease)
        throw new E03RuntimeError(
          "agent_run_heartbeat_restore_orphan",
          `agent run heartbeat ${heartbeat.heartbeatId} has no lease`,
        );
      if (
        heartbeat.taskId !== lease.taskId ||
        heartbeat.holderId !== lease.holderId
      )
        throw new E03RuntimeError(
          "agent_run_heartbeat_restore_custody",
          `agent run heartbeat ${heartbeat.heartbeatId} custody is invalid`,
        );
      if (heartbeats.has(heartbeat.heartbeatId))
        throw new E03RuntimeError(
          "agent_run_heartbeat_restore_duplicate",
          `duplicate agent run heartbeat ${heartbeat.heartbeatId}`,
        );
      heartbeats.set(heartbeat.heartbeatId, heartbeat);
    }
    this.leases = leases;
    this.heartbeats = heartbeats;
    this.byTask = byTask;
  }

  private activeForTask(taskId: string): AgentRunLease | null {
    const leaseIds = this.byTask.get(taskId) ?? [];
    for (let index = leaseIds.length - 1; index >= 0; index -= 1) {
      const lease = this.requireLease(leaseIds[index]!);
      if (!["released", "expired", "revoked"].includes(lease.state))
        return lease;
    }
    return null;
  }

  private requireHolder(leaseId: string, holderId: string): AgentRunLease {
    const lease = this.requireLease(leaseId);
    if (lease.holderId !== holderId)
      throw new E03RuntimeError(
        "agent_run_lease_holder",
        `agent run lease ${leaseId} belongs to another holder`,
      );
    return lease;
  }

  private requireLease(leaseId: string): AgentRunLease {
    const lease = this.leases.get(leaseId);
    if (!lease)
      throw new E03RuntimeError(
        "agent_run_lease_missing",
        `agent run lease ${leaseId} does not exist`,
      );
    assertAgentRunLease(lease);
    return lease;
  }

  private assertRevision(lease: AgentRunLease, expectedRevision: number): void {
    if (lease.revision !== expectedRevision)
      throw new E03RuntimeError(
        "agent_run_lease_stale_revision",
        `agent run lease ${lease.leaseId} revision ${lease.revision} does not match ${expectedRevision}`,
      );
  }

  private transition(
    lease: AgentRunLease,
    patch: Partial<Omit<AgentRunLease, "leaseId" | "revision" | "digest">>,
  ): AgentRunLease {
    const { digest: _, ...prior } = lease;
    const payload = {
      ...prior,
      ...patch,
      leaseId: lease.leaseId,
      revision: lease.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertAgentRunLease(next);
    this.leases.set(next.leaseId, next);
    return structuredClone(next);
  }
}

export type AgentExecutionIncidentKind =
  | "heartbeat_timeout"
  | "deadline_exceeded"
  | "budget_exhausted"
  | "host_failure"
  | "protocol_violation"
  | "output_rejected";
export interface AgentExecutionIncident {
  incidentId: string;
  taskId: string;
  runId: string;
  attempt: number;
  kind: AgentExecutionIncidentKind;
  severity: "warning" | "recoverable" | "fatal";
  state: "open" | "triaged" | "recovering" | "resolved" | "escalated";
  evidence: JsonObject;
  summary: string;
  recoveryPlanId: string | null;
  detectedAt: string;
  triagedAt: string | null;
  resolvedAt: string | null;
  revision: number;
  digest: string;
}
export interface AgentExecutionRecoveryPlan {
  planId: string;
  incidentId: string;
  taskId: string;
  strategy:
    | "retry"
    | "resume_checkpoint"
    | "replace_host"
    | "reduce_scope"
    | "fail";
  state:
    | "planned"
    | "authorized"
    | "executing"
    | "succeeded"
    | "failed"
    | "cancelled";
  checkpointId: string | null;
  targetHostId: string | null;
  maximumAttempts: number;
  attemptsStarted: number;
  backoffMs: number;
  authorizationId: string | null;
  outcome: string | null;
  createdAt: string;
  authorizedAt: string | null;
  startedAt: string | null;
  completedAt: string | null;
  revision: number;
  digest: string;
}
function assertExecutionIncident(value: AgentExecutionIncident): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "agent_execution_incident_digest",
      `agent execution incident ${value.incidentId} is corrupt`,
    );
  if (
    !value.incidentId ||
    !value.taskId ||
    !value.runId ||
    !value.summary.trim()
  )
    throw new E03RuntimeError(
      "agent_execution_incident_identity",
      "agent execution incident identity is required",
    );
  if (
    !Number.isSafeInteger(value.attempt) ||
    value.attempt < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "agent_execution_incident_revision",
      `agent execution incident ${value.incidentId} is invalid`,
    );
  if (value.state === "triaged" && value.triagedAt === null)
    throw new E03RuntimeError(
      "agent_execution_incident_triage_time",
      `triaged incident ${value.incidentId} lacks time`,
    );
  if (value.state === "resolved" && value.resolvedAt === null)
    throw new E03RuntimeError(
      "agent_execution_incident_resolution_time",
      `resolved incident ${value.incidentId} lacks time`,
    );
}
function assertExecutionRecovery(value: AgentExecutionRecoveryPlan): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "agent_execution_recovery_digest",
      `agent execution recovery ${value.planId} is corrupt`,
    );
  if (!value.planId || !value.incidentId || !value.taskId)
    throw new E03RuntimeError(
      "agent_execution_recovery_identity",
      "agent execution recovery identity is required",
    );
  if (
    !Number.isSafeInteger(value.maximumAttempts) ||
    value.maximumAttempts < 1 ||
    !Number.isSafeInteger(value.attemptsStarted) ||
    value.attemptsStarted < 0 ||
    value.attemptsStarted > value.maximumAttempts ||
    !Number.isSafeInteger(value.backoffMs) ||
    value.backoffMs < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "agent_execution_recovery_counters",
      `agent execution recovery ${value.planId} counters are invalid`,
    );
  if (value.state === "authorized" && value.authorizedAt === null)
    throw new E03RuntimeError(
      "agent_execution_recovery_authorization_time",
      `authorized recovery ${value.planId} lacks time`,
    );
  if (
    (value.state === "succeeded" ||
      value.state === "failed" ||
      value.state === "cancelled") &&
    value.completedAt === null
  )
    throw new E03RuntimeError(
      "agent_execution_recovery_completion_time",
      `completed recovery ${value.planId} lacks time`,
    );
}
export class AgentExecutionWatchdogRuntime {
  private incidents = new Map<string, AgentExecutionIncident>();
  private plans = new Map<string, AgentExecutionRecoveryPlan>();
  private openByTaskKind = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  detect(input: {
    task: E03TaskState;
    kind: AgentExecutionIncidentKind;
    severity: AgentExecutionIncident["severity"];
    summary: string;
    evidence?: JsonObject;
  }): AgentExecutionIncident {
    if (isTerminal(input.task.status))
      throw new E03RuntimeError(
        "agent_execution_incident_terminal",
        `terminal task ${input.task.identity.taskId} cannot open an incident`,
      );
    if (!input.summary.trim())
      throw new E03RuntimeError(
        "agent_execution_incident_summary",
        "agent execution incident summary is required",
      );
    const key = `${input.task.identity.taskId}:${input.kind}`;
    const existingId = this.openByTaskKind.get(key);
    if (existingId) {
      const existing = this.requireIncident(existingId);
      if (existing.state !== "resolved") return structuredClone(existing);
    }
    const payload = {
      incidentId: createId("agent-execution-incident"),
      taskId: input.task.identity.taskId,
      runId: input.task.identity.runId,
      attempt: input.task.identity.attempt,
      kind: input.kind,
      severity: input.severity,
      state: "open" as const,
      evidence: structuredClone(input.evidence ?? {}),
      summary: input.summary.trim(),
      recoveryPlanId: null,
      detectedAt: this.clock.now(),
      triagedAt: null,
      resolvedAt: null,
      revision: 1,
    };
    const incident = { ...payload, digest: digest(payload) };
    assertExecutionIncident(incident);
    this.incidents.set(incident.incidentId, incident);
    this.openByTaskKind.set(key, incident.incidentId);
    return structuredClone(incident);
  }
  inspectLease(
    task: E03TaskState,
    lease: AgentRunLease,
    now = this.clock.now(),
  ): AgentExecutionIncident | null {
    if (lease.taskId !== task.identity.taskId)
      throw new E03RuntimeError(
        "agent_execution_watchdog_lease_custody",
        "agent run lease belongs to another task",
      );
    if (
      lease.state !== "claimed" &&
      lease.state !== "running" &&
      lease.state !== "waiting"
    )
      return null;
    const timestamp = Date.parse(now);
    if (Number.isNaN(timestamp))
      throw new E03RuntimeError(
        "agent_execution_watchdog_time",
        "agent execution watchdog time is invalid",
      );
    if (Date.parse(lease.expiresAt) <= timestamp)
      return this.detect({
        task,
        kind: "heartbeat_timeout",
        severity: "recoverable",
        summary: `agent run lease ${lease.leaseId} expired`,
        evidence: {
          lease_id: lease.leaseId,
          expires_at: lease.expiresAt,
          observed_at: now,
        },
      });
    if (
      task.definition.budget.deadlineAt &&
      Date.parse(task.definition.budget.deadlineAt) <= timestamp
    )
      return this.detect({
        task,
        kind: "deadline_exceeded",
        severity: "fatal",
        summary: `task ${task.identity.taskId} exceeded its deadline`,
        evidence: {
          deadline_at: task.definition.budget.deadlineAt,
          observed_at: now,
        },
      });
    return null;
  }
  triage(
    incidentId: string,
    expectedRevision: number,
    strategy: AgentExecutionRecoveryPlan["strategy"],
    input?: {
      checkpointId?: string | null;
      targetHostId?: string | null;
      maximumAttempts?: number;
      backoffMs?: number;
    },
  ): { incident: AgentExecutionIncident; plan: AgentExecutionRecoveryPlan } {
    const incident = this.requireIncident(incidentId);
    this.assertIncidentRevision(incident, expectedRevision);
    if (incident.state !== "open")
      throw new E03RuntimeError(
        "agent_execution_incident_triage_state",
        `agent execution incident ${incidentId} is ${incident.state}`,
      );
    if (
      incident.severity === "fatal" &&
      strategy !== "fail" &&
      strategy !== "replace_host"
    )
      throw new E03RuntimeError(
        "agent_execution_incident_fatal_strategy",
        `fatal incident ${incidentId} requires fail or host replacement`,
      );
    if (strategy === "resume_checkpoint" && !input?.checkpointId)
      throw new E03RuntimeError(
        "agent_execution_recovery_checkpoint",
        "checkpoint recovery requires checkpoint id",
      );
    if (strategy === "replace_host" && !input?.targetHostId)
      throw new E03RuntimeError(
        "agent_execution_recovery_host",
        "host replacement requires target host id",
      );
    const payload = {
      planId: createId("agent-execution-recovery"),
      incidentId: incident.incidentId,
      taskId: incident.taskId,
      strategy,
      state: "planned" as const,
      checkpointId: input?.checkpointId ?? null,
      targetHostId: input?.targetHostId ?? null,
      maximumAttempts: input?.maximumAttempts ?? 1,
      attemptsStarted: 0,
      backoffMs: input?.backoffMs ?? 0,
      authorizationId: null,
      outcome: null,
      createdAt: this.clock.now(),
      authorizedAt: null,
      startedAt: null,
      completedAt: null,
      revision: 1,
    };
    const plan = { ...payload, digest: digest(payload) };
    assertExecutionRecovery(plan);
    this.plans.set(plan.planId, plan);
    const nextIncident = this.transitionIncident(incident, {
      state: "triaged",
      recoveryPlanId: plan.planId,
      triagedAt: this.clock.now(),
    });
    return { incident: nextIncident, plan: structuredClone(plan) };
  }
  authorize(
    planId: string,
    expectedRevision: number,
    authorizationId: string,
  ): AgentExecutionRecoveryPlan {
    const plan = this.requirePlan(planId);
    this.assertPlanRevision(plan, expectedRevision);
    if (plan.state !== "planned")
      throw new E03RuntimeError(
        "agent_execution_recovery_authorize_state",
        `agent execution recovery ${planId} is ${plan.state}`,
      );
    if (!authorizationId.trim())
      throw new E03RuntimeError(
        "agent_execution_recovery_authorization",
        "agent execution recovery authorization is required",
      );
    return this.transitionPlan(plan, {
      state: "authorized",
      authorizationId: authorizationId.trim(),
      authorizedAt: this.clock.now(),
    });
  }
  start(
    planId: string,
    expectedRevision: number,
  ): { plan: AgentExecutionRecoveryPlan; incident: AgentExecutionIncident } {
    const plan = this.requirePlan(planId);
    this.assertPlanRevision(plan, expectedRevision);
    if (plan.state !== "authorized" && plan.state !== "executing")
      throw new E03RuntimeError(
        "agent_execution_recovery_start_state",
        `agent execution recovery ${planId} is ${plan.state}`,
      );
    if (plan.attemptsStarted >= plan.maximumAttempts)
      throw new E03RuntimeError(
        "agent_execution_recovery_attempts",
        `agent execution recovery ${planId} exhausted attempts`,
      );
    const nextPlan = this.transitionPlan(plan, {
      state: "executing",
      attemptsStarted: plan.attemptsStarted + 1,
      startedAt: plan.startedAt ?? this.clock.now(),
    });
    const incident = this.requireIncident(plan.incidentId);
    const nextIncident =
      incident.state === "recovering"
        ? structuredClone(incident)
        : this.transitionIncident(incident, { state: "recovering" });
    return { plan: nextPlan, incident: nextIncident };
  }
  complete(
    planId: string,
    expectedRevision: number,
    outcome: { ok: boolean; summary: string; retryable?: boolean },
  ): { plan: AgentExecutionRecoveryPlan; incident: AgentExecutionIncident } {
    const plan = this.requirePlan(planId);
    this.assertPlanRevision(plan, expectedRevision);
    if (plan.state !== "executing")
      throw new E03RuntimeError(
        "agent_execution_recovery_complete_state",
        `agent execution recovery ${planId} is ${plan.state}`,
      );
    if (!outcome.summary.trim())
      throw new E03RuntimeError(
        "agent_execution_recovery_outcome",
        "agent execution recovery outcome is required",
      );
    if (
      !outcome.ok &&
      outcome.retryable &&
      plan.attemptsStarted < plan.maximumAttempts
    )
      return {
        plan: this.transitionPlan(plan, {
          state: "authorized",
          outcome: outcome.summary.trim(),
        }),
        incident: structuredClone(this.requireIncident(plan.incidentId)),
      };
    const nextPlan = this.transitionPlan(plan, {
      state: outcome.ok ? "succeeded" : "failed",
      outcome: outcome.summary.trim(),
      completedAt: this.clock.now(),
    });
    const incident = this.requireIncident(plan.incidentId);
    const nextIncident = this.transitionIncident(incident, {
      state: outcome.ok ? "resolved" : "escalated",
      resolvedAt: outcome.ok ? this.clock.now() : null,
    });
    if (outcome.ok)
      this.openByTaskKind.delete(`${incident.taskId}:${incident.kind}`);
    return { plan: nextPlan, incident: nextIncident };
  }
  cancel(
    planId: string,
    expectedRevision: number,
    reason: string,
  ): AgentExecutionRecoveryPlan {
    const plan = this.requirePlan(planId);
    this.assertPlanRevision(plan, expectedRevision);
    if (
      plan.state === "succeeded" ||
      plan.state === "failed" ||
      plan.state === "cancelled"
    )
      return structuredClone(plan);
    if (!reason.trim())
      throw new E03RuntimeError(
        "agent_execution_recovery_cancel_reason",
        "agent execution recovery cancellation reason is required",
      );
    const next = this.transitionPlan(plan, {
      state: "cancelled",
      outcome: reason.trim(),
      completedAt: this.clock.now(),
    });
    const incident = this.requireIncident(plan.incidentId);
    if (incident.state !== "resolved")
      this.transitionIncident(incident, { state: "escalated" });
    return next;
  }
  unresolved(taskId?: string): AgentExecutionIncident[] {
    return [...this.incidents.values()]
      .filter(
        (value) =>
          value.state !== "resolved" && (!taskId || value.taskId === taskId),
      )
      .sort((left, right) => left.detectedAt.localeCompare(right.detectedAt))
      .map((value) => structuredClone(value));
  }
  snapshot(): {
    incidents: AgentExecutionIncident[];
    plans: AgentExecutionRecoveryPlan[];
    openByTaskKind: Array<[string, string]>;
  } {
    return {
      incidents: [...this.incidents.values()].map((value) =>
        structuredClone(value),
      ),
      plans: [...this.plans.values()].map((value) => structuredClone(value)),
      openByTaskKind: [...this.openByTaskKind.entries()].map(([key, id]) => [
        key,
        id,
      ]),
    };
  }
  restore(snapshot: {
    incidents: readonly AgentExecutionIncident[];
    plans: readonly AgentExecutionRecoveryPlan[];
    openByTaskKind: ReadonlyArray<readonly [string, string]>;
  }): void {
    const incidents = new Map<string, AgentExecutionIncident>();
    const plans = new Map<string, AgentExecutionRecoveryPlan>();
    const openByTaskKind = new Map<string, string>();
    for (const value of snapshot.incidents) {
      assertExecutionIncident(value);
      if (incidents.has(value.incidentId))
        throw new E03RuntimeError(
          "agent_execution_incident_restore_duplicate",
          `duplicate agent execution incident ${value.incidentId}`,
        );
      incidents.set(value.incidentId, structuredClone(value));
    }
    for (const value of snapshot.plans) {
      assertExecutionRecovery(value);
      const incident = incidents.get(value.incidentId);
      if (
        plans.has(value.planId) ||
        !incident ||
        incident.taskId !== value.taskId
      )
        throw new E03RuntimeError(
          "agent_execution_recovery_restore",
          `agent execution recovery ${value.planId} is invalid`,
        );
      plans.set(value.planId, structuredClone(value));
    }
    for (const [key, id] of snapshot.openByTaskKind) {
      const incident = incidents.get(id);
      if (
        !incident ||
        incident.state === "resolved" ||
        key !== `${incident.taskId}:${incident.kind}` ||
        openByTaskKind.has(key)
      )
        throw new E03RuntimeError(
          "agent_execution_incident_restore_index",
          `agent execution incident index ${key} is invalid`,
        );
      openByTaskKind.set(key, id);
    }
    this.incidents = incidents;
    this.plans = plans;
    this.openByTaskKind = openByTaskKind;
  }
  private requireIncident(id: string): AgentExecutionIncident {
    const value = this.incidents.get(id);
    if (!value)
      throw new E03RuntimeError(
        "agent_execution_incident_missing",
        `agent execution incident ${id} does not exist`,
      );
    assertExecutionIncident(value);
    return value;
  }
  private requirePlan(id: string): AgentExecutionRecoveryPlan {
    const value = this.plans.get(id);
    if (!value)
      throw new E03RuntimeError(
        "agent_execution_recovery_missing",
        `agent execution recovery ${id} does not exist`,
      );
    assertExecutionRecovery(value);
    return value;
  }
  private assertIncidentRevision(
    value: AgentExecutionIncident,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "agent_execution_incident_stale_revision",
        `agent execution incident ${value.incidentId} revision is stale`,
      );
  }
  private assertPlanRevision(
    value: AgentExecutionRecoveryPlan,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "agent_execution_recovery_stale_revision",
        `agent execution recovery ${value.planId} revision is stale`,
      );
  }
  private transitionIncident(
    value: AgentExecutionIncident,
    patch: Partial<
      Omit<AgentExecutionIncident, "incidentId" | "revision" | "digest">
    >,
  ): AgentExecutionIncident {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      incidentId: value.incidentId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertExecutionIncident(next);
    this.incidents.set(next.incidentId, next);
    return structuredClone(next);
  }
  private transitionPlan(
    value: AgentExecutionRecoveryPlan,
    patch: Partial<
      Omit<AgentExecutionRecoveryPlan, "planId" | "revision" | "digest">
    >,
  ): AgentExecutionRecoveryPlan {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      planId: value.planId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertExecutionRecovery(next);
    this.plans.set(next.planId, next);
    return structuredClone(next);
  }
}

export interface AgentExecutionBudgetAccount {
  accountId: string;
  taskId: string;
  tokenLimit: number;
  toolCallLimit: number;
  wallTimeLimitMs: number;
  tokensReserved: number;
  tokensConsumed: number;
  toolCallsReserved: number;
  toolCallsConsumed: number;
  wallTimeConsumedMs: number;
  state: "open" | "exhausted" | "closed";
  openedAt: string;
  closedAt: string | null;
  revision: number;
  digest: string;
}
export interface AgentExecutionBudgetReservation {
  reservationId: string;
  accountId: string;
  taskId: string;
  tokens: number;
  toolCalls: number;
  state: "held" | "consumed" | "released" | "expired";
  expiresAt: string;
  settledAt: string | null;
  revision: number;
  digest: string;
}
function assertBudgetAccount(value: AgentExecutionBudgetAccount): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "agent_execution_budget_digest",
      `agent execution budget ${value.accountId} is corrupt`,
    );
  const numbers = [
    value.tokenLimit,
    value.toolCallLimit,
    value.wallTimeLimitMs,
    value.tokensReserved,
    value.tokensConsumed,
    value.toolCallsReserved,
    value.toolCallsConsumed,
    value.wallTimeConsumedMs,
  ];
  if (
    !value.accountId ||
    !value.taskId ||
    numbers.some((number) => !Number.isSafeInteger(number) || number < 0) ||
    value.tokensReserved + value.tokensConsumed > value.tokenLimit ||
    value.toolCallsReserved + value.toolCallsConsumed > value.toolCallLimit ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "agent_execution_budget_account",
      `agent execution budget ${value.accountId} is invalid`,
    );
}
function assertBudgetReservation(value: AgentExecutionBudgetReservation): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "agent_execution_budget_reservation_digest",
      `agent execution budget reservation ${value.reservationId} is corrupt`,
    );
  if (
    !value.reservationId ||
    !value.accountId ||
    !value.taskId ||
    !Number.isSafeInteger(value.tokens) ||
    value.tokens < 0 ||
    !Number.isSafeInteger(value.toolCalls) ||
    value.toolCalls < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "agent_execution_budget_reservation",
      `agent execution budget reservation ${value.reservationId} is invalid`,
    );
}
export class AgentExecutionBudgetRuntime {
  private accounts = new Map<string, AgentExecutionBudgetAccount>();
  private reservations = new Map<string, AgentExecutionBudgetReservation>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  open(
    task: E03TaskState,
    input?: {
      tokenLimit?: number;
      toolCallLimit?: number;
      wallTimeLimitMs?: number;
    },
  ): AgentExecutionBudgetAccount {
    const existing = [...this.accounts.values()].find(
      (value) =>
        value.taskId === task.identity.taskId && value.state !== "closed",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      accountId: createId("agent-execution-budget"),
      taskId: task.identity.taskId,
      tokenLimit:
        input?.tokenLimit ??
        task.definition.budget.maxInputTokens +
          task.definition.budget.maxOutputTokens,
      toolCallLimit:
        input?.toolCallLimit ?? task.definition.budget.maxToolCalls,
      wallTimeLimitMs:
        input?.wallTimeLimitMs ?? task.definition.budget.maxWallTimeMs,
      tokensReserved: 0,
      tokensConsumed: 0,
      toolCallsReserved: 0,
      toolCallsConsumed: 0,
      wallTimeConsumedMs: 0,
      state: "open" as const,
      openedAt: this.clock.now(),
      closedAt: null,
      revision: 1,
    };
    const account = { ...payload, digest: digest(payload) };
    assertBudgetAccount(account);
    this.accounts.set(account.accountId, account);
    return structuredClone(account);
  }
  reserve(input: {
    accountId: string;
    expectedRevision: number;
    tokens: number;
    toolCalls: number;
    ttlMs: number;
  }): {
    account: AgentExecutionBudgetAccount;
    reservation: AgentExecutionBudgetReservation;
  } {
    const account = this.requireAccount(input.accountId);
    this.assertAccountRevision(account, input.expectedRevision);
    if (account.state !== "open")
      throw new E03RuntimeError(
        "agent_execution_budget_reserve_state",
        `agent execution budget ${account.accountId} is ${account.state}`,
      );
    if (
      !Number.isSafeInteger(input.tokens) ||
      input.tokens < 0 ||
      !Number.isSafeInteger(input.toolCalls) ||
      input.toolCalls < 0 ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "agent_execution_budget_reserve_input",
        "agent execution budget reservation is invalid",
      );
    if (
      account.tokensConsumed + account.tokensReserved + input.tokens >
        account.tokenLimit ||
      account.toolCallsConsumed + account.toolCallsReserved + input.toolCalls >
        account.toolCallLimit
    )
      throw new E03RuntimeError(
        "agent_execution_budget_exceeded",
        `agent execution budget ${account.accountId} cannot satisfy reservation`,
      );
    const payload = {
      reservationId: createId("agent-execution-budget-reservation"),
      accountId: account.accountId,
      taskId: account.taskId,
      tokens: input.tokens,
      toolCalls: input.toolCalls,
      state: "held" as const,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      settledAt: null,
      revision: 1,
    };
    const reservation = { ...payload, digest: digest(payload) };
    assertBudgetReservation(reservation);
    this.reservations.set(reservation.reservationId, reservation);
    const nextAccount = this.transitionAccount(account, {
      tokensReserved: account.tokensReserved + input.tokens,
      toolCallsReserved: account.toolCallsReserved + input.toolCalls,
    });
    return { account: nextAccount, reservation: structuredClone(reservation) };
  }
  consume(input: {
    reservationId: string;
    expectedRevision: number;
    actualTokens: number;
    actualToolCalls: number;
    wallTimeMs: number;
  }): {
    account: AgentExecutionBudgetAccount;
    reservation: AgentExecutionBudgetReservation;
  } {
    const reservation = this.requireReservation(input.reservationId);
    this.assertReservationRevision(reservation, input.expectedRevision);
    if (reservation.state !== "held")
      throw new E03RuntimeError(
        "agent_execution_budget_consume_state",
        `agent execution budget reservation ${reservation.reservationId} is ${reservation.state}`,
      );
    if (Date.parse(reservation.expiresAt) <= Date.parse(this.clock.now()))
      return this.expire(reservation.reservationId, reservation.revision);
    if (
      !Number.isSafeInteger(input.actualTokens) ||
      input.actualTokens < 0 ||
      input.actualTokens > reservation.tokens ||
      !Number.isSafeInteger(input.actualToolCalls) ||
      input.actualToolCalls < 0 ||
      input.actualToolCalls > reservation.toolCalls ||
      !Number.isSafeInteger(input.wallTimeMs) ||
      input.wallTimeMs < 0
    )
      throw new E03RuntimeError(
        "agent_execution_budget_consumption",
        "agent execution budget consumption is invalid",
      );
    const account = this.requireAccount(reservation.accountId);
    const tokensConsumed = account.tokensConsumed + input.actualTokens;
    const toolCallsConsumed = account.toolCallsConsumed + input.actualToolCalls;
    const wallTimeConsumedMs = account.wallTimeConsumedMs + input.wallTimeMs;
    const exhausted =
      tokensConsumed >= account.tokenLimit ||
      toolCallsConsumed >= account.toolCallLimit ||
      wallTimeConsumedMs >= account.wallTimeLimitMs;
    const nextAccount = this.transitionAccount(account, {
      tokensReserved: account.tokensReserved - reservation.tokens,
      toolCallsReserved: account.toolCallsReserved - reservation.toolCalls,
      tokensConsumed,
      toolCallsConsumed,
      wallTimeConsumedMs,
      state: exhausted ? "exhausted" : account.state,
    });
    const nextReservation = this.transitionReservation(reservation, {
      state: "consumed",
      settledAt: this.clock.now(),
    });
    return { account: nextAccount, reservation: nextReservation };
  }
  release(
    reservationId: string,
    expectedRevision: number,
  ): {
    account: AgentExecutionBudgetAccount;
    reservation: AgentExecutionBudgetReservation;
  } {
    const reservation = this.requireReservation(reservationId);
    this.assertReservationRevision(reservation, expectedRevision);
    if (reservation.state !== "held")
      return {
        account: structuredClone(this.requireAccount(reservation.accountId)),
        reservation: structuredClone(reservation),
      };
    const account = this.requireAccount(reservation.accountId);
    const nextAccount = this.transitionAccount(account, {
      tokensReserved: account.tokensReserved - reservation.tokens,
      toolCallsReserved: account.toolCallsReserved - reservation.toolCalls,
    });
    const nextReservation = this.transitionReservation(reservation, {
      state: "released",
      settledAt: this.clock.now(),
    });
    return { account: nextAccount, reservation: nextReservation };
  }
  expire(
    reservationId: string,
    expectedRevision: number,
  ): {
    account: AgentExecutionBudgetAccount;
    reservation: AgentExecutionBudgetReservation;
  } {
    const reservation = this.requireReservation(reservationId);
    this.assertReservationRevision(reservation, expectedRevision);
    if (reservation.state !== "held")
      return {
        account: structuredClone(this.requireAccount(reservation.accountId)),
        reservation: structuredClone(reservation),
      };
    const account = this.requireAccount(reservation.accountId);
    const nextAccount = this.transitionAccount(account, {
      tokensReserved: account.tokensReserved - reservation.tokens,
      toolCallsReserved: account.toolCallsReserved - reservation.toolCalls,
    });
    const nextReservation = this.transitionReservation(reservation, {
      state: "expired",
      settledAt: this.clock.now(),
    });
    return { account: nextAccount, reservation: nextReservation };
  }
  close(
    accountId: string,
    expectedRevision: number,
  ): AgentExecutionBudgetAccount {
    const account = this.requireAccount(accountId);
    this.assertAccountRevision(account, expectedRevision);
    if (
      [...this.reservations.values()].some(
        (value) => value.accountId === accountId && value.state === "held",
      )
    )
      throw new E03RuntimeError(
        "agent_execution_budget_live_reservation",
        `agent execution budget ${accountId} has live reservations`,
      );
    if (account.state === "closed") return structuredClone(account);
    return this.transitionAccount(account, {
      state: "closed",
      closedAt: this.clock.now(),
    });
  }
  snapshot(): {
    accounts: AgentExecutionBudgetAccount[];
    reservations: AgentExecutionBudgetReservation[];
  } {
    return {
      accounts: [...this.accounts.values()].map((value) =>
        structuredClone(value),
      ),
      reservations: [...this.reservations.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }
  restore(snapshot: {
    accounts: readonly AgentExecutionBudgetAccount[];
    reservations: readonly AgentExecutionBudgetReservation[];
  }): void {
    const accounts = new Map<string, AgentExecutionBudgetAccount>();
    const reservations = new Map<string, AgentExecutionBudgetReservation>();
    for (const value of snapshot.accounts) {
      assertBudgetAccount(value);
      if (accounts.has(value.accountId))
        throw new E03RuntimeError(
          "agent_execution_budget_restore_duplicate",
          `duplicate agent execution budget ${value.accountId}`,
        );
      accounts.set(value.accountId, structuredClone(value));
    }
    for (const value of snapshot.reservations) {
      assertBudgetReservation(value);
      const account = accounts.get(value.accountId);
      if (
        reservations.has(value.reservationId) ||
        !account ||
        account.taskId !== value.taskId
      )
        throw new E03RuntimeError(
          "agent_execution_budget_reservation_restore",
          `agent execution budget reservation ${value.reservationId} is invalid`,
        );
      reservations.set(value.reservationId, structuredClone(value));
    }
    this.accounts = accounts;
    this.reservations = reservations;
  }
  private requireAccount(id: string): AgentExecutionBudgetAccount {
    const value = this.accounts.get(id);
    if (!value)
      throw new E03RuntimeError(
        "agent_execution_budget_missing",
        `agent execution budget ${id} does not exist`,
      );
    assertBudgetAccount(value);
    return value;
  }
  private requireReservation(id: string): AgentExecutionBudgetReservation {
    const value = this.reservations.get(id);
    if (!value)
      throw new E03RuntimeError(
        "agent_execution_budget_reservation_missing",
        `agent execution budget reservation ${id} does not exist`,
      );
    assertBudgetReservation(value);
    return value;
  }
  private assertAccountRevision(
    value: AgentExecutionBudgetAccount,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "agent_execution_budget_stale_revision",
        `agent execution budget ${value.accountId} revision is stale`,
      );
  }
  private assertReservationRevision(
    value: AgentExecutionBudgetReservation,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "agent_execution_budget_reservation_stale_revision",
        `agent execution budget reservation ${value.reservationId} revision is stale`,
      );
  }
  private transitionAccount(
    value: AgentExecutionBudgetAccount,
    patch: Partial<
      Omit<AgentExecutionBudgetAccount, "accountId" | "revision" | "digest">
    >,
  ): AgentExecutionBudgetAccount {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      accountId: value.accountId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertBudgetAccount(next);
    this.accounts.set(next.accountId, next);
    return structuredClone(next);
  }
  private transitionReservation(
    value: AgentExecutionBudgetReservation,
    patch: Partial<
      Omit<
        AgentExecutionBudgetReservation,
        "reservationId" | "revision" | "digest"
      >
    >,
  ): AgentExecutionBudgetReservation {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      reservationId: value.reservationId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertBudgetReservation(next);
    this.reservations.set(next.reservationId, next);
    return structuredClone(next);
  }
}

export interface AgentExecutionCheckpoint {
  checkpointId: string;
  taskId: string;
  runId: string;
  sessionId: string;
  attempt: number;
  state: "prepared" | "committed" | "superseded" | "restored" | "rejected";
  taskRevision: number;
  contextChecksum: string;
  memoryChecksum: string;
  toolCursor: number;
  messageCursor: number;
  deliveryCursor: number;
  leaseId: string;
  hostId: string;
  payload: JsonObject;
  predecessorCheckpointId: string | null;
  preparedAt: string;
  committedAt: string | null;
  restoredAt: string | null;
  rejectionReason: string | null;
  revision: number;
  digest: string;
}
export interface AgentCheckpointRestore {
  restoreId: string;
  checkpointId: string;
  taskId: string;
  requestedAttempt: number;
  outcome: "accepted" | "rejected";
  restoredTaskRevision: number | null;
  reason: string;
  requestedAt: string;
  digest: string;
}
function assertAgentCheckpoint(value: AgentExecutionCheckpoint): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "agent_checkpoint_digest",
      `agent checkpoint ${value.checkpointId} is corrupt`,
    );
  if (
    !value.checkpointId ||
    !value.taskId ||
    !value.runId ||
    !value.sessionId ||
    !value.contextChecksum ||
    !value.memoryChecksum ||
    !value.leaseId ||
    !value.hostId ||
    !Number.isSafeInteger(value.attempt) ||
    value.attempt < 1 ||
    !Number.isSafeInteger(value.taskRevision) ||
    value.taskRevision < 1 ||
    [value.toolCursor, value.messageCursor, value.deliveryCursor].some(
      (number) => !Number.isSafeInteger(number) || number < 0,
    ) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "agent_checkpoint",
      `agent checkpoint ${value.checkpointId} is invalid`,
    );
}
function assertCheckpointRestore(value: AgentCheckpointRestore): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "agent_checkpoint_restore_digest",
      `agent checkpoint restore ${value.restoreId} is corrupt`,
    );
  if (
    !value.restoreId ||
    !value.checkpointId ||
    !value.taskId ||
    !value.reason ||
    !Number.isSafeInteger(value.requestedAttempt) ||
    value.requestedAttempt < 1 ||
    (value.restoredTaskRevision !== null &&
      (!Number.isSafeInteger(value.restoredTaskRevision) ||
        value.restoredTaskRevision < 1))
  )
    throw new E03RuntimeError(
      "agent_checkpoint_restore",
      `agent checkpoint restore ${value.restoreId} is invalid`,
    );
}
export class AgentExecutionCheckpointRuntime {
  private checkpoints = new Map<string, AgentExecutionCheckpoint>();
  private heads = new Map<string, string>();
  private restores = new Map<string, AgentCheckpointRestore[]>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  prepare(input: {
    task: E03TaskState;
    hostId: string;
    contextChecksum: string;
    memoryChecksum: string;
    toolCursor: number;
    messageCursor: number;
    deliveryCursor: number;
    payload?: JsonObject;
  }): AgentExecutionCheckpoint {
    if (
      isTerminal(input.task.status) ||
      !input.hostId.trim() ||
      !input.contextChecksum ||
      !input.memoryChecksum ||
      [input.toolCursor, input.messageCursor, input.deliveryCursor].some(
        (number) => !Number.isSafeInteger(number) || number < 0,
      )
    )
      throw new E03RuntimeError(
        "agent_checkpoint_prepare",
        "agent checkpoint prepare input is invalid",
      );
    const headId = this.heads.get(input.task.identity.taskId) ?? null;
    const head = headId ? this.requireCheckpoint(headId) : null;
    if (head && head.taskRevision >= input.task.revision) {
      if (
        head.taskRevision === input.task.revision &&
        head.contextChecksum === input.contextChecksum &&
        head.memoryChecksum === input.memoryChecksum
      )
        return structuredClone(head);
      throw new E03RuntimeError(
        "agent_checkpoint_revision_conflict",
        `task ${input.task.identity.taskId} checkpoint revision did not advance`,
      );
    }
    const payload = {
      checkpointId: createId("agent-execution-checkpoint"),
      taskId: input.task.identity.taskId,
      runId: input.task.identity.runId,
      sessionId: input.task.identity.sessionId,
      attempt: input.task.identity.attempt,
      state: "prepared" as const,
      taskRevision: input.task.revision,
      contextChecksum: input.contextChecksum,
      memoryChecksum: input.memoryChecksum,
      toolCursor: input.toolCursor,
      messageCursor: input.messageCursor,
      deliveryCursor: input.deliveryCursor,
      leaseId: input.task.identity.leaseId,
      hostId: input.hostId.trim(),
      payload: structuredClone(input.payload ?? {}),
      predecessorCheckpointId: headId,
      preparedAt: this.clock.now(),
      committedAt: null,
      restoredAt: null,
      rejectionReason: null,
      revision: 1,
    };
    const checkpoint = { ...payload, digest: digest(payload) };
    assertAgentCheckpoint(checkpoint);
    this.checkpoints.set(checkpoint.checkpointId, checkpoint);
    return structuredClone(checkpoint);
  }
  commit(
    checkpointId: string,
    expectedRevision: number,
  ): AgentExecutionCheckpoint {
    const checkpoint = this.requireCheckpoint(checkpointId);
    this.assertCheckpointRevision(checkpoint, expectedRevision);
    if (checkpoint.state !== "prepared")
      throw new E03RuntimeError(
        "agent_checkpoint_commit_state",
        `agent checkpoint ${checkpointId} is ${checkpoint.state}`,
      );
    const currentHeadId = this.heads.get(checkpoint.taskId) ?? null;
    if (currentHeadId !== checkpoint.predecessorCheckpointId)
      throw new E03RuntimeError(
        "agent_checkpoint_head_changed",
        `task ${checkpoint.taskId} checkpoint head changed`,
      );
    if (currentHeadId) {
      const head = this.requireCheckpoint(currentHeadId);
      this.transitionCheckpoint(head, { state: "superseded" });
    }
    const next = this.transitionCheckpoint(checkpoint, {
      state: "committed",
      committedAt: this.clock.now(),
    });
    this.heads.set(next.taskId, next.checkpointId);
    return next;
  }
  reject(
    checkpointId: string,
    expectedRevision: number,
    reason: string,
  ): AgentExecutionCheckpoint {
    const checkpoint = this.requireCheckpoint(checkpointId);
    this.assertCheckpointRevision(checkpoint, expectedRevision);
    if (checkpoint.state !== "prepared")
      throw new E03RuntimeError(
        "agent_checkpoint_reject_state",
        `agent checkpoint ${checkpointId} is ${checkpoint.state}`,
      );
    if (!reason.trim())
      throw new E03RuntimeError(
        "agent_checkpoint_reject_reason",
        "agent checkpoint rejection reason is required",
      );
    return this.transitionCheckpoint(checkpoint, {
      state: "rejected",
      rejectionReason: reason.trim(),
    });
  }
  restore(input: {
    checkpointId: string;
    requestedAttempt: number;
    leaseId: string;
    contextChecksum: string;
    memoryChecksum: string;
  }): {
    checkpoint: AgentExecutionCheckpoint;
    receipt: AgentCheckpointRestore;
  } {
    const checkpoint = this.requireCheckpoint(input.checkpointId);
    let accepted = true;
    let reason = "agent checkpoint accepted for restore";
    if (checkpoint.state !== "committed" && checkpoint.state !== "superseded") {
      accepted = false;
      reason = `agent checkpoint is ${checkpoint.state}`;
    } else if (
      !Number.isSafeInteger(input.requestedAttempt) ||
      input.requestedAttempt <= checkpoint.attempt
    ) {
      accepted = false;
      reason = "restore attempt must advance";
    } else if (input.leaseId === checkpoint.leaseId) {
      accepted = false;
      reason = "restore requires a new lease";
    } else if (
      input.contextChecksum !== checkpoint.contextChecksum ||
      input.memoryChecksum !== checkpoint.memoryChecksum
    ) {
      accepted = false;
      reason = "checkpoint restore checksum mismatch";
    }
    const payload = {
      restoreId: createId("agent-checkpoint-restore"),
      checkpointId: checkpoint.checkpointId,
      taskId: checkpoint.taskId,
      requestedAttempt: input.requestedAttempt,
      outcome: accepted ? ("accepted" as const) : ("rejected" as const),
      restoredTaskRevision: accepted ? checkpoint.taskRevision : null,
      reason,
      requestedAt: this.clock.now(),
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertCheckpointRestore(receipt);
    const entries = this.restores.get(checkpoint.checkpointId) ?? [];
    entries.push(receipt);
    this.restores.set(checkpoint.checkpointId, entries);
    const nextCheckpoint = accepted
      ? this.transitionCheckpoint(checkpoint, {
          state: "restored",
          restoredAt: this.clock.now(),
        })
      : structuredClone(checkpoint);
    return { checkpoint: nextCheckpoint, receipt: structuredClone(receipt) };
  }
  head(taskId: string): AgentExecutionCheckpoint | null {
    const id = this.heads.get(taskId);
    return id ? structuredClone(this.requireCheckpoint(id)) : null;
  }
  lineage(checkpointId: string): AgentExecutionCheckpoint[] {
    const values: AgentExecutionCheckpoint[] = [];
    const seen = new Set<string>();
    let cursor: AgentExecutionCheckpoint | null =
      this.requireCheckpoint(checkpointId);
    while (cursor) {
      if (seen.has(cursor.checkpointId))
        throw new E03RuntimeError(
          "agent_checkpoint_cycle",
          `agent checkpoint lineage cycles at ${cursor.checkpointId}`,
        );
      seen.add(cursor.checkpointId);
      values.push(structuredClone(cursor));
      cursor = cursor.predecessorCheckpointId
        ? this.requireCheckpoint(cursor.predecessorCheckpointId)
        : null;
    }
    return values;
  }
  snapshot(): {
    checkpoints: AgentExecutionCheckpoint[];
    heads: Array<[string, string]>;
    restores: AgentCheckpointRestore[];
  } {
    return {
      checkpoints: [...this.checkpoints.values()].map((value) =>
        structuredClone(value),
      ),
      heads: [...this.heads.entries()].map(([taskId, checkpointId]) => [
        taskId,
        checkpointId,
      ]),
      restores: [...this.restores.values()]
        .flat()
        .map((value) => structuredClone(value)),
    };
  }
  restoreSnapshot(snapshot: {
    checkpoints: readonly AgentExecutionCheckpoint[];
    heads: ReadonlyArray<readonly [string, string]>;
    restores: readonly AgentCheckpointRestore[];
  }): void {
    const checkpoints = new Map<string, AgentExecutionCheckpoint>();
    const heads = new Map<string, string>();
    const restores = new Map<string, AgentCheckpointRestore[]>();
    for (const value of snapshot.checkpoints) {
      assertAgentCheckpoint(value);
      if (checkpoints.has(value.checkpointId))
        throw new E03RuntimeError(
          "agent_checkpoint_restore_duplicate",
          `duplicate agent checkpoint ${value.checkpointId}`,
        );
      checkpoints.set(value.checkpointId, structuredClone(value));
    }
    for (const value of checkpoints.values())
      if (
        value.predecessorCheckpointId &&
        !checkpoints.has(value.predecessorCheckpointId)
      )
        throw new E03RuntimeError(
          "agent_checkpoint_restore_predecessor",
          `agent checkpoint ${value.checkpointId} has no predecessor`,
        );
    for (const [taskId, checkpointId] of snapshot.heads) {
      const checkpoint = checkpoints.get(checkpointId);
      if (
        !checkpoint ||
        checkpoint.taskId !== taskId ||
        (checkpoint.state !== "committed" && checkpoint.state !== "restored") ||
        heads.has(taskId)
      )
        throw new E03RuntimeError(
          "agent_checkpoint_restore_head",
          `agent checkpoint head ${taskId} is invalid`,
        );
      heads.set(taskId, checkpointId);
    }
    for (const value of snapshot.restores) {
      assertCheckpointRestore(value);
      if (!checkpoints.has(value.checkpointId))
        throw new E03RuntimeError(
          "agent_checkpoint_restore_receipt",
          `agent checkpoint restore receipt ${value.restoreId} is invalid`,
        );
      const entries = restores.get(value.checkpointId) ?? [];
      if (entries.some((entry) => entry.restoreId === value.restoreId))
        throw new E03RuntimeError(
          "agent_checkpoint_restore_receipt_duplicate",
          `duplicate agent checkpoint restore receipt ${value.restoreId}`,
        );
      entries.push(structuredClone(value));
      restores.set(value.checkpointId, entries);
    }
    this.checkpoints = checkpoints;
    this.heads = heads;
    this.restores = restores;
    for (const value of checkpoints.values()) this.lineage(value.checkpointId);
  }
  private requireCheckpoint(id: string): AgentExecutionCheckpoint {
    const value = this.checkpoints.get(id);
    if (!value)
      throw new E03RuntimeError(
        "agent_checkpoint_missing",
        `agent checkpoint ${id} does not exist`,
      );
    assertAgentCheckpoint(value);
    return value;
  }
  private assertCheckpointRevision(
    value: AgentExecutionCheckpoint,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "agent_checkpoint_stale_revision",
        `agent checkpoint ${value.checkpointId} revision is stale`,
      );
  }
  private transitionCheckpoint(
    value: AgentExecutionCheckpoint,
    patch: Partial<
      Omit<AgentExecutionCheckpoint, "checkpointId" | "revision" | "digest">
    >,
  ): AgentExecutionCheckpoint {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      checkpointId: value.checkpointId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertAgentCheckpoint(next);
    this.checkpoints.set(next.checkpointId, next);
    return structuredClone(next);
  }
}
