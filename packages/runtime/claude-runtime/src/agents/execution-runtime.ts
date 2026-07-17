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
