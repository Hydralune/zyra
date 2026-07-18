import { createId, type E03Clock, SystemE03Clock } from "../e03/contracts.ts";
import type {
  JsonObject,
  RuntimeRunInput,
  RuntimeRunResult,
} from "../contracts.ts";
import {
  digest,
  E03RuntimeError,
  isTerminal,
  response,
  type E03ControlResponse,
  type E03TaskState,
} from "../e03/contracts.ts";
import { TaskStateMachine } from "./state-machine.ts";
import { DurableTaskRegistry, taskProjection } from "./registry.ts";
import { TaskLeaseRuntime } from "./identity-runtime.ts";
import { TeamDelivery } from "../team/delivery.ts";
import { DeliveryOutboxRuntime } from "../team/delivery.ts";

export interface TaskExecutionHost {
  runChild(input: RuntimeRunInput): Promise<RuntimeRunResult>;
  abortChild?(taskId: string, reason: string): Promise<void>;
  waitForSignal?(
    taskId: string,
    timeoutMs: number,
  ): Promise<"resume" | "cancel" | "timeout">;
}

export interface TaskDispatchInput {
  requestId: string;
  idempotencyKey: string;
  writerId: string;
  task: E03TaskState;
  parentInput: RuntimeRunInput;
  argumentsValue: JsonObject;
}

export class TaskExecutor {
  private readonly active = new Map<string, Promise<E03TaskState>>();
  private readonly deadlines = new TaskDeadlineRuntime();
  private readonly leases = new TaskLeaseRuntime();
  private readonly deliveries = new TeamDelivery();
  private readonly outbox = new DeliveryOutboxRuntime();

  constructor(
    private readonly registry: DurableTaskRegistry,
    private readonly stateMachine: TaskStateMachine,
    private readonly host: TaskExecutionHost,
  ) {}

  async dispatch(input: TaskDispatchInput): Promise<E03TaskState> {
    this.deadlines.assertDispatchable(input.task);
    this.leases.assertMutation(
      this.leases.fromTask(input.task),
      input.task.identity,
    );
    const existing = this.active.get(input.task.identity.taskId);
    if (existing) return existing;
    const execution = this.execute(input).finally(() =>
      this.active.delete(input.task.identity.taskId),
    );
    this.active.set(input.task.identity.taskId, execution);
    return execution;
  }

  async wait(taskId: string, timeoutMs = 0): Promise<E03TaskState> {
    const active = this.active.get(taskId);
    if (!active) return this.registry.require(taskId);
    if (!timeoutMs) return active;
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1)
      throw new E03RuntimeError(
        "invalid_wait_timeout",
        "wait timeout must be a positive integer",
      );
    return Promise.race([
      active,
      new Promise<E03TaskState>((_resolve, reject) =>
        setTimeout(
          () =>
            reject(
              new E03RuntimeError(
                "wait_timeout",
                `task ${taskId} did not settle within ${timeoutMs}ms`,
              ),
            ),
          timeoutMs,
        ),
      ),
    ]);
  }

  result(taskId: string): E03ControlResponse {
    const task = this.registry.require(taskId);
    if (!isTerminal(task.status))
      return response({
        ok: false,
        request_id: `result:${taskId}`,
        command: "agent.result",
        phase: "rejected",
        revision: task.revision,
        state: taskProjection(task),
        error: "task_not_terminal",
      });
    return response({
      ok: task.status === "completed",
      request_id: `result:${taskId}`,
      command: "agent.result",
      phase: "ack",
      revision: task.revision,
      state: taskProjection(task),
      result: task.result,
      error: task.error,
    });
  }

  async timeout(
    taskId: string,
    reason = "task_timeout",
  ): Promise<E03TaskState> {
    const task = this.registry.require(taskId);
    if (isTerminal(task.status)) return task;
    await this.host.abortChild?.(taskId, reason);
    const transitioned = this.stateMachine.transition(task, "failed", {
      requestId: `timeout:${taskId}:${task.revision}`,
      idempotencyKey: `timeout:${taskId}:${task.revision}`,
      writerId: "typescript.E03AgentControlCoordinator",
      expectedRevision: task.revision,
      eventType: "agent_task_timeout",
      error: reason,
    });
    const prepared = this.registry.prepare({
      requestId: transitioned.transition.requestId,
      idempotencyKey: transitioned.transition.idempotencyKey,
      writerId: transitioned.transition.writerId,
      taskId,
      expectedRevision: task.revision,
      proposed: transitioned.state,
      effectKind: "process",
      effectOperation: "abort_child_process",
      effectPayload: { reason },
    });
    const receipt = await this.registry.recordReceipt(
      prepared.transition.idempotencyKey,
    );
    return (
      await this.registry.commit(prepared.transition.idempotencyKey, receipt)
    ).state;
  }

  activeTaskIds(): string[] {
    return [...this.active.keys()].sort();
  }

  private async execute(input: TaskDispatchInput): Promise<E03TaskState> {
    let task = input.task;
    if (task.status === "created")
      task = await this.persistTransition(
        task,
        "queued",
        "agent_task_queued",
        input,
      );
    if (task.status === "queued" || task.status === "waiting")
      task = await this.persistTransition(
        task,
        "running",
        "agent_task_running",
        input,
      );
    try {
      const result = await this.host.runChild(
        childInput(task, input.parentInput, input.argumentsValue),
      );
      const current = this.registry.require(task.identity.taskId);
      this.stateMachine.rejectLateResult(
        current,
        current.revision,
        task.identity.leaseId,
      );
      if (!result.ok) {
        return this.persistTransition(
          current,
          "failed",
          "agent_task_failed",
          input,
          {
            error: result.stoppedReason ?? "child_runtime_failed",
            result: runtimeResultPayload(result),
            artifacts: result.artifacts,
            usage: usagePayload(result),
          },
        );
      }
      return this.persistTransition(
        current,
        "completed",
        "agent_task_completed",
        input,
        {
          result: runtimeResultPayload(result),
          artifacts: result.artifacts,
          usage: usagePayload(result),
        },
      );
    } catch (error) {
      const current = this.registry.require(task.identity.taskId);
      if (isTerminal(current.status)) return current;
      return this.persistTransition(
        current,
        "failed",
        "agent_task_crashed",
        input,
        { error: error instanceof Error ? error.message : String(error) },
      );
    }
  }

  private async persistTransition(
    task: E03TaskState,
    target: "queued" | "running" | "completed" | "failed",
    eventType: string,
    input: Pick<TaskDispatchInput, "requestId" | "idempotencyKey" | "writerId">,
    changes: {
      result?: JsonObject;
      artifacts?: E03TaskState["artifacts"];
      usage?: JsonObject;
      error?: string;
    } = {},
  ): Promise<E03TaskState> {
    const key = `${input.idempotencyKey}:${target}:${task.revision}`;
    let transitioned = this.stateMachine.transition(task, target, {
      requestId: `${input.requestId}:${target}`,
      idempotencyKey: key,
      writerId: input.writerId,
      expectedRevision: task.revision,
      eventType,
      ...changes,
    });
    let outboxId = "";
    if (target === "completed" || target === "failed") {
      const published = this.deliveries.publishFinal(transitioned.state, {
        summary:
          target === "completed"
            ? `Agent ${task.identity.taskId} completed`
            : changes.error || `Agent ${task.identity.taskId} failed`,
        payload: changes.result ?? { error: changes.error ?? "" },
        artifactIds: changes.artifacts?.map((artifact) => artifact.artifact_id),
        idempotencyKey: `${key}:final-delivery`,
      });
      transitioned = { ...transitioned, state: published.task };
      outboxId = this.outbox.prepare(published.task, published.delivery, {
        idempotencyKey: `${key}:outbox`,
      }).outboxId;
    }
    const prepared = this.registry.prepare({
      requestId: transitioned.transition.requestId,
      idempotencyKey: key,
      writerId: input.writerId,
      taskId: task.identity.taskId,
      expectedRevision: task.revision,
      proposed: transitioned.state,
      effectKind: "persist",
      effectOperation: "persist_task_transition",
      effectPayload: {
        event_type: eventType,
        from_status: task.status,
        to_status: target,
        outbox_id: outboxId,
      },
    });
    const receipt = await this.registry.recordReceipt(key);
    return (await this.registry.commit(key, receipt)).state;
  }
}

function childInput(
  task: E03TaskState,
  parent: RuntimeRunInput,
  argumentsValue: JsonObject,
): RuntimeRunInput {
  const tools = parent.tools
    .filter(
      (tool) =>
        task.scope.tools.length === 0 || task.scope.tools.includes(tool.name),
    )
    .filter((tool) => !task.scope.deniedTools.includes(tool.name));
  const messages = Array.isArray(argumentsValue.messages)
    ? (argumentsValue.messages as RuntimeRunInput["messages"])
    : [
        {
          role: "user",
          content: task.prompt,
          metadata: {
            agent_task_id: task.identity.taskId,
            parent_task_id: task.identity.parentTaskId,
          },
        },
      ];
  return {
    ...parent,
    taskId: task.identity.taskId,
    sessionId: task.identity.sessionId,
    workerRequestId: `agent-worker:${task.identity.attemptId}`,
    messages,
    tools,
    config: {
      ...parent.config,
      maxTurns: task.definition.budget.maxTurns,
      maxToolResultChars: task.definition.budget.maxResultChars,
      modelName:
        task.definition.model === "inherit"
          ? parent.config.modelName
          : task.definition.model,
      permissionPolicy: {
        ...parent.config.permissionPolicy,
        mode: task.scope.permissionMode,
        parent_ceiling_digest: task.scope.permissionCeilingDigest,
      },
      runtimeConstraints: {
        ...parent.config.runtimeConstraints,
        e03_agent_task_id: task.identity.taskId,
        e03_parent_task_id: task.identity.parentTaskId,
        e03_attempt_id: task.identity.attemptId,
        e03_lease_id: task.identity.leaseId,
        e03_lineage: task.identity.lineage,
        e03_scope_digest: task.scope.digest,
        e03_context_checksum: task.context.checksum,
      },
      controlCommands: [],
    },
    contextSnapshot: {
      version: "zyra.e03-agent-context/v1",
      snapshot_id: task.context.snapshotId,
      branch_id: task.context.branchId,
      checksum: task.context.checksum,
    },
    metadata: {
      ...parent.metadata,
      e03_agent_task_id: task.identity.taskId,
      canonical_agent_owner: "typescript",
    },
  };
}

function runtimeResultPayload(result: RuntimeRunResult): JsonObject {
  return {
    ok: result.ok,
    stopped_reason: result.stoppedReason,
    turn_count: result.turnCount,
    tool_call_count: result.toolCallCount,
    context_compaction_count: result.contextCompactionCount,
    step_summaries: result.stepSummaries,
    artifact_ids: result.artifacts.map((artifact) => artifact.artifact_id),
    session_snapshot: result.sessionSnapshot,
    metadata: result.metadata,
  };
}

function usagePayload(result: RuntimeRunResult): JsonObject {
  return {
    turns: result.turnCount,
    tool_calls: result.toolCallCount,
    compactions: result.contextCompactionCount,
    artifacts: result.artifacts.length,
  };
}

export type DeadlineDisposition =
  | "healthy"
  | "warning"
  | "expired"
  | "terminal"
  | "invalid";

export interface DeadlineAssessment {
  taskId: string;
  disposition: DeadlineDisposition;
  now: string;
  startedAt: string;
  deadlineAt: string;
  elapsedMs: number;
  remainingMs: number;
  consumedRatio: number;
  shouldCancel: boolean;
  reason: string;
}

export interface BudgetAssessment {
  taskId: string;
  accepted: boolean;
  exhausted: string[];
  warnings: string[];
  remaining: {
    turns: number;
    toolCalls: number;
    inputTokens: number;
    outputTokens: number;
    resultChars: number;
    children: number;
  };
}

export class TaskDeadlineRuntime {
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  assess(task: E03TaskState, warningRatio = 0.8): DeadlineAssessment {
    if (
      warningRatio <= 0 ||
      warningRatio >= 1 ||
      !Number.isFinite(warningRatio)
    )
      throw new E03RuntimeError(
        "invalid_deadline_warning_ratio",
        "deadline warning ratio must be between zero and one",
      );
    const now = this.clock.now();
    const startedAt = task.definition.budget.startedAt || task.createdAt;
    const deadlineAt = task.definition.budget.deadlineAt;
    const start = Date.parse(startedAt);
    const deadline = Date.parse(deadlineAt);
    const current = Date.parse(now);
    if (
      !Number.isFinite(start) ||
      !Number.isFinite(deadline) ||
      deadline <= start
    ) {
      return {
        taskId: task.identity.taskId,
        disposition: "invalid",
        now,
        startedAt,
        deadlineAt,
        elapsedMs: 0,
        remainingMs: 0,
        consumedRatio: 1,
        shouldCancel: true,
        reason: "invalid_deadline",
      };
    }
    const elapsedMs = Math.max(0, current - start);
    const remainingMs = Math.max(0, deadline - current);
    const consumedRatio = Math.min(1, elapsedMs / (deadline - start));
    if (isTerminal(task.status))
      return {
        taskId: task.identity.taskId,
        disposition: "terminal",
        now,
        startedAt,
        deadlineAt,
        elapsedMs,
        remainingMs,
        consumedRatio,
        shouldCancel: false,
        reason: task.status,
      };
    if (current >= deadline)
      return {
        taskId: task.identity.taskId,
        disposition: "expired",
        now,
        startedAt,
        deadlineAt,
        elapsedMs,
        remainingMs: 0,
        consumedRatio: 1,
        shouldCancel: true,
        reason: "wall_time_exhausted",
      };
    if (consumedRatio >= warningRatio)
      return {
        taskId: task.identity.taskId,
        disposition: "warning",
        now,
        startedAt,
        deadlineAt,
        elapsedMs,
        remainingMs,
        consumedRatio,
        shouldCancel: false,
        reason: "wall_time_near_exhaustion",
      };
    return {
      taskId: task.identity.taskId,
      disposition: "healthy",
      now,
      startedAt,
      deadlineAt,
      elapsedMs,
      remainingMs,
      consumedRatio,
      shouldCancel: false,
      reason: "within_deadline",
    };
  }

  assessBudget(task: E03TaskState): BudgetAssessment {
    const budget = task.definition.budget;
    const remaining = {
      turns: budget.maxTurns - budget.consumedTurns,
      toolCalls: budget.maxToolCalls - budget.consumedToolCalls,
      inputTokens: budget.maxInputTokens - budget.consumedInputTokens,
      outputTokens: budget.maxOutputTokens - budget.consumedOutputTokens,
      resultChars: budget.maxResultChars - budget.consumedResultChars,
      children: budget.maxChildren - task.childTaskIds.length,
    };
    const exhausted = Object.entries(remaining)
      .filter(([, value]) => value <= 0)
      .map(([name]) => name);
    const warnings = Object.entries(remaining)
      .filter(([name, value]) => {
        const maximum = maximumFor(name, budget);
        return value > 0 && maximum > 0 && value / maximum <= 0.2;
      })
      .map(([name]) => name);
    return {
      taskId: task.identity.taskId,
      accepted: exhausted.length === 0,
      exhausted,
      warnings,
      remaining,
    };
  }

  assertDispatchable(task: E03TaskState): void {
    if (isTerminal(task.status))
      throw new E03RuntimeError(
        "terminal_task",
        `cannot dispatch ${task.status} task`,
      );
    const deadline = this.assess(task);
    if (deadline.shouldCancel)
      throw new E03RuntimeError(
        deadline.reason,
        `task ${task.identity.taskId} exceeded its wall-time budget`,
        { deadlineAt: deadline.deadlineAt, elapsedMs: deadline.elapsedMs },
      );
    const budget = this.assessBudget(task);
    if (!budget.accepted)
      throw new E03RuntimeError(
        "agent_budget_exhausted",
        `task budget exhausted: ${budget.exhausted.join(", ")}`,
        { exhausted: budget.exhausted, remaining: budget.remaining },
      );
  }

  nextWakeup(tasks: readonly E03TaskState[]): string | null {
    const active = tasks
      .filter((task) => !isTerminal(task.status))
      .map((task) => task.definition.budget.deadlineAt)
      .filter((value) => Number.isFinite(Date.parse(value)))
      .sort();
    return active[0] ?? null;
  }

  expired(tasks: readonly E03TaskState[]): DeadlineAssessment[] {
    return tasks
      .map((task) => this.assess(task))
      .filter((assessment) => assessment.disposition === "expired");
  }
}

function maximumFor(
  name: string,
  budget: E03TaskState["definition"]["budget"],
): number {
  if (name === "turns") return budget.maxTurns;
  if (name === "toolCalls") return budget.maxToolCalls;
  if (name === "inputTokens") return budget.maxInputTokens;
  if (name === "outputTokens") return budget.maxOutputTokens;
  if (name === "resultChars") return budget.maxResultChars;
  if (name === "children") return budget.maxChildren;
  return 0;
}

export type ExecutionClaimPhase =
  | "offered"
  | "claimed"
  | "started"
  | "heartbeat"
  | "released"
  | "expired"
  | "completed"
  | "failed";

export interface TaskExecutionClaim {
  claimId: string;
  taskId: string;
  sessionId: string;
  taskLeaseId: string;
  attempt: number;
  expectedRevision: number;
  workerId: string;
  workerClass: string;
  capabilities: string[];
  phase: ExecutionClaimPhase;
  acquiredAt: string;
  lastHeartbeatAt: string;
  expiresAt: string;
  releasedAt: string | null;
  releaseReason: string;
  heartbeatSequence: number;
  revision: number;
  idempotencyKey: string;
  digest: string;
}

export interface ExecutionClaimDecision {
  accepted: boolean;
  code: string;
  taskId: string;
  workerId: string;
  currentClaimId: string | null;
  currentWorkerId: string | null;
  taskLeaseMatched: boolean;
  revisionMatched: boolean;
  expiredClaimRecovered: boolean;
  decidedAt: string;
  digest: string;
}

function assertExecutionClaim(claim: TaskExecutionClaim): void {
  const { digest: checksum, ...payload } = claim;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "execution_claim_checksum",
      `execution claim ${claim.claimId} checksum mismatch`,
    );
  if (!claim.claimId || !claim.taskId || !claim.workerId || !claim.taskLeaseId)
    throw new E03RuntimeError(
      "execution_claim_identity",
      "execution claim identity is incomplete",
    );
  if (
    claim.attempt < 1 ||
    claim.expectedRevision < 1 ||
    claim.heartbeatSequence < 0 ||
    claim.revision < 1
  )
    throw new E03RuntimeError(
      "execution_claim_revision",
      `execution claim ${claim.claimId} revision is invalid`,
    );
  if (Date.parse(claim.expiresAt) <= Date.parse(claim.acquiredAt))
    throw new E03RuntimeError(
      "execution_claim_window",
      `execution claim ${claim.claimId} has an invalid validity window`,
    );
  if (
    ["released", "expired", "completed", "failed"].includes(claim.phase) &&
    !claim.releasedAt
  )
    throw new E03RuntimeError(
      "execution_claim_release_time",
      `terminal execution claim ${claim.claimId} has no release time`,
    );
}

function resealExecutionClaim(
  claim: TaskExecutionClaim,
  patch: Partial<Omit<TaskExecutionClaim, "claimId" | "taskId" | "digest">>,
): TaskExecutionClaim {
  const { digest: _, ...prior } = claim;
  const payload = {
    ...prior,
    ...patch,
    claimId: claim.claimId,
    taskId: claim.taskId,
  };
  const next = { ...payload, digest: digest(payload) };
  assertExecutionClaim(next);
  return next;
}

export class TaskExecutionClaimRuntime {
  private claims = new Map<string, TaskExecutionClaim>();
  private activeByTask = new Map<string, string>();
  private idempotency = new Map<string, string>();

  decide(input: {
    task: E03TaskState;
    workerId: string;
    expectedRevision: number;
    now?: string;
  }): ExecutionClaimDecision {
    const now = input.now ?? new Date().toISOString();
    const activeId = this.activeByTask.get(input.task.identity.taskId) ?? null;
    const active = activeId ? (this.claims.get(activeId) ?? null) : null;
    const activeExpired = Boolean(
      active && Date.parse(now) >= Date.parse(active.expiresAt),
    );
    const taskLeaseMatched =
      !active || active.taskLeaseId === input.task.identity.leaseId;
    const revisionMatched = input.task.revision === input.expectedRevision;
    let code = "execution_claim_accepted";
    if (isTerminal(input.task.status)) code = "execution_claim_terminal_task";
    else if (!input.workerId.trim()) code = "execution_claim_worker_missing";
    else if (!revisionMatched) code = "execution_claim_stale_revision";
    else if (active && !activeExpired && active.workerId !== input.workerId)
      code = "execution_claim_held";
    else if (active && !taskLeaseMatched)
      code = "execution_claim_stale_task_lease";
    const payload = {
      accepted: code === "execution_claim_accepted",
      code,
      taskId: input.task.identity.taskId,
      workerId: input.workerId,
      currentClaimId: active?.claimId ?? null,
      currentWorkerId: active?.workerId ?? null,
      taskLeaseMatched,
      revisionMatched,
      expiredClaimRecovered: Boolean(activeExpired),
      decidedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  acquire(input: {
    task: E03TaskState;
    workerId: string;
    workerClass: string;
    capabilities?: readonly string[];
    expectedRevision: number;
    ttlMs: number;
    idempotencyKey: string;
    now?: string;
  }): TaskExecutionClaim {
    if (!Number.isSafeInteger(input.ttlMs) || input.ttlMs < 1)
      throw new E03RuntimeError(
        "execution_claim_ttl_invalid",
        "execution claim TTL is invalid",
      );
    const idempotencyKey = input.idempotencyKey.trim();
    if (!idempotencyKey)
      throw new E03RuntimeError(
        "execution_claim_idempotency_missing",
        "execution claim idempotency key is required",
      );
    const bound = this.idempotency.get(idempotencyKey);
    if (bound) return structuredClone(this.claims.get(bound)!);
    const decision = this.decide(input);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `worker ${input.workerId} cannot claim task ${input.task.identity.taskId}`,
        { currentWorkerId: decision.currentWorkerId },
      );
    if (decision.currentClaimId && decision.expiredClaimRecovered) {
      const current = this.claims.get(decision.currentClaimId)!;
      const expired = resealExecutionClaim(current, {
        phase: "expired",
        releasedAt: decision.decidedAt,
        releaseReason: "lease_expired",
        revision: current.revision + 1,
      });
      this.claims.set(expired.claimId, expired);
      this.activeByTask.delete(expired.taskId);
    }
    const acquiredAt = input.now ?? new Date().toISOString();
    const payload = {
      claimId: `execution-claim-${digest({
        taskId: input.task.identity.taskId,
        taskLeaseId: input.task.identity.leaseId,
        workerId: input.workerId,
        expectedRevision: input.expectedRevision,
        idempotencyKey,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      taskLeaseId: input.task.identity.leaseId,
      attempt: input.task.identity.attempt,
      expectedRevision: input.expectedRevision,
      workerId: input.workerId.trim(),
      workerClass: input.workerClass.trim() || "default",
      capabilities: [...new Set(input.capabilities ?? [])].sort(),
      phase: "claimed" as const,
      acquiredAt,
      lastHeartbeatAt: acquiredAt,
      expiresAt: new Date(Date.parse(acquiredAt) + input.ttlMs).toISOString(),
      releasedAt: null,
      releaseReason: "",
      heartbeatSequence: 0,
      revision: 1,
      idempotencyKey,
    };
    const claim = { ...payload, digest: digest(payload) };
    assertExecutionClaim(claim);
    this.claims.set(claim.claimId, claim);
    this.activeByTask.set(claim.taskId, claim.claimId);
    this.idempotency.set(idempotencyKey, claim.claimId);
    return structuredClone(claim);
  }

  start(
    claimId: string,
    task: E03TaskState,
    now = new Date().toISOString(),
  ): TaskExecutionClaim {
    const current = this.require(claimId);
    this.assertTask(current, task, now);
    if (current.phase === "started" || current.phase === "heartbeat")
      return structuredClone(current);
    if (current.phase !== "claimed")
      throw new E03RuntimeError(
        "execution_claim_not_claimed",
        `execution claim ${claimId} cannot start from ${current.phase}`,
      );
    const next = resealExecutionClaim(current, {
      phase: "started",
      lastHeartbeatAt: now,
      revision: current.revision + 1,
    });
    this.claims.set(claimId, next);
    return structuredClone(next);
  }

  heartbeat(input: {
    claimId: string;
    task: E03TaskState;
    expectedHeartbeatSequence: number;
    ttlMs: number;
    now?: string;
  }): TaskExecutionClaim {
    const current = this.require(input.claimId);
    const now = input.now ?? new Date().toISOString();
    this.assertTask(current, input.task, now);
    if (!["started", "heartbeat"].includes(current.phase))
      throw new E03RuntimeError(
        "execution_claim_not_running",
        `execution claim ${current.claimId} cannot heartbeat from ${current.phase}`,
      );
    if (current.heartbeatSequence !== input.expectedHeartbeatSequence)
      throw new E03RuntimeError(
        "execution_claim_stale_heartbeat",
        `execution claim ${current.claimId} heartbeat sequence changed`,
      );
    if (!Number.isSafeInteger(input.ttlMs) || input.ttlMs < 1)
      throw new E03RuntimeError(
        "execution_claim_ttl_invalid",
        "execution claim TTL is invalid",
      );
    const next = resealExecutionClaim(current, {
      phase: "heartbeat",
      lastHeartbeatAt: now,
      expiresAt: new Date(Date.parse(now) + input.ttlMs).toISOString(),
      heartbeatSequence: current.heartbeatSequence + 1,
      revision: current.revision + 1,
    });
    this.claims.set(current.claimId, next);
    return structuredClone(next);
  }

  release(input: {
    claimId: string;
    task: E03TaskState;
    outcome: "released" | "completed" | "failed";
    reason: string;
    now?: string;
  }): TaskExecutionClaim {
    const current = this.require(input.claimId);
    const now = input.now ?? new Date().toISOString();
    if (["released", "completed", "failed", "expired"].includes(current.phase))
      return structuredClone(current);
    if (
      current.taskId !== input.task.identity.taskId ||
      current.taskLeaseId !== input.task.identity.leaseId
    )
      throw new E03RuntimeError(
        "execution_claim_release_custody",
        "execution claim release belongs to another task or lease",
      );
    const next = resealExecutionClaim(current, {
      phase: input.outcome,
      releasedAt: now,
      releaseReason: input.reason.trim() || input.outcome,
      revision: current.revision + 1,
    });
    this.claims.set(current.claimId, next);
    this.activeByTask.delete(current.taskId);
    return structuredClone(next);
  }

  restore(claims: readonly TaskExecutionClaim[]): void {
    const next = new Map<string, TaskExecutionClaim>();
    const active = new Map<string, string>();
    const idempotency = new Map<string, string>();
    for (const raw of claims) {
      const claim = structuredClone(raw);
      assertExecutionClaim(claim);
      if (next.has(claim.claimId) || idempotency.has(claim.idempotencyKey))
        throw new E03RuntimeError(
          "duplicate_execution_claim",
          `execution claim ${claim.claimId} repeats`,
        );
      if (
        !["released", "expired", "completed", "failed"].includes(claim.phase)
      ) {
        if (active.has(claim.taskId))
          throw new E03RuntimeError(
            "duplicate_active_execution_claim",
            `task ${claim.taskId} has multiple active claims`,
          );
        active.set(claim.taskId, claim.claimId);
      }
      next.set(claim.claimId, claim);
      idempotency.set(claim.idempotencyKey, claim.claimId);
    }
    this.claims = next;
    this.activeByTask = active;
    this.idempotency = idempotency;
  }

  snapshot(): TaskExecutionClaim[] {
    return [...this.claims.values()]
      .sort((left, right) => left.claimId.localeCompare(right.claimId))
      .map((claim) => structuredClone(claim));
  }

  private require(claimId: string): TaskExecutionClaim {
    const claim = this.claims.get(claimId);
    if (!claim)
      throw new E03RuntimeError(
        "execution_claim_missing",
        `execution claim ${claimId} is missing`,
      );
    assertExecutionClaim(claim);
    return claim;
  }

  private assertTask(
    claim: TaskExecutionClaim,
    task: E03TaskState,
    now: string,
  ): void {
    if (
      claim.taskId !== task.identity.taskId ||
      claim.sessionId !== task.identity.sessionId ||
      claim.taskLeaseId !== task.identity.leaseId ||
      claim.attempt !== task.identity.attempt
    )
      throw new E03RuntimeError(
        "execution_claim_task_custody",
        "execution claim belongs to another task attempt",
      );
    if (Date.parse(now) >= Date.parse(claim.expiresAt))
      throw new E03RuntimeError(
        "execution_claim_expired",
        `execution claim ${claim.claimId} expired`,
      );
    if (task.revision < claim.expectedRevision)
      throw new E03RuntimeError(
        "execution_claim_task_revision",
        "task revision is behind execution claim fence",
      );
  }
}

export type TaskExecutionActivityKind =
  | "dispatch"
  | "claim"
  | "start"
  | "model-request"
  | "model-response"
  | "tool-request"
  | "tool-result"
  | "message"
  | "artifact"
  | "checkpoint"
  | "wait"
  | "resume"
  | "heartbeat"
  | "budget-warning"
  | "timeout"
  | "cancel"
  | "kill"
  | "result"
  | "error";

export interface TaskExecutionActivityRecord {
  activityId: string;
  taskId: string;
  sessionId: string;
  leaseId: string;
  attempt: number;
  taskRevision: number;
  activitySequence: number;
  kind: TaskExecutionActivityKind;
  source: string;
  summary: string;
  detail: JsonObject;
  detailDigest: string;
  inputTokens: number;
  outputTokens: number;
  toolCalls: number;
  resultChars: number;
  artifactIds: string[];
  messageIds: string[];
  correlationIds: string[];
  occurredAt: string;
  previousDigest: string;
  digest: string;
}

export interface TaskExecutionActivityProjection {
  taskId: string;
  sessionId: string;
  leaseId: string;
  attempt: number;
  activityCount: number;
  lastKind: TaskExecutionActivityKind | null;
  lastSummary: string;
  lastActivityAt: string | null;
  inputTokens: number;
  outputTokens: number;
  toolCalls: number;
  resultChars: number;
  artifactIds: string[];
  messageIds: string[];
  correlationIds: string[];
  errorCount: number;
  warningCount: number;
  idleMs: number;
  digest: string;
}

function executionCounter(value: number | undefined, name: string): number {
  const normalized = value ?? 0;
  if (!Number.isSafeInteger(normalized) || normalized < 0)
    throw new E03RuntimeError(
      "execution_activity_counter",
      `execution activity ${name} counter is invalid`,
    );
  return normalized;
}

function assertExecutionActivity(record: TaskExecutionActivityRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "execution_activity_checksum",
      `execution activity ${record.activityId} checksum mismatch`,
    );
  if (
    !record.taskId ||
    !record.leaseId ||
    !record.source ||
    !record.summary ||
    record.attempt < 1 ||
    record.taskRevision < 1 ||
    record.activitySequence < 1
  )
    throw new E03RuntimeError(
      "execution_activity_identity",
      `execution activity ${record.activityId} identity is invalid`,
    );
  executionCounter(record.inputTokens, "inputTokens");
  executionCounter(record.outputTokens, "outputTokens");
  executionCounter(record.toolCalls, "toolCalls");
  executionCounter(record.resultChars, "resultChars");
}

export class TaskExecutionActivityRuntime {
  private activities = new Map<string, TaskExecutionActivityRecord[]>();
  private byId = new Map<string, TaskExecutionActivityRecord>();

  append(input: {
    task: E03TaskState;
    kind: TaskExecutionActivityKind;
    source: string;
    summary: string;
    detail?: JsonObject;
    inputTokens?: number;
    outputTokens?: number;
    toolCalls?: number;
    resultChars?: number;
    artifactIds?: readonly string[];
    messageIds?: readonly string[];
    correlationIds?: readonly string[];
    now?: string;
  }): TaskExecutionActivityRecord {
    const source = input.source.trim();
    const summary = input.summary.trim();
    if (!source || !summary)
      throw new E03RuntimeError(
        "execution_activity_text_missing",
        "execution activity source and summary are required",
      );
    const current = this.activities.get(input.task.identity.taskId) ?? [];
    const last = current.at(-1);
    if (last && last.leaseId !== input.task.identity.leaseId)
      throw new E03RuntimeError(
        "execution_activity_stale_lease",
        "execution activity uses another task lease",
      );
    if (last && last.taskRevision > input.task.revision)
      throw new E03RuntimeError(
        "execution_activity_stale_revision",
        "execution activity is behind the prior activity revision",
      );
    const detail = structuredClone(input.detail ?? {});
    const payload = {
      activityId: `execution-activity-${digest({
        taskId: input.task.identity.taskId,
        leaseId: input.task.identity.leaseId,
        sequence: current.length + 1,
        kind: input.kind,
        source,
        detail,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      leaseId: input.task.identity.leaseId,
      attempt: input.task.identity.attempt,
      taskRevision: input.task.revision,
      activitySequence: current.length + 1,
      kind: input.kind,
      source,
      summary,
      detail,
      detailDigest: digest(detail),
      inputTokens: executionCounter(input.inputTokens, "inputTokens"),
      outputTokens: executionCounter(input.outputTokens, "outputTokens"),
      toolCalls: executionCounter(input.toolCalls, "toolCalls"),
      resultChars: executionCounter(input.resultChars, "resultChars"),
      artifactIds: [...new Set(input.artifactIds ?? [])].sort(),
      messageIds: [...new Set(input.messageIds ?? [])].sort(),
      correlationIds: [...new Set(input.correlationIds ?? [])].sort(),
      occurredAt: input.now ?? new Date().toISOString(),
      previousDigest: last?.digest ?? "",
    };
    const record = { ...payload, digest: digest(payload) };
    assertExecutionActivity(record);
    const existing = this.byId.get(record.activityId);
    if (existing) return structuredClone(existing);
    current.push(record);
    this.activities.set(record.taskId, current);
    this.byId.set(record.activityId, record);
    return structuredClone(record);
  }

  project(
    task: E03TaskState,
    now = new Date().toISOString(),
  ): TaskExecutionActivityProjection {
    const activities = this.forTask(task.identity.taskId);
    const last = activities.at(-1);
    const payload = {
      taskId: task.identity.taskId,
      sessionId: task.identity.sessionId,
      leaseId: task.identity.leaseId,
      attempt: task.identity.attempt,
      activityCount: activities.length,
      lastKind: last?.kind ?? null,
      lastSummary: last?.summary ?? "",
      lastActivityAt: last?.occurredAt ?? null,
      inputTokens: activities.reduce(
        (sum, value) => sum + value.inputTokens,
        0,
      ),
      outputTokens: activities.reduce(
        (sum, value) => sum + value.outputTokens,
        0,
      ),
      toolCalls: activities.reduce((sum, value) => sum + value.toolCalls, 0),
      resultChars: activities.reduce(
        (sum, value) => sum + value.resultChars,
        0,
      ),
      artifactIds: [
        ...new Set(activities.flatMap((value) => value.artifactIds)),
      ].sort(),
      messageIds: [
        ...new Set(activities.flatMap((value) => value.messageIds)),
      ].sort(),
      correlationIds: [
        ...new Set(activities.flatMap((value) => value.correlationIds)),
      ].sort(),
      errorCount: activities.filter((value) => value.kind === "error").length,
      warningCount: activities.filter(
        (value) => value.kind === "budget-warning",
      ).length,
      idleMs: Math.max(
        0,
        Date.parse(now) - Date.parse(last?.occurredAt ?? task.createdAt),
      ),
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(records: readonly TaskExecutionActivityRecord[]): void {
    const activities = new Map<string, TaskExecutionActivityRecord[]>();
    const byId = new Map<string, TaskExecutionActivityRecord>();
    for (const raw of records) {
      const record = structuredClone(raw);
      assertExecutionActivity(record);
      if (byId.has(record.activityId))
        throw new E03RuntimeError(
          "duplicate_execution_activity",
          `execution activity ${record.activityId} repeats`,
        );
      const current = activities.get(record.taskId) ?? [];
      const last = current.at(-1);
      if (record.activitySequence !== current.length + 1)
        throw new E03RuntimeError(
          "execution_activity_sequence",
          `task ${record.taskId} execution activity sequence is discontinuous`,
        );
      if (record.previousDigest !== (last?.digest ?? ""))
        throw new E03RuntimeError(
          "execution_activity_chain",
          `task ${record.taskId} execution activity digest chain is broken`,
        );
      current.push(record);
      activities.set(record.taskId, current);
      byId.set(record.activityId, record);
    }
    this.activities = activities;
    this.byId = byId;
  }

  forTask(taskId: string): TaskExecutionActivityRecord[] {
    return (this.activities.get(taskId) ?? []).map((record) =>
      structuredClone(record),
    );
  }

  snapshot(): TaskExecutionActivityRecord[] {
    return [...this.activities.values()]
      .flat()
      .sort(
        (left, right) =>
          left.occurredAt.localeCompare(right.occurredAt) ||
          left.taskId.localeCompare(right.taskId) ||
          left.activitySequence - right.activitySequence,
      )
      .map((record) => structuredClone(record));
  }
}

export type TaskWaitConditionKind =
  | "signal"
  | "message"
  | "child"
  | "artifact"
  | "approval"
  | "time"
  | "resource";

export interface TaskWaitCondition {
  conditionId: string;
  taskId: string;
  sessionId: string;
  leaseId: string;
  expectedRevision: number;
  kind: TaskWaitConditionKind;
  key: string;
  requiredCount: number;
  observedCount: number;
  optional: boolean;
  createdAt: string;
  expiresAt: string | null;
  satisfiedAt: string | null;
  satisfactionDigest: string | null;
  cancelledAt: string | null;
  cancellationReason: string;
  revision: number;
  digest: string;
}

export interface TaskWaitProjection {
  waitId: string;
  taskId: string;
  ready: boolean;
  expired: boolean;
  cancelled: boolean;
  pendingConditionIds: string[];
  satisfiedConditionIds: string[];
  optionalConditionIds: string[];
  expiredConditionIds: string[];
  nextWakeupAt: string | null;
  evaluatedAt: string;
  digest: string;
}

function assertWaitCondition(condition: TaskWaitCondition): void {
  const { digest: checksum, ...payload } = condition;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "wait_condition_checksum",
      `wait condition ${condition.conditionId} checksum mismatch`,
    );
  if (
    !condition.key ||
    condition.requiredCount < 1 ||
    condition.observedCount < 0 ||
    condition.observedCount > condition.requiredCount ||
    condition.revision < 1
  )
    throw new E03RuntimeError(
      "wait_condition_invalid",
      `wait condition ${condition.conditionId} is invalid`,
    );
}

function resealWaitCondition(
  condition: TaskWaitCondition,
  patch: Partial<Omit<TaskWaitCondition, "conditionId" | "taskId" | "digest">>,
): TaskWaitCondition {
  const { digest: _, ...prior } = condition;
  const payload = {
    ...prior,
    ...patch,
    conditionId: condition.conditionId,
    taskId: condition.taskId,
  };
  const next = { ...payload, digest: digest(payload) };
  assertWaitCondition(next);
  return next;
}

export class TaskWaitConditionRuntime {
  private conditions = new Map<string, TaskWaitCondition>();
  private byTask = new Map<string, Set<string>>();

  register(input: {
    task: E03TaskState;
    kind: TaskWaitConditionKind;
    key: string;
    requiredCount?: number;
    optional?: boolean;
    timeoutMs?: number;
    now?: string;
  }): TaskWaitCondition {
    if (input.task.status !== "waiting" && input.task.status !== "running")
      throw new E03RuntimeError(
        "wait_condition_task_state",
        `task ${input.task.identity.taskId} cannot register wait condition from ${input.task.status}`,
      );
    const key = input.key.trim();
    if (!key)
      throw new E03RuntimeError(
        "wait_condition_key_missing",
        "wait condition key is required",
      );
    const requiredCount = input.requiredCount ?? 1;
    if (!Number.isSafeInteger(requiredCount) || requiredCount < 1)
      throw new E03RuntimeError(
        "wait_condition_count_invalid",
        "wait condition required count is invalid",
      );
    if (
      input.timeoutMs !== undefined &&
      (!Number.isSafeInteger(input.timeoutMs) || input.timeoutMs < 1)
    )
      throw new E03RuntimeError(
        "wait_condition_timeout_invalid",
        "wait condition timeout is invalid",
      );
    const createdAt = input.now ?? new Date().toISOString();
    const payload = {
      conditionId: `wait-condition-${digest({
        taskId: input.task.identity.taskId,
        leaseId: input.task.identity.leaseId,
        revision: input.task.revision,
        kind: input.kind,
        key,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      leaseId: input.task.identity.leaseId,
      expectedRevision: input.task.revision,
      kind: input.kind,
      key,
      requiredCount,
      observedCount: 0,
      optional: input.optional ?? false,
      createdAt,
      expiresAt: input.timeoutMs
        ? new Date(Date.parse(createdAt) + input.timeoutMs).toISOString()
        : null,
      satisfiedAt: null,
      satisfactionDigest: null,
      cancelledAt: null,
      cancellationReason: "",
      revision: 1,
    };
    const condition = { ...payload, digest: digest(payload) };
    assertWaitCondition(condition);
    const existing = this.conditions.get(condition.conditionId);
    if (existing) return structuredClone(existing);
    this.conditions.set(condition.conditionId, condition);
    const ids = this.byTask.get(condition.taskId) ?? new Set<string>();
    ids.add(condition.conditionId);
    this.byTask.set(condition.taskId, ids);
    return structuredClone(condition);
  }

  observe(input: {
    conditionId: string;
    task: E03TaskState;
    proof: unknown;
    increment?: number;
    now?: string;
  }): TaskWaitCondition {
    const current = this.require(input.conditionId);
    if (current.taskId !== input.task.identity.taskId)
      throw new E03RuntimeError(
        "wait_condition_task_mismatch",
        "wait condition belongs to another task",
      );
    if (current.leaseId !== input.task.identity.leaseId)
      throw new E03RuntimeError(
        "wait_condition_stale_lease",
        "wait condition uses a stale task lease",
      );
    if (current.satisfiedAt || current.cancelledAt)
      return structuredClone(current);
    const now = input.now ?? new Date().toISOString();
    if (current.expiresAt && Date.parse(now) >= Date.parse(current.expiresAt))
      throw new E03RuntimeError(
        "wait_condition_expired",
        `wait condition ${current.conditionId} expired`,
      );
    const increment = input.increment ?? 1;
    if (!Number.isSafeInteger(increment) || increment < 1)
      throw new E03RuntimeError(
        "wait_condition_increment_invalid",
        "wait condition increment is invalid",
      );
    const observedCount = Math.min(
      current.requiredCount,
      current.observedCount + increment,
    );
    const satisfied = observedCount >= current.requiredCount;
    const next = resealWaitCondition(current, {
      observedCount,
      satisfiedAt: satisfied ? now : null,
      satisfactionDigest: satisfied ? digest(input.proof) : null,
      revision: current.revision + 1,
    });
    this.conditions.set(next.conditionId, next);
    return structuredClone(next);
  }

  cancel(
    conditionId: string,
    reason: string,
    now = new Date().toISOString(),
  ): TaskWaitCondition {
    const current = this.require(conditionId);
    if (current.cancelledAt || current.satisfiedAt)
      return structuredClone(current);
    const next = resealWaitCondition(current, {
      cancelledAt: now,
      cancellationReason: reason.trim() || "wait_cancelled",
      revision: current.revision + 1,
    });
    this.conditions.set(next.conditionId, next);
    return structuredClone(next);
  }

  project(
    task: E03TaskState,
    now = new Date().toISOString(),
  ): TaskWaitProjection {
    const conditions = this.forTask(task.identity.taskId);
    const pendingConditionIds: string[] = [];
    const satisfiedConditionIds: string[] = [];
    const optionalConditionIds: string[] = [];
    const expiredConditionIds: string[] = [];
    let cancelled = false;
    for (const condition of conditions) {
      if (condition.optional) optionalConditionIds.push(condition.conditionId);
      if (condition.cancelledAt) {
        cancelled = true;
        continue;
      }
      if (condition.satisfiedAt) {
        satisfiedConditionIds.push(condition.conditionId);
        continue;
      }
      if (
        condition.expiresAt &&
        Date.parse(now) >= Date.parse(condition.expiresAt)
      ) {
        expiredConditionIds.push(condition.conditionId);
        continue;
      }
      pendingConditionIds.push(condition.conditionId);
    }
    const requiredPending = pendingConditionIds.filter(
      (conditionId) => !optionalConditionIds.includes(conditionId),
    );
    const requiredExpired = expiredConditionIds.filter(
      (conditionId) => !optionalConditionIds.includes(conditionId),
    );
    const nextWakeupAt =
      conditions
        .map((condition) => condition.expiresAt)
        .filter((value): value is string => Boolean(value))
        .filter((value) => Date.parse(value) > Date.parse(now))
        .sort()[0] ?? null;
    const payload = {
      waitId: `task-wait-${digest({
        taskId: task.identity.taskId,
        conditionDigests: conditions.map((condition) => condition.digest),
      }).slice(0, 32)}`,
      taskId: task.identity.taskId,
      ready: !cancelled && !requiredPending.length && !requiredExpired.length,
      expired: requiredExpired.length > 0,
      cancelled,
      pendingConditionIds,
      satisfiedConditionIds,
      optionalConditionIds,
      expiredConditionIds,
      nextWakeupAt,
      evaluatedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(conditions: readonly TaskWaitCondition[]): void {
    const next = new Map<string, TaskWaitCondition>();
    const byTask = new Map<string, Set<string>>();
    for (const raw of conditions) {
      const condition = structuredClone(raw);
      assertWaitCondition(condition);
      if (next.has(condition.conditionId))
        throw new E03RuntimeError(
          "duplicate_wait_condition",
          `wait condition ${condition.conditionId} repeats`,
        );
      next.set(condition.conditionId, condition);
      const ids = byTask.get(condition.taskId) ?? new Set<string>();
      ids.add(condition.conditionId);
      byTask.set(condition.taskId, ids);
    }
    this.conditions = next;
    this.byTask = byTask;
  }

  forTask(taskId: string): TaskWaitCondition[] {
    return [...(this.byTask.get(taskId) ?? new Set<string>())]
      .map((conditionId) => this.conditions.get(conditionId)!)
      .sort(
        (left, right) =>
          left.createdAt.localeCompare(right.createdAt) ||
          left.conditionId.localeCompare(right.conditionId),
      )
      .map((condition) => structuredClone(condition));
  }

  snapshot(): TaskWaitCondition[] {
    return [...this.conditions.values()]
      .sort((left, right) => left.conditionId.localeCompare(right.conditionId))
      .map((condition) => structuredClone(condition));
  }

  private require(conditionId: string): TaskWaitCondition {
    const condition = this.conditions.get(conditionId);
    if (!condition)
      throw new E03RuntimeError(
        "wait_condition_missing",
        `wait condition ${conditionId} is missing`,
      );
    assertWaitCondition(condition);
    return condition;
  }
}

export type ResultFragmentKind =
  | "text"
  | "json"
  | "artifact"
  | "evidence"
  | "diagnostic";

export interface TaskResultFragment {
  fragmentId: string;
  taskId: string;
  leaseId: string;
  attempt: number;
  expectedRevision: number;
  sequence: number;
  kind: ResultFragmentKind;
  contentType: string;
  content: string;
  contentDigest: string;
  artifactId: string | null;
  evidenceId: string | null;
  final: boolean;
  truncated: boolean;
  source: string;
  createdAt: string;
  previousDigest: string;
  digest: string;
}

export interface TaskResultAssembly {
  assemblyId: string;
  taskId: string;
  leaseId: string;
  attempt: number;
  expectedRevision: number;
  fragments: TaskResultFragment[];
  text: string;
  json: JsonObject | null;
  artifactIds: string[];
  evidenceIds: string[];
  diagnosticDigests: string[];
  totalChars: number;
  truncated: boolean;
  finalFragmentId: string | null;
  complete: boolean;
  assembledAt: string;
  digest: string;
}

function assertResultFragment(fragment: TaskResultFragment): void {
  const { digest: checksum, ...payload } = fragment;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "result_fragment_checksum",
      `result fragment ${fragment.fragmentId} checksum mismatch`,
    );
  if (
    !fragment.taskId ||
    !fragment.leaseId ||
    !fragment.source ||
    fragment.attempt < 1 ||
    fragment.expectedRevision < 1 ||
    fragment.sequence < 1
  )
    throw new E03RuntimeError(
      "result_fragment_identity",
      `result fragment ${fragment.fragmentId} identity is invalid`,
    );
  if (digest(fragment.content) !== fragment.contentDigest)
    throw new E03RuntimeError(
      "result_fragment_content_digest",
      `result fragment ${fragment.fragmentId} content digest mismatch`,
    );
  if (fragment.kind === "artifact" && !fragment.artifactId)
    throw new E03RuntimeError(
      "result_fragment_artifact_missing",
      "artifact result fragment requires an artifact id",
    );
  if (fragment.kind === "evidence" && !fragment.evidenceId)
    throw new E03RuntimeError(
      "result_fragment_evidence_missing",
      "evidence result fragment requires an evidence id",
    );
}

export class TaskResultAssembler {
  private fragments = new Map<string, TaskResultFragment[]>();
  private byId = new Map<string, TaskResultFragment>();

  append(input: {
    task: E03TaskState;
    kind: ResultFragmentKind;
    contentType: string;
    content: string;
    artifactId?: string;
    evidenceId?: string;
    final?: boolean;
    source: string;
    maximumChars?: number;
    now?: string;
  }): TaskResultFragment {
    if (isTerminal(input.task.status))
      throw new E03RuntimeError(
        "result_fragment_terminal_task",
        `cannot append result fragment to ${input.task.status} task`,
      );
    const contentType = input.contentType.trim();
    const source = input.source.trim();
    if (!contentType || !source)
      throw new E03RuntimeError(
        "result_fragment_metadata_missing",
        "result fragment content type and source are required",
      );
    const maximumChars =
      input.maximumChars ?? input.task.definition.budget.maxResultChars;
    if (!Number.isSafeInteger(maximumChars) || maximumChars < 0)
      throw new E03RuntimeError(
        "result_fragment_limit_invalid",
        "result fragment character limit is invalid",
      );
    const current = this.fragments.get(input.task.identity.taskId) ?? [];
    const last = current.at(-1);
    if (last?.final)
      throw new E03RuntimeError(
        "result_fragment_after_final",
        "cannot append a result fragment after the final fragment",
      );
    if (last && last.leaseId !== input.task.identity.leaseId)
      throw new E03RuntimeError(
        "result_fragment_stale_lease",
        "result fragment uses another task lease",
      );
    const used = current.reduce(
      (sum, fragment) => sum + fragment.content.length,
      0,
    );
    const remaining = Math.max(0, maximumChars - used);
    const content = input.content.slice(0, remaining);
    const truncated = content.length < input.content.length;
    const payload = {
      fragmentId: `result-fragment-${digest({
        taskId: input.task.identity.taskId,
        leaseId: input.task.identity.leaseId,
        sequence: current.length + 1,
        kind: input.kind,
        contentDigest: digest(content),
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      leaseId: input.task.identity.leaseId,
      attempt: input.task.identity.attempt,
      expectedRevision: input.task.revision,
      sequence: current.length + 1,
      kind: input.kind,
      contentType,
      content,
      contentDigest: digest(content),
      artifactId: input.artifactId?.trim() || null,
      evidenceId: input.evidenceId?.trim() || null,
      final: input.final ?? false,
      truncated,
      source,
      createdAt: input.now ?? new Date().toISOString(),
      previousDigest: last?.digest ?? "",
    };
    const fragment = { ...payload, digest: digest(payload) };
    assertResultFragment(fragment);
    const existing = this.byId.get(fragment.fragmentId);
    if (existing) return structuredClone(existing);
    current.push(fragment);
    this.fragments.set(fragment.taskId, current);
    this.byId.set(fragment.fragmentId, fragment);
    return structuredClone(fragment);
  }

  assemble(task: E03TaskState): TaskResultAssembly {
    const fragments = this.forTask(task.identity.taskId);
    if (
      fragments.some((fragment) => fragment.leaseId !== task.identity.leaseId)
    )
      throw new E03RuntimeError(
        "result_assembly_stale_lease",
        "result assembly contains fragments from another task lease",
      );
    const text = fragments
      .filter((fragment) => fragment.kind === "text")
      .map((fragment) => fragment.content)
      .join("");
    const jsonFragments = fragments.filter(
      (fragment) => fragment.kind === "json",
    );
    let json: JsonObject | null = null;
    if (jsonFragments.length) {
      const values = jsonFragments.map((fragment) => {
        try {
          return JSON.parse(fragment.content) as unknown;
        } catch (error) {
          throw new E03RuntimeError(
            "result_fragment_json_invalid",
            `result fragment ${fragment.fragmentId} contains invalid JSON`,
            { error: error instanceof Error ? error.message : String(error) },
          );
        }
      });
      if (
        values.some(
          (value) =>
            !value || typeof value !== "object" || Array.isArray(value),
        )
      )
        throw new E03RuntimeError(
          "result_fragment_json_object_required",
          "JSON result fragments must contain objects",
        );
      json = Object.assign({}, ...values) as JsonObject;
    }
    const finalFragments = fragments.filter((fragment) => fragment.final);
    if (finalFragments.length > 1)
      throw new E03RuntimeError(
        "duplicate_final_result_fragment",
        "result assembly contains multiple final fragments",
      );
    const payload = {
      assemblyId: `result-assembly-${digest({
        taskId: task.identity.taskId,
        leaseId: task.identity.leaseId,
        fragments: fragments.map((fragment) => fragment.digest),
      }).slice(0, 32)}`,
      taskId: task.identity.taskId,
      leaseId: task.identity.leaseId,
      attempt: task.identity.attempt,
      expectedRevision: task.revision,
      fragments,
      text,
      json,
      artifactIds: [
        ...new Set(
          fragments
            .map((fragment) => fragment.artifactId)
            .filter((value): value is string => Boolean(value)),
        ),
      ].sort(),
      evidenceIds: [
        ...new Set(
          fragments
            .map((fragment) => fragment.evidenceId)
            .filter((value): value is string => Boolean(value)),
        ),
      ].sort(),
      diagnosticDigests: fragments
        .filter((fragment) => fragment.kind === "diagnostic")
        .map((fragment) => fragment.contentDigest)
        .sort(),
      totalChars: fragments.reduce(
        (sum, fragment) => sum + fragment.content.length,
        0,
      ),
      truncated: fragments.some((fragment) => fragment.truncated),
      finalFragmentId: finalFragments[0]?.fragmentId ?? null,
      complete: finalFragments.length === 1,
      assembledAt: new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  assertComplete(task: E03TaskState): TaskResultAssembly {
    const assembly = this.assemble(task);
    if (!assembly.complete)
      throw new E03RuntimeError(
        "result_assembly_incomplete",
        `task ${task.identity.taskId} result has no final fragment`,
      );
    if (assembly.totalChars > task.definition.budget.maxResultChars)
      throw new E03RuntimeError(
        "result_assembly_budget",
        "task result exceeds its character budget",
      );
    return assembly;
  }

  restore(fragments: readonly TaskResultFragment[]): void {
    const next = new Map<string, TaskResultFragment[]>();
    const byId = new Map<string, TaskResultFragment>();
    for (const raw of fragments) {
      const fragment = structuredClone(raw);
      assertResultFragment(fragment);
      if (byId.has(fragment.fragmentId))
        throw new E03RuntimeError(
          "duplicate_result_fragment",
          `result fragment ${fragment.fragmentId} repeats`,
        );
      const current = next.get(fragment.taskId) ?? [];
      const last = current.at(-1);
      if (
        fragment.sequence !== current.length + 1 ||
        fragment.previousDigest !== (last?.digest ?? "")
      )
        throw new E03RuntimeError(
          "result_fragment_chain",
          `task ${fragment.taskId} result fragment chain is invalid`,
        );
      if (last?.final)
        throw new E03RuntimeError(
          "result_fragment_after_final",
          `task ${fragment.taskId} has a fragment after final`,
        );
      current.push(fragment);
      next.set(fragment.taskId, current);
      byId.set(fragment.fragmentId, fragment);
    }
    this.fragments = next;
    this.byId = byId;
  }

  forTask(taskId: string): TaskResultFragment[] {
    return (this.fragments.get(taskId) ?? []).map((fragment) =>
      structuredClone(fragment),
    );
  }

  snapshot(): TaskResultFragment[] {
    return [...this.fragments.values()]
      .flat()
      .sort(
        (left, right) =>
          left.taskId.localeCompare(right.taskId) ||
          left.sequence - right.sequence,
      )
      .map((fragment) => structuredClone(fragment));
  }
}

export interface TaskUsageSample {
  sampleId: string;
  taskId: string;
  leaseId: string;
  attempt: number;
  revision: number;
  source: string;
  inputTokens: number;
  outputTokens: number;
  toolCalls: number;
  resultChars: number;
  wallTimeMs: number;
  costMicros: number;
  model: string;
  provider: string;
  observedAt: string;
  digest: string;
}

export interface TaskUsageReconciliation {
  taskId: string;
  accepted: boolean;
  code: string;
  sampleCount: number;
  inputTokens: number;
  outputTokens: number;
  toolCalls: number;
  resultChars: number;
  wallTimeMs: number;
  costMicros: number;
  exceeded: string[];
  warnings: string[];
  providers: string[];
  models: string[];
  reconciledAt: string;
  digest: string;
}

function assertUsageSample(sample: TaskUsageSample): void {
  const { digest: checksum, ...payload } = sample;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "usage_sample_checksum",
      `usage sample ${sample.sampleId} checksum mismatch`,
    );
  for (const key of [
    "inputTokens",
    "outputTokens",
    "toolCalls",
    "resultChars",
    "wallTimeMs",
    "costMicros",
  ] as const)
    executionCounter(sample[key], key);
}

export class TaskUsageReconciliationRuntime {
  private samples = new Map<string, TaskUsageSample[]>();

  record(input: Omit<TaskUsageSample, "sampleId" | "digest">): TaskUsageSample {
    const payload = {
      ...structuredClone(input),
      sampleId: `usage-sample-${digest({
        taskId: input.taskId,
        leaseId: input.leaseId,
        revision: input.revision,
        source: input.source,
        observedAt: input.observedAt,
      }).slice(0, 32)}`,
    };
    const sample = { ...payload, digest: digest(payload) };
    assertUsageSample(sample);
    const current = this.samples.get(sample.taskId) ?? [];
    const existing = current.find(
      (value) => value.sampleId === sample.sampleId,
    );
    if (existing) return structuredClone(existing);
    const last = current.at(-1);
    if (last && last.leaseId !== sample.leaseId)
      throw new E03RuntimeError(
        "usage_sample_stale_lease",
        "usage sample uses another task lease",
      );
    if (last && last.revision > sample.revision)
      throw new E03RuntimeError(
        "usage_sample_stale_revision",
        "usage sample revision is behind prior sample",
      );
    current.push(sample);
    this.samples.set(sample.taskId, current);
    return structuredClone(sample);
  }

  reconcile(task: E03TaskState): TaskUsageReconciliation {
    const samples = this.forTask(task.identity.taskId);
    if (samples.some((sample) => sample.leaseId !== task.identity.leaseId))
      throw new E03RuntimeError(
        "usage_reconcile_stale_lease",
        "usage samples contain another task lease",
      );
    const totals = {
      inputTokens: samples.reduce((sum, value) => sum + value.inputTokens, 0),
      outputTokens: samples.reduce((sum, value) => sum + value.outputTokens, 0),
      toolCalls: samples.reduce((sum, value) => sum + value.toolCalls, 0),
      resultChars: samples.reduce((sum, value) => sum + value.resultChars, 0),
      wallTimeMs: samples.reduce((sum, value) => sum + value.wallTimeMs, 0),
      costMicros: samples.reduce((sum, value) => sum + value.costMicros, 0),
    };
    const budget = task.definition.budget;
    const checks: Array<[string, number, number]> = [
      ["inputTokens", totals.inputTokens, budget.maxInputTokens],
      ["outputTokens", totals.outputTokens, budget.maxOutputTokens],
      ["toolCalls", totals.toolCalls, budget.maxToolCalls],
      ["resultChars", totals.resultChars, budget.maxResultChars],
      ["wallTimeMs", totals.wallTimeMs, budget.maxWallTimeMs],
    ];
    const exceeded = checks
      .filter(([, value, maximum]) => value > maximum)
      .map(([name]) => name);
    const warnings = checks
      .filter(
        ([, value, maximum]) =>
          maximum > 0 && value <= maximum && value / maximum >= 0.8,
      )
      .map(([name]) => name);
    const payload = {
      taskId: task.identity.taskId,
      accepted: exceeded.length === 0,
      code: exceeded.length ? "task_usage_exceeded" : "task_usage_accepted",
      sampleCount: samples.length,
      ...totals,
      exceeded,
      warnings,
      providers: [
        ...new Set(samples.map((sample) => sample.provider).filter(Boolean)),
      ].sort(),
      models: [
        ...new Set(samples.map((sample) => sample.model).filter(Boolean)),
      ].sort(),
      reconciledAt: new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(task: E03TaskState): TaskUsageReconciliation {
    const reconciliation = this.reconcile(task);
    if (!reconciliation.accepted)
      throw new E03RuntimeError(
        reconciliation.code,
        `task ${task.identity.taskId} exceeded usage budget`,
        { exceeded: reconciliation.exceeded },
      );
    return reconciliation;
  }

  restore(samples: readonly TaskUsageSample[]): void {
    const next = new Map<string, TaskUsageSample[]>();
    const ids = new Set<string>();
    for (const raw of samples) {
      const sample = structuredClone(raw);
      assertUsageSample(sample);
      if (ids.has(sample.sampleId))
        throw new E03RuntimeError(
          "duplicate_usage_sample",
          `usage sample ${sample.sampleId} repeats`,
        );
      ids.add(sample.sampleId);
      const current = next.get(sample.taskId) ?? [];
      const last = current.at(-1);
      if (last && last.leaseId !== sample.leaseId)
        throw new E03RuntimeError(
          "usage_sample_stale_lease",
          `task ${sample.taskId} usage sample lease changed`,
        );
      current.push(sample);
      next.set(sample.taskId, current);
    }
    this.samples = next;
  }

  forTask(taskId: string): TaskUsageSample[] {
    return (this.samples.get(taskId) ?? []).map((sample) =>
      structuredClone(sample),
    );
  }

  snapshot(): TaskUsageSample[] {
    return [...this.samples.values()]
      .flat()
      .sort(
        (left, right) =>
          left.observedAt.localeCompare(right.observedAt) ||
          left.sampleId.localeCompare(right.sampleId),
      )
      .map((sample) => structuredClone(sample));
  }
}

export type ExecutionAttemptState =
  | "scheduled"
  | "starting"
  | "running"
  | "waiting"
  | "settling"
  | "succeeded"
  | "failed"
  | "timed-out"
  | "cancelled"
  | "superseded";

export interface TaskExecutionAttempt {
  executionAttemptId: string;
  taskId: string;
  leaseId: string;
  taskAttempt: number;
  executionNumber: number;
  state: ExecutionAttemptState;
  workerId: string | null;
  providerId: string | null;
  modelId: string | null;
  scheduledAt: string;
  notBefore: string;
  deadlineAt: string;
  startedAt: string | null;
  waitingAt: string | null;
  settlingAt: string | null;
  finishedAt: string | null;
  lastHeartbeatAt: string | null;
  failureCode: string | null;
  failureMessage: string | null;
  retryOfAttemptId: string | null;
  revision: number;
  digest: string;
}

export interface TaskExecutionAttemptDecision {
  decisionId: string;
  executionAttemptId: string;
  taskId: string;
  action: "start" | "wait" | "cancel" | "retry" | "reject";
  reason: string;
  retryAt: string | null;
  expectedTaskRevision: number;
  expectedAttemptRevision: number;
  decidedAt: string;
  digest: string;
}

function terminalExecutionAttempt(state: ExecutionAttemptState): boolean {
  return [
    "succeeded",
    "failed",
    "timed-out",
    "cancelled",
    "superseded",
  ].includes(state);
}

function assertTaskExecutionAttempt(attempt: TaskExecutionAttempt): void {
  const { digest: checksum, ...payload } = attempt;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_execution_attempt_digest",
      `task execution attempt ${attempt.executionAttemptId} digest is invalid`,
    );
  if (!attempt.executionAttemptId || !attempt.taskId || !attempt.leaseId)
    throw new E03RuntimeError(
      "task_execution_attempt_identity",
      "task execution attempt identity is incomplete",
    );
  for (const value of [attempt.taskAttempt, attempt.executionNumber])
    if (!Number.isSafeInteger(value) || value < 1)
      throw new E03RuntimeError(
        "task_execution_attempt_number",
        "task execution attempt number is invalid",
      );
  if (Date.parse(attempt.notBefore) > Date.parse(attempt.deadlineAt))
    throw new E03RuntimeError(
      "task_execution_attempt_interval",
      "task execution attempt starts after its deadline",
    );
  if (!Number.isSafeInteger(attempt.revision) || attempt.revision < 1)
    throw new E03RuntimeError(
      "task_execution_attempt_revision",
      "task execution attempt revision is invalid",
    );
  if (attempt.state === "running" && (!attempt.workerId || !attempt.startedAt))
    throw new E03RuntimeError(
      "task_execution_attempt_state",
      "running task execution attempt requires worker and start time",
    );
  if (terminalExecutionAttempt(attempt.state) && attempt.finishedAt === null)
    throw new E03RuntimeError(
      "task_execution_attempt_state",
      "terminal task execution attempt requires finish time",
    );
  if (
    ["failed", "timed-out"].includes(attempt.state) &&
    (!attempt.failureCode || !attempt.failureMessage)
  )
    throw new E03RuntimeError(
      "task_execution_attempt_failure",
      "failed task execution attempt requires failure metadata",
    );
}

export class TaskExecutionAttemptRuntime {
  private attempts = new Map<string, TaskExecutionAttempt>();
  private decisions = new Map<string, TaskExecutionAttemptDecision>();
  private byTask = new Map<string, string[]>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  schedule(input: {
    task: E03TaskState;
    notBefore?: string;
    deadlineAt?: string;
    retryOfAttemptId?: string | null;
  }): TaskExecutionAttempt {
    if (isTerminal(input.task.status))
      throw new E03RuntimeError(
        "task_execution_attempt_terminal",
        `cannot schedule terminal task ${input.task.identity.taskId}`,
      );
    const priorIds = this.byTask.get(input.task.identity.taskId) ?? [];
    const active = priorIds
      .map((attemptId) => this.requireAttempt(attemptId))
      .find((attempt) => !terminalExecutionAttempt(attempt.state));
    if (active)
      throw new E03RuntimeError(
        "task_execution_attempt_active",
        `task already has active execution attempt ${active.executionAttemptId}`,
      );
    let retryOf: TaskExecutionAttempt | null = null;
    if (input.retryOfAttemptId) {
      retryOf = this.requireAttempt(input.retryOfAttemptId);
      if (
        retryOf.taskId !== input.task.identity.taskId ||
        !terminalExecutionAttempt(retryOf.state)
      )
        throw new E03RuntimeError(
          "task_execution_attempt_retry_custody",
          "retry attempt does not reference a terminal attempt for the same task",
        );
    }
    const now = this.clock.now();
    const payload = {
      executionAttemptId: createId("task-execution-attempt"),
      taskId: input.task.identity.taskId,
      leaseId: input.task.identity.leaseId,
      taskAttempt: input.task.identity.attempt,
      executionNumber: priorIds.length + 1,
      state: "scheduled" as const,
      workerId: null,
      providerId: null,
      modelId: null,
      scheduledAt: now,
      notBefore: input.notBefore ?? now,
      deadlineAt: input.deadlineAt ?? input.task.definition.budget.deadlineAt,
      startedAt: null,
      waitingAt: null,
      settlingAt: null,
      finishedAt: null,
      lastHeartbeatAt: null,
      failureCode: null,
      failureMessage: null,
      retryOfAttemptId: retryOf?.executionAttemptId ?? null,
      revision: 1,
    };
    const attempt = { ...payload, digest: digest(payload) };
    assertTaskExecutionAttempt(attempt);
    this.attempts.set(attempt.executionAttemptId, attempt);
    this.byTask.set(attempt.taskId, [...priorIds, attempt.executionAttemptId]);
    return structuredClone(attempt);
  }

  decide(attemptId: string, task: E03TaskState): TaskExecutionAttemptDecision {
    const attempt = this.requireAttempt(attemptId);
    let action: TaskExecutionAttemptDecision["action"] = "start";
    let reason = "attempt is ready";
    let retryAt: string | null = null;
    const now = this.clock.now();
    if (isTerminal(task.status)) {
      action = "cancel";
      reason = `task is ${task.status}`;
    } else if (
      task.identity.taskId !== attempt.taskId ||
      task.identity.leaseId !== attempt.leaseId ||
      task.identity.attempt !== attempt.taskAttempt
    ) {
      action = "reject";
      reason = "task identity no longer matches attempt";
    } else if (terminalExecutionAttempt(attempt.state)) {
      action =
        attempt.state === "failed" || attempt.state === "timed-out"
          ? "retry"
          : "reject";
      reason = `attempt is ${attempt.state}`;
    } else if (Date.parse(now) < Date.parse(attempt.notBefore)) {
      action = "wait";
      reason = "attempt not-before time has not arrived";
      retryAt = attempt.notBefore;
    } else if (Date.parse(now) >= Date.parse(attempt.deadlineAt)) {
      action = "cancel";
      reason = "attempt deadline elapsed";
    }
    const payload = {
      decisionId: createId("task-execution-decision"),
      executionAttemptId: attempt.executionAttemptId,
      taskId: attempt.taskId,
      action,
      reason,
      retryAt,
      expectedTaskRevision: task.revision,
      expectedAttemptRevision: attempt.revision,
      decidedAt: now,
    };
    const decision = { ...payload, digest: digest(payload) };
    this.decisions.set(decision.decisionId, decision);
    return structuredClone(decision);
  }

  start(input: {
    attemptId: string;
    expectedRevision: number;
    workerId: string;
    providerId?: string | null;
    modelId?: string | null;
  }): TaskExecutionAttempt {
    let attempt = this.requireAttempt(input.attemptId);
    this.assertRevision(attempt, input.expectedRevision);
    if (attempt.state !== "scheduled" && attempt.state !== "starting")
      throw new E03RuntimeError(
        "task_execution_attempt_start_state",
        `task execution attempt ${attempt.executionAttemptId} is ${attempt.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) < Date.parse(attempt.notBefore))
      throw new E03RuntimeError(
        "task_execution_attempt_too_early",
        `task execution attempt ${attempt.executionAttemptId} is not ready`,
      );
    if (Date.parse(now) >= Date.parse(attempt.deadlineAt))
      return this.transition(attempt, {
        state: "timed-out",
        finishedAt: now,
        failureCode: "deadline_elapsed",
        failureMessage: "attempt deadline elapsed before start",
      });
    if (attempt.state === "scheduled")
      attempt = this.transition(attempt, { state: "starting" });
    return this.transition(attempt, {
      state: "running",
      workerId: input.workerId.trim(),
      providerId: input.providerId?.trim() || null,
      modelId: input.modelId?.trim() || null,
      startedAt: now,
      lastHeartbeatAt: now,
    });
  }

  heartbeat(
    attemptId: string,
    expectedRevision: number,
    state: "running" | "waiting",
  ): TaskExecutionAttempt {
    const attempt = this.requireAttempt(attemptId);
    this.assertRevision(attempt, expectedRevision);
    if (attempt.state !== "running" && attempt.state !== "waiting")
      throw new E03RuntimeError(
        "task_execution_attempt_heartbeat_state",
        `task execution attempt ${attemptId} is ${attempt.state}`,
      );
    return this.transition(attempt, {
      state,
      waitingAt: state === "waiting" ? this.clock.now() : attempt.waitingAt,
      lastHeartbeatAt: this.clock.now(),
    });
  }

  settle(attemptId: string, expectedRevision: number): TaskExecutionAttempt {
    const attempt = this.requireAttempt(attemptId);
    this.assertRevision(attempt, expectedRevision);
    if (attempt.state !== "running" && attempt.state !== "waiting")
      throw new E03RuntimeError(
        "task_execution_attempt_settle_state",
        `task execution attempt ${attemptId} is ${attempt.state}`,
      );
    return this.transition(attempt, {
      state: "settling",
      settlingAt: this.clock.now(),
    });
  }

  complete(attemptId: string, expectedRevision: number): TaskExecutionAttempt {
    const attempt = this.requireAttempt(attemptId);
    this.assertRevision(attempt, expectedRevision);
    if (attempt.state !== "settling")
      throw new E03RuntimeError(
        "task_execution_attempt_complete_state",
        `task execution attempt ${attemptId} is ${attempt.state}`,
      );
    return this.transition(attempt, {
      state: "succeeded",
      finishedAt: this.clock.now(),
    });
  }

  fail(input: {
    attemptId: string;
    expectedRevision: number;
    code: string;
    message: string;
    timedOut?: boolean;
  }): TaskExecutionAttempt {
    const attempt = this.requireAttempt(input.attemptId);
    this.assertRevision(attempt, input.expectedRevision);
    if (terminalExecutionAttempt(attempt.state))
      throw new E03RuntimeError(
        "task_execution_attempt_fail_state",
        `task execution attempt ${attempt.executionAttemptId} is terminal`,
      );
    if (!input.code.trim() || !input.message.trim())
      throw new E03RuntimeError(
        "task_execution_attempt_failure_missing",
        "task execution failure requires code and message",
      );
    return this.transition(attempt, {
      state: input.timedOut ? "timed-out" : "failed",
      finishedAt: this.clock.now(),
      failureCode: input.code.trim(),
      failureMessage: input.message.trim(),
    });
  }

  cancel(attemptId: string, expectedRevision: number): TaskExecutionAttempt {
    const attempt = this.requireAttempt(attemptId);
    this.assertRevision(attempt, expectedRevision);
    if (terminalExecutionAttempt(attempt.state))
      return structuredClone(attempt);
    return this.transition(attempt, {
      state: "cancelled",
      finishedAt: this.clock.now(),
    });
  }

  sweep(input: {
    now?: string;
    heartbeatTimeoutMs: number;
  }): TaskExecutionAttempt[] {
    if (
      !Number.isSafeInteger(input.heartbeatTimeoutMs) ||
      input.heartbeatTimeoutMs < 1
    )
      throw new E03RuntimeError(
        "task_execution_attempt_heartbeat_timeout",
        "task execution heartbeat timeout is invalid",
      );
    const now = input.now ?? this.clock.now();
    const failed: TaskExecutionAttempt[] = [];
    for (const attempt of this.attempts.values()) {
      if (terminalExecutionAttempt(attempt.state)) continue;
      if (Date.parse(now) >= Date.parse(attempt.deadlineAt)) {
        failed.push(
          this.transition(attempt, {
            state: "timed-out",
            finishedAt: now,
            failureCode: "deadline_elapsed",
            failureMessage: "task execution deadline elapsed",
          }),
        );
      } else if (
        attempt.lastHeartbeatAt &&
        Date.parse(now) - Date.parse(attempt.lastHeartbeatAt) >=
          input.heartbeatTimeoutMs
      )
        failed.push(
          this.transition(attempt, {
            state: "failed",
            finishedAt: now,
            failureCode: "heartbeat_lost",
            failureMessage: "task execution heartbeat was lost",
          }),
        );
    }
    return failed;
  }

  latest(taskId: string): TaskExecutionAttempt | null {
    const ids = this.byTask.get(taskId) ?? [];
    return ids.length
      ? structuredClone(this.requireAttempt(ids[ids.length - 1]!))
      : null;
  }

  snapshot(): {
    attempts: TaskExecutionAttempt[];
    decisions: TaskExecutionAttemptDecision[];
  } {
    return {
      attempts: [...this.attempts.values()].map((value) =>
        structuredClone(value),
      ),
      decisions: [...this.decisions.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    attempts: readonly TaskExecutionAttempt[];
    decisions: readonly TaskExecutionAttemptDecision[];
  }): void {
    const attempts = new Map<string, TaskExecutionAttempt>();
    const decisions = new Map<string, TaskExecutionAttemptDecision>();
    const byTask = new Map<string, string[]>();
    for (const attempt of input.attempts) {
      assertTaskExecutionAttempt(attempt);
      if (attempts.has(attempt.executionAttemptId))
        throw new E03RuntimeError(
          "task_execution_attempt_restore_duplicate",
          `duplicate task execution attempt ${attempt.executionAttemptId}`,
        );
      attempts.set(attempt.executionAttemptId, structuredClone(attempt));
      byTask.set(attempt.taskId, [
        ...(byTask.get(attempt.taskId) ?? []),
        attempt.executionAttemptId,
      ]);
    }
    for (const ids of byTask.values()) {
      ids.sort(
        (leftId, rightId) =>
          attempts.get(leftId)!.executionNumber -
          attempts.get(rightId)!.executionNumber,
      );
      const active = ids
        .map((id) => attempts.get(id)!)
        .filter((attempt) => !terminalExecutionAttempt(attempt.state));
      if (active.length > 1)
        throw new E03RuntimeError(
          "task_execution_attempt_restore_multiple_active",
          `task ${active[0]!.taskId} has multiple active execution attempts`,
        );
    }
    for (const decision of input.decisions) {
      const { digest: checksum, ...payload } = decision;
      if (digest(payload) !== checksum)
        throw new E03RuntimeError(
          "task_execution_decision_restore_digest",
          `task execution decision ${decision.decisionId} digest is invalid`,
        );
      const attempt = attempts.get(decision.executionAttemptId);
      if (!attempt || attempt.taskId !== decision.taskId)
        throw new E03RuntimeError(
          "task_execution_decision_restore_attempt",
          `task execution decision ${decision.decisionId} has invalid attempt`,
        );
      if (decisions.has(decision.decisionId))
        throw new E03RuntimeError(
          "task_execution_decision_restore_duplicate",
          `duplicate task execution decision ${decision.decisionId}`,
        );
      decisions.set(decision.decisionId, structuredClone(decision));
    }
    this.attempts = attempts;
    this.decisions = decisions;
    this.byTask = byTask;
  }

  private requireAttempt(attemptId: string): TaskExecutionAttempt {
    const attempt = this.attempts.get(attemptId);
    if (!attempt)
      throw new E03RuntimeError(
        "task_execution_attempt_missing",
        `task execution attempt ${attemptId} does not exist`,
      );
    assertTaskExecutionAttempt(attempt);
    return attempt;
  }

  private assertRevision(
    attempt: TaskExecutionAttempt,
    expected: number,
  ): void {
    if (attempt.revision !== expected)
      throw new E03RuntimeError(
        "task_execution_attempt_stale_revision",
        `task execution attempt ${attempt.executionAttemptId} revision is stale`,
      );
  }

  private transition(
    attempt: TaskExecutionAttempt,
    patch: Partial<
      Omit<TaskExecutionAttempt, "executionAttemptId" | "revision" | "digest">
    >,
  ): TaskExecutionAttempt {
    const { digest: _, ...prior } = attempt;
    const payload = {
      ...prior,
      ...patch,
      executionAttemptId: attempt.executionAttemptId,
      revision: attempt.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskExecutionAttempt(next);
    this.attempts.set(next.executionAttemptId, next);
    return structuredClone(next);
  }
}

export interface TaskOutputVerificationPolicy {
  policyId: string;
  name: string;
  requiredChecks: string[];
  optionalChecks: string[];
  minimumOptionalPasses: number;
  rejectOnWarning: boolean;
  maximumArtifactBytes: number;
  allowedMediaTypes: string[];
  state: "active" | "disabled" | "retired";
  revision: number;
  digest: string;
}
export interface TaskOutputVerificationCheck {
  checkId: string;
  reportId: string;
  name: string;
  required: boolean;
  outcome: "passed" | "failed" | "warning" | "skipped";
  summary: string;
  evidence: JsonObject;
  checkedAt: string;
  digest: string;
}
export interface TaskOutputVerificationReport {
  reportId: string;
  taskId: string;
  attempt: number;
  policyId: string;
  state: "open" | "verified" | "rejected" | "cancelled";
  resultDigest: string;
  artifactIds: string[];
  checkIds: string[];
  requiredPassed: number;
  requiredFailed: number;
  optionalPassed: number;
  warningCount: number;
  openedAt: string;
  completedAt: string | null;
  rejectionReason: string | null;
  revision: number;
  digest: string;
}
function assertOutputPolicy(value: TaskOutputVerificationPolicy): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_output_policy_digest",
      `task output policy ${value.policyId} is corrupt`,
    );
  if (
    !value.policyId ||
    !value.name ||
    !value.requiredChecks.length ||
    !Number.isSafeInteger(value.minimumOptionalPasses) ||
    value.minimumOptionalPasses < 0 ||
    value.minimumOptionalPasses > value.optionalChecks.length ||
    !Number.isSafeInteger(value.maximumArtifactBytes) ||
    value.maximumArtifactBytes < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "task_output_policy",
      `task output policy ${value.policyId} is invalid`,
    );
}
function assertOutputCheck(value: TaskOutputVerificationCheck): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_output_check_digest",
      `task output check ${value.checkId} is corrupt`,
    );
  if (!value.checkId || !value.reportId || !value.name || !value.summary)
    throw new E03RuntimeError(
      "task_output_check",
      `task output check ${value.checkId} is invalid`,
    );
}
function assertOutputReport(value: TaskOutputVerificationReport): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_output_report_digest",
      `task output report ${value.reportId} is corrupt`,
    );
  if (
    !value.reportId ||
    !value.taskId ||
    !value.policyId ||
    !value.resultDigest ||
    !Number.isSafeInteger(value.attempt) ||
    value.attempt < 1 ||
    [
      value.requiredPassed,
      value.requiredFailed,
      value.optionalPassed,
      value.warningCount,
    ].some((number) => !Number.isSafeInteger(number) || number < 0) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "task_output_report",
      `task output report ${value.reportId} is invalid`,
    );
  if (
    (value.state === "verified" ||
      value.state === "rejected" ||
      value.state === "cancelled") &&
    value.completedAt === null
  )
    throw new E03RuntimeError(
      "task_output_report_completion",
      `completed task output report ${value.reportId} lacks time`,
    );
}
export interface TaskExecutionPlacement {
  placementId: string;
  taskId: string;
  leaseId: string;
  attempt: number;
  hostId: string;
  region: string;
  runtimeKind: "local" | "edge" | "cloud";
  requiredCapabilities: string[];
  state:
    | "reserved"
    | "starting"
    | "running"
    | "migrating"
    | "released"
    | "failed";
  fencingToken: number;
  reservedAt: string;
  startedAt: string;
  releasedAt: string;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface TaskExecutionHostCapacity {
  hostId: string;
  region: string;
  runtimeKind: TaskExecutionPlacement["runtimeKind"];
  capabilities: string[];
  maximumPlacements: number;
  activePlacements: number;
  healthy: boolean;
  draining: boolean;
  leaseExpiresAt: string;
  lastHeartbeatAt: string;
  revision: number;
  digest: string;
}

export interface TaskPlacementMigration {
  migrationId: string;
  placementId: string;
  taskId: string;
  sourceHostId: string;
  targetHostId: string;
  checkpointDigest: string;
  idempotencyKey: string;
  state:
    | "prepared"
    | "checkpointed"
    | "restoring"
    | "committed"
    | "rolled_back"
    | "failed";
  targetFencingToken: number;
  preparedAt: string;
  updatedAt: string;
  committedAt: string;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface TaskPlacementSnapshot {
  hosts: TaskExecutionHostCapacity[];
  placements: TaskExecutionPlacement[];
  migrations: TaskPlacementMigration[];
  activePlacementByTask: [string, string][];
  migrationByIdempotencyKey: [string, string][];
  nextFenceByTask: [string, number][];
}

function assertTaskExecutionPlacement(value: TaskExecutionPlacement): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.placementId ||
    !value.taskId ||
    !value.leaseId ||
    value.attempt < 1 ||
    !value.hostId ||
    !value.region ||
    value.fencingToken < 1 ||
    new Set(value.requiredCapabilities).size !==
      value.requiredCapabilities.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_execution_placement_corrupt",
      `task execution placement ${value.placementId || "<empty>"} is corrupt`,
    );
}

function assertTaskHostCapacity(value: TaskExecutionHostCapacity): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.hostId ||
    !value.region ||
    value.maximumPlacements < 1 ||
    value.activePlacements < 0 ||
    value.activePlacements > value.maximumPlacements ||
    new Set(value.capabilities).size !== value.capabilities.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_execution_host_capacity_corrupt",
      `task execution host ${value.hostId || "<empty>"} is corrupt`,
    );
}

function assertTaskPlacementMigration(value: TaskPlacementMigration): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.migrationId ||
    !value.placementId ||
    !value.taskId ||
    !value.sourceHostId ||
    !value.targetHostId ||
    value.sourceHostId === value.targetHostId ||
    !value.idempotencyKey ||
    value.targetFencingToken < 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "task_placement_migration_corrupt",
      `task placement migration ${value.migrationId || "<empty>"} is corrupt`,
    );
}

export class TaskExecutionPlacementRuntime {
  private hosts = new Map<string, TaskExecutionHostCapacity>();
  private placements = new Map<string, TaskExecutionPlacement>();
  private migrations = new Map<string, TaskPlacementMigration>();
  private activePlacementByTask = new Map<string, string>();
  private migrationByIdempotencyKey = new Map<string, string>();
  private nextFenceByTask = new Map<string, number>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  registerHost(input: {
    hostId: string;
    region: string;
    runtimeKind: TaskExecutionHostCapacity["runtimeKind"];
    capabilities: readonly string[];
    maximumPlacements: number;
    leaseExpiresAt: string;
  }): TaskExecutionHostCapacity {
    if (this.hosts.has(input.hostId))
      throw new E03RuntimeError(
        "task_execution_host_duplicate",
        `task execution host ${input.hostId} already exists`,
      );
    const payload = {
      hostId: input.hostId,
      region: input.region,
      runtimeKind: input.runtimeKind,
      capabilities: [...new Set(input.capabilities)].sort(),
      maximumPlacements: input.maximumPlacements,
      activePlacements: 0,
      healthy: true,
      draining: false,
      leaseExpiresAt: input.leaseExpiresAt,
      lastHeartbeatAt: this.clock.now(),
      revision: 1,
    };
    const host = { ...payload, digest: digest(payload) };
    assertTaskHostCapacity(host);
    this.hosts.set(host.hostId, host);
    return structuredClone(host);
  }

  heartbeat(
    hostId: string,
    expectedRevision: number,
    input: { leaseExpiresAt: string; healthy: boolean },
  ): TaskExecutionHostCapacity {
    const host = this.requireHost(hostId);
    this.assertHostRevision(host, expectedRevision);
    if (host.draining && input.healthy)
      throw new E03RuntimeError(
        "task_execution_host_draining_heartbeat",
        `draining host ${hostId} cannot advertise healthy`,
      );
    return this.transitionHost(host, {
      leaseExpiresAt: input.leaseExpiresAt,
      healthy: input.healthy,
      lastHeartbeatAt: this.clock.now(),
    });
  }

  reserve(input: {
    placementId?: string;
    taskId: string;
    leaseId: string;
    attempt: number;
    requiredCapabilities: readonly string[];
    preferredRegion?: string;
    runtimeKinds?: readonly TaskExecutionPlacement["runtimeKind"][];
  }): TaskExecutionPlacement {
    const activeId = this.activePlacementByTask.get(input.taskId);
    if (activeId) return structuredClone(this.requirePlacement(activeId));
    const requiredCapabilities = [
      ...new Set(input.requiredCapabilities),
    ].sort();
    const allowedKinds = input.runtimeKinds ?? ["local", "edge", "cloud"];
    const now = this.clock.now();
    const host = [...this.hosts.values()]
      .filter(
        (value) =>
          value.healthy &&
          !value.draining &&
          Date.parse(value.leaseExpiresAt) > Date.parse(now) &&
          value.activePlacements < value.maximumPlacements &&
          allowedKinds.includes(value.runtimeKind) &&
          requiredCapabilities.every((capability) =>
            value.capabilities.includes(capability),
          ),
      )
      .sort(
        (a, b) =>
          Number(b.region === input.preferredRegion) -
            Number(a.region === input.preferredRegion) ||
          a.activePlacements / a.maximumPlacements -
            b.activePlacements / b.maximumPlacements ||
          a.hostId.localeCompare(b.hostId),
      )[0];
    if (!host)
      throw new E03RuntimeError(
        "task_execution_placement_unavailable",
        `task ${input.taskId} has no execution placement`,
      );
    const fencingToken = (this.nextFenceByTask.get(input.taskId) ?? 0) + 1;
    const placementId =
      input.placementId ?? createId("task-execution-placement");
    const payload = {
      placementId,
      taskId: input.taskId,
      leaseId: input.leaseId,
      attempt: input.attempt,
      hostId: host.hostId,
      region: host.region,
      runtimeKind: host.runtimeKind,
      requiredCapabilities,
      state: "reserved" as const,
      fencingToken,
      reservedAt: now,
      startedAt: "",
      releasedAt: "",
      errorCode: "",
      revision: 1,
    };
    const placement = { ...payload, digest: digest(payload) };
    assertTaskExecutionPlacement(placement);
    this.placements.set(placementId, placement);
    this.activePlacementByTask.set(input.taskId, placementId);
    this.nextFenceByTask.set(input.taskId, fencingToken);
    this.transitionHost(host, { activePlacements: host.activePlacements + 1 });
    return structuredClone(placement);
  }

  start(
    placementId: string,
    expectedRevision: number,
    fencingToken: number,
  ): TaskExecutionPlacement {
    const placement = this.requirePlacement(placementId);
    this.assertPlacementRevision(placement, expectedRevision);
    if (
      placement.state !== "reserved" ||
      placement.fencingToken !== fencingToken
    )
      throw new E03RuntimeError(
        "task_execution_placement_start_fence",
        `task execution placement ${placementId} fence is invalid`,
      );
    const host = this.requireHost(placement.hostId);
    if (!host.healthy || host.draining)
      throw new E03RuntimeError(
        "task_execution_placement_host_unhealthy",
        `task execution host ${host.hostId} is unavailable`,
      );
    return this.transitionPlacement(placement, {
      state: "running",
      startedAt: this.clock.now(),
    });
  }

  prepareMigration(input: {
    migrationId?: string;
    placementId: string;
    expectedRevision: number;
    targetHostId: string;
    idempotencyKey: string;
  }): TaskPlacementMigration {
    const duplicateId = this.migrationByIdempotencyKey.get(
      input.idempotencyKey,
    );
    if (duplicateId) return structuredClone(this.requireMigration(duplicateId));
    const placement = this.requirePlacement(input.placementId);
    this.assertPlacementRevision(placement, input.expectedRevision);
    if (placement.state !== "running")
      throw new E03RuntimeError(
        "task_placement_migration_source_state",
        `task execution placement ${placement.placementId} is ${placement.state}`,
      );
    const target = this.requireHost(input.targetHostId);
    if (
      target.hostId === placement.hostId ||
      !target.healthy ||
      target.draining ||
      target.activePlacements >= target.maximumPlacements ||
      target.runtimeKind !== placement.runtimeKind ||
      placement.requiredCapabilities.some(
        (value) => !target.capabilities.includes(value),
      )
    )
      throw new E03RuntimeError(
        "task_placement_migration_target_invalid",
        `task placement migration target ${target.hostId} is invalid`,
      );
    const targetFencingToken =
      (this.nextFenceByTask.get(placement.taskId) ?? 0) + 1;
    const migrationId =
      input.migrationId ?? createId("task-placement-migration");
    const now = this.clock.now();
    const payload = {
      migrationId,
      placementId: placement.placementId,
      taskId: placement.taskId,
      sourceHostId: placement.hostId,
      targetHostId: target.hostId,
      checkpointDigest: "",
      idempotencyKey: input.idempotencyKey,
      state: "prepared" as const,
      targetFencingToken,
      preparedAt: now,
      updatedAt: now,
      committedAt: "",
      errorCode: "",
      revision: 1,
    };
    const migration = { ...payload, digest: digest(payload) };
    assertTaskPlacementMigration(migration);
    this.migrations.set(migrationId, migration);
    this.migrationByIdempotencyKey.set(input.idempotencyKey, migrationId);
    this.transitionPlacement(placement, { state: "migrating" });
    this.transitionHost(target, {
      activePlacements: target.activePlacements + 1,
    });
    return structuredClone(migration);
  }

  checkpoint(
    migrationId: string,
    expectedRevision: number,
    checkpointDigest: string,
  ): TaskPlacementMigration {
    const migration = this.requireMigration(migrationId);
    this.assertMigrationRevision(migration, expectedRevision);
    if (migration.state !== "prepared" || !checkpointDigest)
      throw new E03RuntimeError(
        "task_placement_migration_checkpoint_state",
        `task placement migration ${migrationId} cannot checkpoint`,
      );
    return this.transitionMigration(migration, {
      state: "checkpointed",
      checkpointDigest,
    });
  }

  restoreTarget(
    migrationId: string,
    expectedRevision: number,
    fencingToken: number,
  ): TaskPlacementMigration {
    const migration = this.requireMigration(migrationId);
    this.assertMigrationRevision(migration, expectedRevision);
    if (
      migration.state !== "checkpointed" ||
      migration.targetFencingToken !== fencingToken
    )
      throw new E03RuntimeError(
        "task_placement_migration_restore_fence",
        `task placement migration ${migrationId} fence is invalid`,
      );
    return this.transitionMigration(migration, { state: "restoring" });
  }

  commitMigration(
    migrationId: string,
    expectedRevision: number,
  ): { migration: TaskPlacementMigration; placement: TaskExecutionPlacement } {
    const migration = this.requireMigration(migrationId);
    this.assertMigrationRevision(migration, expectedRevision);
    if (migration.state !== "restoring")
      throw new E03RuntimeError(
        "task_placement_migration_commit_state",
        `task placement migration ${migrationId} is ${migration.state}`,
      );
    const placement = this.requirePlacement(migration.placementId);
    const source = this.requireHost(migration.sourceHostId);
    const target = this.requireHost(migration.targetHostId);
    if (source.activePlacements < 1)
      throw new E03RuntimeError(
        "task_placement_migration_source_underflow",
        `task execution host ${source.hostId} has no placement`,
      );
    const committedAt = this.clock.now();
    const nextMigration = this.transitionMigration(migration, {
      state: "committed",
      committedAt,
    });
    const nextPlacement = this.transitionPlacement(placement, {
      state: "running",
      hostId: target.hostId,
      region: target.region,
      runtimeKind: target.runtimeKind,
      fencingToken: migration.targetFencingToken,
    });
    this.nextFenceByTask.set(placement.taskId, migration.targetFencingToken);
    this.transitionHost(source, {
      activePlacements: source.activePlacements - 1,
    });
    return { migration: nextMigration, placement: nextPlacement };
  }

  release(
    placementId: string,
    expectedRevision: number,
    errorCode = "",
  ): TaskExecutionPlacement {
    const placement = this.requirePlacement(placementId);
    this.assertPlacementRevision(placement, expectedRevision);
    if (!["reserved", "running", "failed"].includes(placement.state))
      throw new E03RuntimeError(
        "task_execution_placement_release_state",
        `task execution placement ${placementId} is ${placement.state}`,
      );
    const host = this.requireHost(placement.hostId);
    if (host.activePlacements < 1)
      throw new E03RuntimeError(
        "task_execution_placement_capacity_underflow",
        `task execution host ${host.hostId} has no placement`,
      );
    const next = this.transitionPlacement(placement, {
      state: errorCode ? "failed" : "released",
      errorCode,
      releasedAt: this.clock.now(),
    });
    this.transitionHost(host, { activePlacements: host.activePlacements - 1 });
    this.activePlacementByTask.delete(placement.taskId);
    return next;
  }

  snapshot(): TaskPlacementSnapshot {
    return {
      hosts: [...this.hosts.values()].map((value) => structuredClone(value)),
      placements: [...this.placements.values()].map((value) =>
        structuredClone(value),
      ),
      migrations: [...this.migrations.values()].map((value) =>
        structuredClone(value),
      ),
      activePlacementByTask: [...this.activePlacementByTask.entries()],
      migrationByIdempotencyKey: [...this.migrationByIdempotencyKey.entries()],
      nextFenceByTask: [...this.nextFenceByTask.entries()],
    };
  }

  restore(snapshot: TaskPlacementSnapshot): void {
    const hosts = new Map<string, TaskExecutionHostCapacity>();
    const placements = new Map<string, TaskExecutionPlacement>();
    const migrations = new Map<string, TaskPlacementMigration>();
    for (const value of snapshot.hosts) {
      assertTaskHostCapacity(value);
      if (hosts.has(value.hostId))
        throw new E03RuntimeError(
          "task_placement_restore_host_duplicate",
          `host ${value.hostId} duplicate`,
        );
      hosts.set(value.hostId, structuredClone(value));
    }
    for (const value of snapshot.placements) {
      assertTaskExecutionPlacement(value);
      if (!hosts.has(value.hostId) || placements.has(value.placementId))
        throw new E03RuntimeError(
          "task_placement_restore_placement",
          `placement ${value.placementId} invalid`,
        );
      placements.set(value.placementId, structuredClone(value));
    }
    for (const value of snapshot.migrations) {
      assertTaskPlacementMigration(value);
      const placement = placements.get(value.placementId);
      if (
        !placement ||
        placement.taskId !== value.taskId ||
        !hosts.has(value.sourceHostId) ||
        !hosts.has(value.targetHostId)
      )
        throw new E03RuntimeError(
          "task_placement_restore_migration",
          `migration ${value.migrationId} invalid`,
        );
      migrations.set(value.migrationId, structuredClone(value));
    }
    const activePlacementByTask = new Map(snapshot.activePlacementByTask);
    const migrationByIdempotencyKey = new Map(
      snapshot.migrationByIdempotencyKey,
    );
    const nextFenceByTask = new Map(snapshot.nextFenceByTask);
    if (
      activePlacementByTask.size !== snapshot.activePlacementByTask.length ||
      migrationByIdempotencyKey.size !==
        snapshot.migrationByIdempotencyKey.length
    )
      throw new E03RuntimeError(
        "task_placement_restore_index_duplicate",
        "task placement indexes duplicate",
      );
    for (const [taskId, placementId] of activePlacementByTask) {
      const value = placements.get(placementId);
      if (
        !value ||
        value.taskId !== taskId ||
        ["released", "failed"].includes(value.state)
      )
        throw new E03RuntimeError(
          "task_placement_restore_active",
          `task placement index ${taskId} invalid`,
        );
    }
    for (const [key, migrationId] of migrationByIdempotencyKey) {
      const value = migrations.get(migrationId);
      if (!value || value.idempotencyKey !== key)
        throw new E03RuntimeError(
          "task_placement_restore_idempotency",
          `migration index ${key} invalid`,
        );
    }
    for (const host of hosts.values()) {
      const active = [...placements.values()].filter(
        (value) =>
          value.hostId === host.hostId &&
          !["released", "failed"].includes(value.state),
      ).length;
      if (active !== host.activePlacements)
        throw new E03RuntimeError(
          "task_placement_restore_capacity",
          `host ${host.hostId} capacity invalid`,
        );
    }
    this.hosts = hosts;
    this.placements = placements;
    this.migrations = migrations;
    this.activePlacementByTask = activePlacementByTask;
    this.migrationByIdempotencyKey = migrationByIdempotencyKey;
    this.nextFenceByTask = nextFenceByTask;
  }

  private requireHost(id: string): TaskExecutionHostCapacity {
    const value = this.hosts.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_execution_host_missing",
        `host ${id} missing`,
      );
    assertTaskHostCapacity(value);
    return value;
  }

  private requirePlacement(id: string): TaskExecutionPlacement {
    const value = this.placements.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_execution_placement_missing",
        `placement ${id} missing`,
      );
    assertTaskExecutionPlacement(value);
    return value;
  }

  private requireMigration(id: string): TaskPlacementMigration {
    const value = this.migrations.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_placement_migration_missing",
        `migration ${id} missing`,
      );
    assertTaskPlacementMigration(value);
    return value;
  }

  private assertHostRevision(
    value: TaskExecutionHostCapacity,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_execution_host_stale_revision",
        `host ${value.hostId} stale`,
      );
  }

  private assertPlacementRevision(
    value: TaskExecutionPlacement,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_execution_placement_stale_revision",
        `placement ${value.placementId} stale`,
      );
  }

  private assertMigrationRevision(
    value: TaskPlacementMigration,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_placement_migration_stale_revision",
        `migration ${value.migrationId} stale`,
      );
  }

  private transitionHost(
    value: TaskExecutionHostCapacity,
    patch: Partial<
      Omit<TaskExecutionHostCapacity, "hostId" | "revision" | "digest">
    >,
  ): TaskExecutionHostCapacity {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      hostId: value.hostId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskHostCapacity(next);
    this.hosts.set(next.hostId, next);
    return structuredClone(next);
  }

  private transitionPlacement(
    value: TaskExecutionPlacement,
    patch: Partial<
      Omit<TaskExecutionPlacement, "placementId" | "revision" | "digest">
    >,
  ): TaskExecutionPlacement {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      placementId: value.placementId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskExecutionPlacement(next);
    this.placements.set(next.placementId, next);
    return structuredClone(next);
  }

  private transitionMigration(
    value: TaskPlacementMigration,
    patch: Partial<
      Omit<TaskPlacementMigration, "migrationId" | "revision" | "digest">
    >,
  ): TaskPlacementMigration {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      migrationId: value.migrationId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskPlacementMigration(next);
    this.migrations.set(next.migrationId, next);
    return structuredClone(next);
  }
}

export class TaskOutputVerificationRuntime {
  private policies = new Map<string, TaskOutputVerificationPolicy>();
  private reports = new Map<string, TaskOutputVerificationReport>();
  private checks = new Map<string, TaskOutputVerificationCheck[]>();
  private activeByTaskAttempt = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  registerPolicy(
    input: Omit<
      TaskOutputVerificationPolicy,
      "policyId" | "revision" | "digest"
    >,
  ): TaskOutputVerificationPolicy {
    if (
      [...this.policies.values()].some(
        (value) => value.name === input.name && value.state !== "retired",
      )
    )
      throw new E03RuntimeError(
        "task_output_policy_duplicate",
        `task output policy ${input.name} already exists`,
      );
    const requiredChecks = [...new Set(input.requiredChecks)].sort();
    const optionalChecks = [...new Set(input.optionalChecks)].sort();
    if (requiredChecks.some((name) => optionalChecks.includes(name)))
      throw new E03RuntimeError(
        "task_output_policy_overlap",
        "task output verification checks overlap",
      );
    const payload = {
      ...structuredClone(input),
      policyId: createId("task-output-policy"),
      requiredChecks,
      optionalChecks,
      allowedMediaTypes: [...new Set(input.allowedMediaTypes)].sort(),
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertOutputPolicy(policy);
    this.policies.set(policy.policyId, policy);
    return structuredClone(policy);
  }
  updatePolicy(
    policyId: string,
    expectedRevision: number,
    patch: Partial<
      Pick<
        TaskOutputVerificationPolicy,
        | "requiredChecks"
        | "optionalChecks"
        | "minimumOptionalPasses"
        | "rejectOnWarning"
        | "maximumArtifactBytes"
        | "allowedMediaTypes"
        | "state"
      >
    >,
  ): TaskOutputVerificationPolicy {
    const policy = this.requirePolicy(policyId);
    if (policy.revision !== expectedRevision)
      throw new E03RuntimeError(
        "task_output_policy_stale_revision",
        `task output policy ${policyId} revision is stale`,
      );
    if (policy.state === "retired")
      throw new E03RuntimeError(
        "task_output_policy_update_state",
        `task output policy ${policyId} is retired`,
      );
    const { digest: _, ...prior } = policy;
    const payload = {
      ...prior,
      ...structuredClone(patch),
      policyId: policy.policyId,
      requiredChecks: patch.requiredChecks
        ? [...new Set(patch.requiredChecks)].sort()
        : policy.requiredChecks,
      optionalChecks: patch.optionalChecks
        ? [...new Set(patch.optionalChecks)].sort()
        : policy.optionalChecks,
      allowedMediaTypes: patch.allowedMediaTypes
        ? [...new Set(patch.allowedMediaTypes)].sort()
        : policy.allowedMediaTypes,
      revision: policy.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertOutputPolicy(next);
    if (next.requiredChecks.some((name) => next.optionalChecks.includes(name)))
      throw new E03RuntimeError(
        "task_output_policy_overlap",
        "task output verification checks overlap",
      );
    this.policies.set(next.policyId, next);
    return structuredClone(next);
  }
  begin(input: {
    task: E03TaskState;
    policyId: string;
    resultDigest: string;
    artifactIds?: readonly string[];
  }): TaskOutputVerificationReport {
    const policy = this.requirePolicy(input.policyId);
    if (policy.state !== "active")
      throw new E03RuntimeError(
        "task_output_policy_inactive",
        `task output policy ${policy.policyId} is ${policy.state}`,
      );
    if (!input.resultDigest)
      throw new E03RuntimeError(
        "task_output_result_digest",
        "task output result digest is required",
      );
    const key = `${input.task.identity.taskId}:${input.task.identity.attempt}`;
    const existingId = this.activeByTaskAttempt.get(key);
    if (existingId) return structuredClone(this.requireReport(existingId));
    const payload = {
      reportId: createId("task-output-report"),
      taskId: input.task.identity.taskId,
      attempt: input.task.identity.attempt,
      policyId: policy.policyId,
      state: "open" as const,
      resultDigest: input.resultDigest,
      artifactIds: [...new Set(input.artifactIds ?? [])].sort(),
      checkIds: [],
      requiredPassed: 0,
      requiredFailed: 0,
      optionalPassed: 0,
      warningCount: 0,
      openedAt: this.clock.now(),
      completedAt: null,
      rejectionReason: null,
      revision: 1,
    };
    const report = { ...payload, digest: digest(payload) };
    assertOutputReport(report);
    this.reports.set(report.reportId, report);
    this.activeByTaskAttempt.set(key, report.reportId);
    return structuredClone(report);
  }
  record(input: {
    reportId: string;
    expectedRevision: number;
    name: string;
    outcome: TaskOutputVerificationCheck["outcome"];
    summary: string;
    evidence?: JsonObject;
  }): {
    report: TaskOutputVerificationReport;
    check: TaskOutputVerificationCheck;
  } {
    const report = this.requireReport(input.reportId);
    this.assertReportRevision(report, input.expectedRevision);
    if (report.state !== "open")
      throw new E03RuntimeError(
        "task_output_check_state",
        `task output report ${report.reportId} is ${report.state}`,
      );
    const policy = this.requirePolicy(report.policyId);
    const required = policy.requiredChecks.includes(input.name);
    if (!required && !policy.optionalChecks.includes(input.name))
      throw new E03RuntimeError(
        "task_output_check_unknown",
        `task output check ${input.name} is not in policy`,
      );
    if (!input.summary.trim())
      throw new E03RuntimeError(
        "task_output_check_summary",
        "task output check summary is required",
      );
    const entries = this.checks.get(report.reportId) ?? [];
    const prior = entries.find((value) => value.name === input.name);
    if (prior) {
      if (prior.outcome !== input.outcome)
        throw new E03RuntimeError(
          "task_output_check_conflict",
          `task output check ${input.name} already has an outcome`,
        );
      return { report: structuredClone(report), check: structuredClone(prior) };
    }
    const payload = {
      checkId: createId("task-output-check"),
      reportId: report.reportId,
      name: input.name,
      required,
      outcome: input.outcome,
      summary: input.summary.trim(),
      evidence: structuredClone(input.evidence ?? {}),
      checkedAt: this.clock.now(),
    };
    const check = { ...payload, digest: digest(payload) };
    assertOutputCheck(check);
    entries.push(check);
    this.checks.set(report.reportId, entries);
    const next = this.transitionReport(report, {
      checkIds: [...report.checkIds, check.checkId],
      requiredPassed:
        report.requiredPassed +
        (required && input.outcome === "passed" ? 1 : 0),
      requiredFailed:
        report.requiredFailed +
        (required && (input.outcome === "failed" || input.outcome === "skipped")
          ? 1
          : 0),
      optionalPassed:
        report.optionalPassed +
        (!required && input.outcome === "passed" ? 1 : 0),
      warningCount: report.warningCount + (input.outcome === "warning" ? 1 : 0),
    });
    return { report: next, check: structuredClone(check) };
  }
  finalize(
    reportId: string,
    expectedRevision: number,
  ): TaskOutputVerificationReport {
    const report = this.requireReport(reportId);
    this.assertReportRevision(report, expectedRevision);
    if (report.state !== "open")
      throw new E03RuntimeError(
        "task_output_finalize_state",
        `task output report ${reportId} is ${report.state}`,
      );
    const policy = this.requirePolicy(report.policyId);
    const entries = this.checks.get(report.reportId) ?? [];
    const missing = policy.requiredChecks.filter(
      (name) => !entries.some((entry) => entry.name === name),
    );
    const rejected =
      missing.length > 0 ||
      report.requiredFailed > 0 ||
      report.optionalPassed < policy.minimumOptionalPasses ||
      (policy.rejectOnWarning && report.warningCount > 0);
    const reasons: string[] = [];
    if (missing.length)
      reasons.push(`missing required checks: ${missing.join(",")}`);
    if (report.requiredFailed)
      reasons.push(`${report.requiredFailed} required checks failed`);
    if (report.optionalPassed < policy.minimumOptionalPasses)
      reasons.push("optional check threshold not met");
    if (policy.rejectOnWarning && report.warningCount)
      reasons.push("warning rejected by policy");
    const next = this.transitionReport(report, {
      state: rejected ? "rejected" : "verified",
      completedAt: this.clock.now(),
      rejectionReason: rejected ? reasons.join("; ") : null,
    });
    this.activeByTaskAttempt.delete(`${report.taskId}:${report.attempt}`);
    return next;
  }
  cancel(
    reportId: string,
    expectedRevision: number,
    reason: string,
  ): TaskOutputVerificationReport {
    const report = this.requireReport(reportId);
    this.assertReportRevision(report, expectedRevision);
    if (report.state !== "open") return structuredClone(report);
    if (!reason.trim())
      throw new E03RuntimeError(
        "task_output_cancel_reason",
        "task output verification cancellation reason is required",
      );
    const next = this.transitionReport(report, {
      state: "cancelled",
      completedAt: this.clock.now(),
      rejectionReason: reason.trim(),
    });
    this.activeByTaskAttempt.delete(`${report.taskId}:${report.attempt}`);
    return next;
  }
  snapshot(): {
    policies: TaskOutputVerificationPolicy[];
    reports: TaskOutputVerificationReport[];
    checks: TaskOutputVerificationCheck[];
    activeByTaskAttempt: Array<[string, string]>;
  } {
    return {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      reports: [...this.reports.values()].map((value) =>
        structuredClone(value),
      ),
      checks: [...this.checks.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeByTaskAttempt: [...this.activeByTaskAttempt.entries()].map(
        ([key, id]) => [key, id],
      ),
    };
  }
  restore(snapshot: {
    policies: readonly TaskOutputVerificationPolicy[];
    reports: readonly TaskOutputVerificationReport[];
    checks: readonly TaskOutputVerificationCheck[];
    activeByTaskAttempt: ReadonlyArray<readonly [string, string]>;
  }): void {
    const policies = new Map<string, TaskOutputVerificationPolicy>();
    const reports = new Map<string, TaskOutputVerificationReport>();
    const checks = new Map<string, TaskOutputVerificationCheck[]>();
    const activeByTaskAttempt = new Map<string, string>();
    for (const value of snapshot.policies) {
      assertOutputPolicy(value);
      if (policies.has(value.policyId))
        throw new E03RuntimeError(
          "task_output_policy_restore_duplicate",
          `duplicate task output policy ${value.policyId}`,
        );
      policies.set(value.policyId, structuredClone(value));
    }
    for (const value of snapshot.reports) {
      assertOutputReport(value);
      if (reports.has(value.reportId) || !policies.has(value.policyId))
        throw new E03RuntimeError(
          "task_output_report_restore",
          `task output report ${value.reportId} is invalid`,
        );
      reports.set(value.reportId, structuredClone(value));
    }
    for (const value of snapshot.checks) {
      assertOutputCheck(value);
      if (!reports.has(value.reportId))
        throw new E03RuntimeError(
          "task_output_check_restore",
          `task output check ${value.checkId} has no report`,
        );
      const entries = checks.get(value.reportId) ?? [];
      if (
        entries.some(
          (entry) =>
            entry.name === value.name || entry.checkId === value.checkId,
        )
      )
        throw new E03RuntimeError(
          "task_output_check_restore_duplicate",
          `duplicate task output check ${value.checkId}`,
        );
      entries.push(structuredClone(value));
      checks.set(value.reportId, entries);
    }
    for (const [key, id] of snapshot.activeByTaskAttempt) {
      const report = reports.get(id);
      if (
        !report ||
        report.state !== "open" ||
        key !== `${report.taskId}:${report.attempt}` ||
        activeByTaskAttempt.has(key)
      )
        throw new E03RuntimeError(
          "task_output_active_restore",
          `task output active index ${key} is invalid`,
        );
      activeByTaskAttempt.set(key, id);
    }
    this.policies = policies;
    this.reports = reports;
    this.checks = checks;
    this.activeByTaskAttempt = activeByTaskAttempt;
  }
  private requirePolicy(id: string): TaskOutputVerificationPolicy {
    const value = this.policies.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_output_policy_missing",
        `task output policy ${id} does not exist`,
      );
    assertOutputPolicy(value);
    return value;
  }
  private requireReport(id: string): TaskOutputVerificationReport {
    const value = this.reports.get(id);
    if (!value)
      throw new E03RuntimeError(
        "task_output_report_missing",
        `task output report ${id} does not exist`,
      );
    assertOutputReport(value);
    return value;
  }
  private assertReportRevision(
    value: TaskOutputVerificationReport,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "task_output_report_stale_revision",
        `task output report ${value.reportId} revision is stale`,
      );
  }
  private transitionReport(
    value: TaskOutputVerificationReport,
    patch: Partial<
      Omit<TaskOutputVerificationReport, "reportId" | "revision" | "digest">
    >,
  ): TaskOutputVerificationReport {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      reportId: value.reportId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertOutputReport(next);
    this.reports.set(next.reportId, next);
    return structuredClone(next);
  }
}

export type ExecutionCustodySettlementState =
  | "prepared"
  | "observing"
  | "reconciled"
  | "committed"
  | "aborted";

export interface ExecutionCustodySettlement {
  settlementId: string;
  taskId: string;
  taskLeaseId: string;
  claimId: string;
  executionAttemptId: string;
  workerId: string;
  state: ExecutionCustodySettlementState;
  requiredObservationKinds: ExecutionCustodyObservationKind[];
  acceptedObservationIds: string[];
  rejectedObservationIds: string[];
  expectedTaskRevision: number;
  claimDigest: string;
  attemptDigest: string;
  outcomeDigest: string;
  commitFence: string;
  abortReason: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export type ExecutionCustodyObservationKind =
  | "worker-exit"
  | "output"
  | "usage"
  | "artifact"
  | "side-effect";

export interface ExecutionCustodyObservation {
  observationId: string;
  settlementId: string;
  kind: ExecutionCustodyObservationKind;
  observerId: string;
  accepted: boolean;
  sequence: number;
  evidenceDigest: string;
  errorCode: string;
  previousObservationDigest: string;
  observedAt: string;
  revision: number;
  digest: string;
}

export interface ExecutionCustodyCommitReceipt {
  receiptId: string;
  settlementId: string;
  taskId: string;
  taskLeaseId: string;
  executionAttemptId: string;
  commitFence: string;
  expectedTaskRevision: number;
  nextTaskRevision: number;
  outcomeDigest: string;
  observationChainDigest: string;
  committedAt: string;
  revision: number;
  digest: string;
}

export interface ExecutionCustodySettlementSnapshot {
  settlements: ExecutionCustodySettlement[];
  observations: ExecutionCustodyObservation[];
  receipts: ExecutionCustodyCommitReceipt[];
  receiptIdByCommitFence: Array<[string, string]>;
  digest: string;
}

function assertExecutionCustodySettlement(
  value: ExecutionCustodySettlement,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.settlementId ||
    !value.taskId ||
    !value.taskLeaseId ||
    !value.claimId ||
    !value.executionAttemptId ||
    !value.workerId ||
    !value.requiredObservationKinds.length ||
    new Set(value.requiredObservationKinds).size !==
      value.requiredObservationKinds.length ||
    value.acceptedObservationIds.some((id) =>
      value.rejectedObservationIds.includes(id),
    ) ||
    value.expectedTaskRevision < 1 ||
    !value.claimDigest ||
    !value.attemptDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "execution_custody_settlement_corrupt",
      `execution custody settlement ${value.settlementId || "<empty>"} is corrupt`,
    );
  if (
    value.state === "committed" &&
    (!value.commitFence || !value.outcomeDigest)
  )
    throw new E03RuntimeError(
      "execution_custody_commit_fields_missing",
      "committed execution custody settlement lacks fence or outcome",
    );
  if (value.state === "aborted" && !value.abortReason)
    throw new E03RuntimeError(
      "execution_custody_abort_reason_missing",
      "aborted execution custody settlement requires reason",
    );
}

function assertExecutionCustodyObservation(
  value: ExecutionCustodyObservation,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.observationId ||
    !value.settlementId ||
    !value.observerId ||
    value.sequence < 1 ||
    !value.evidenceDigest ||
    !value.previousObservationDigest ||
    (!value.accepted && !value.errorCode) ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "execution_custody_observation_corrupt",
      `execution custody observation ${value.observationId || "<empty>"} is corrupt`,
    );
}

function assertExecutionCustodyCommitReceipt(
  value: ExecutionCustodyCommitReceipt,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.settlementId ||
    !value.taskId ||
    !value.taskLeaseId ||
    !value.executionAttemptId ||
    !value.commitFence ||
    value.expectedTaskRevision < 1 ||
    value.nextTaskRevision !== value.expectedTaskRevision + 1 ||
    !value.outcomeDigest ||
    !value.observationChainDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "execution_custody_commit_receipt_corrupt",
      `execution custody receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

export class TaskExecutionCustodySettlementRuntime {
  private settlements = new Map<string, ExecutionCustodySettlement>();
  private observations = new Map<string, ExecutionCustodyObservation[]>();
  private receipts = new Map<string, ExecutionCustodyCommitReceipt>();
  private receiptIdByCommitFence = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  prepare(input: {
    settlementId?: string;
    task: E03TaskState;
    claim: TaskExecutionClaim;
    attempt: TaskExecutionAttempt;
    requiredObservationKinds?: ExecutionCustodyObservationKind[];
  }): ExecutionCustodySettlement {
    assertExecutionClaim(input.claim);
    assertTaskExecutionAttempt(input.attempt);
    if (
      input.task.identity.taskId !== input.claim.taskId ||
      input.task.identity.taskId !== input.attempt.taskId ||
      input.task.identity.leaseId !== input.claim.taskLeaseId ||
      input.task.identity.leaseId !== input.attempt.leaseId ||
      input.claim.workerId !== input.attempt.workerId
    )
      throw new E03RuntimeError(
        "execution_custody_prepare_binding_mismatch",
        "task, claim, and execution attempt custody do not match",
      );
    if (!input.attempt.workerId)
      throw new E03RuntimeError(
        "execution_custody_prepare_worker_missing",
        "execution custody settlement requires assigned worker",
      );
    if (input.claim.phase !== "completed" && input.claim.phase !== "failed")
      throw new E03RuntimeError(
        "execution_custody_prepare_claim_not_terminal",
        "execution custody settlement requires terminal claim",
      );
    if (!terminalExecutionAttempt(input.attempt.state))
      throw new E03RuntimeError(
        "execution_custody_prepare_attempt_not_terminal",
        "execution custody settlement requires terminal attempt",
      );
    const settlementId =
      input.settlementId ?? createId("execution-custody-settlement");
    const prior = this.settlements.get(settlementId);
    if (prior) {
      if (
        prior.claimDigest !== input.claim.digest ||
        prior.attemptDigest !== input.attempt.digest
      )
        throw new E03RuntimeError(
          "execution_custody_prepare_id_conflict",
          `execution custody settlement ${settlementId} conflicts`,
        );
      return structuredClone(prior);
    }
    const requiredObservationKinds = [
      ...new Set(
        input.requiredObservationKinds ?? ["worker-exit", "output", "usage"],
      ),
    ].sort() as ExecutionCustodyObservationKind[];
    const now = this.clock.now();
    const payload = {
      settlementId,
      taskId: input.task.identity.taskId,
      taskLeaseId: input.task.identity.leaseId,
      claimId: input.claim.claimId,
      executionAttemptId: input.attempt.executionAttemptId,
      workerId: input.attempt.workerId,
      state: "prepared" as const,
      requiredObservationKinds,
      acceptedObservationIds: [] as string[],
      rejectedObservationIds: [] as string[],
      expectedTaskRevision: input.task.revision,
      claimDigest: input.claim.digest,
      attemptDigest: input.attempt.digest,
      outcomeDigest: "",
      commitFence: "",
      abortReason: "",
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const settlement = { ...payload, digest: digest(payload) };
    assertExecutionCustodySettlement(settlement);
    this.settlements.set(settlement.settlementId, settlement);
    this.observations.set(settlement.settlementId, []);
    return structuredClone(settlement);
  }

  begin(input: {
    settlementId: string;
    expectedRevision: number;
  }): ExecutionCustodySettlement {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state !== "prepared")
      throw new E03RuntimeError(
        "execution_custody_begin_invalid_state",
        `cannot begin execution settlement from ${settlement.state}`,
      );
    return this.transitionSettlement(settlement, {
      state: "observing",
      updatedAt: this.clock.now(),
    });
  }

  observe(input: {
    settlementId: string;
    expectedRevision: number;
    kind: ExecutionCustodyObservationKind;
    observerId: string;
    accepted: boolean;
    evidenceDigest: string;
    errorCode?: string;
  }): {
    settlement: ExecutionCustodySettlement;
    observation: ExecutionCustodyObservation;
  } {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state !== "observing")
      throw new E03RuntimeError(
        "execution_custody_observe_invalid_state",
        "execution custody settlement is not observing",
      );
    if (!settlement.requiredObservationKinds.includes(input.kind))
      throw new E03RuntimeError(
        "execution_custody_observation_kind_unplanned",
        `execution custody observation ${input.kind} was not planned`,
      );
    const list = this.observations.get(settlement.settlementId) ?? [];
    const priorForKind = [...list]
      .reverse()
      .find((value) => value.kind === input.kind);
    if (priorForKind?.accepted) {
      if (
        priorForKind.evidenceDigest !== input.evidenceDigest ||
        priorForKind.observerId !== input.observerId
      )
        throw new E03RuntimeError(
          "execution_custody_observation_conflict",
          `execution custody observation ${input.kind} already accepted`,
        );
      return {
        settlement: structuredClone(settlement),
        observation: structuredClone(priorForKind),
      };
    }
    const payload = {
      observationId: createId("execution-custody-observation"),
      settlementId: settlement.settlementId,
      kind: input.kind,
      observerId: input.observerId,
      accepted: input.accepted,
      sequence: list.length + 1,
      evidenceDigest: input.evidenceDigest,
      errorCode: input.errorCode ?? "",
      previousObservationDigest:
        list.at(-1)?.digest ?? digest("execution-custody-observation-root"),
      observedAt: this.clock.now(),
      revision: 1,
    };
    const observation = { ...payload, digest: digest(payload) };
    assertExecutionCustodyObservation(observation);
    this.observations.set(settlement.settlementId, [...list, observation]);
    const accepted = new Set(settlement.acceptedObservationIds);
    const rejected = new Set(settlement.rejectedObservationIds);
    for (const prior of list.filter((value) => value.kind === input.kind)) {
      accepted.delete(prior.observationId);
      rejected.delete(prior.observationId);
    }
    if (observation.accepted) accepted.add(observation.observationId);
    else rejected.add(observation.observationId);
    const next = this.transitionSettlement(settlement, {
      acceptedObservationIds: [...accepted],
      rejectedObservationIds: [...rejected],
      updatedAt: this.clock.now(),
    });
    return { settlement: next, observation: structuredClone(observation) };
  }

  reconcile(input: {
    settlementId: string;
    expectedRevision: number;
    outcomeDigest: string;
  }): ExecutionCustodySettlement {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state !== "observing")
      throw new E03RuntimeError(
        "execution_custody_reconcile_invalid_state",
        "execution custody settlement must be observing",
      );
    if (!input.outcomeDigest)
      throw new E03RuntimeError(
        "execution_custody_outcome_digest_missing",
        "execution custody settlement requires outcome digest",
      );
    const list = this.observations.get(settlement.settlementId) ?? [];
    const acceptedKinds = new Set(
      list.filter((value) => value.accepted).map((value) => value.kind),
    );
    const missing = settlement.requiredObservationKinds.filter(
      (kind) => !acceptedKinds.has(kind),
    );
    if (missing.length)
      throw new E03RuntimeError(
        "execution_custody_observation_incomplete",
        `execution custody settlement is missing ${missing.join(",")}`,
      );
    const latestByKind = new Map<
      ExecutionCustodyObservationKind,
      ExecutionCustodyObservation
    >();
    for (const observation of list)
      if (observation.accepted) latestByKind.set(observation.kind, observation);
    const acceptedObservationIds = [...latestByKind.values()]
      .sort((left, right) => left.kind.localeCompare(right.kind))
      .map((value) => value.observationId);
    return this.transitionSettlement(settlement, {
      state: "reconciled",
      acceptedObservationIds,
      outcomeDigest: input.outcomeDigest,
      updatedAt: this.clock.now(),
    });
  }

  commit(input: {
    settlementId: string;
    expectedRevision: number;
    task: E03TaskState;
    commitFence: string;
  }): {
    settlement: ExecutionCustodySettlement;
    receipt: ExecutionCustodyCommitReceipt;
  } {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state === "committed") {
      if (settlement.commitFence !== input.commitFence)
        throw new E03RuntimeError(
          "execution_custody_commit_fence_conflict",
          "execution custody settlement was committed by another fence",
        );
      const receiptId = this.receiptIdByCommitFence.get(input.commitFence);
      const receipt = receiptId ? this.receipts.get(receiptId) : undefined;
      if (!receipt)
        throw new E03RuntimeError(
          "execution_custody_commit_receipt_missing",
          "committed execution custody settlement lacks receipt",
        );
      return {
        settlement: structuredClone(settlement),
        receipt: structuredClone(receipt),
      };
    }
    if (settlement.state !== "reconciled")
      throw new E03RuntimeError(
        "execution_custody_commit_invalid_state",
        "execution custody settlement must be reconciled before commit",
      );
    if (
      input.task.identity.taskId !== settlement.taskId ||
      input.task.identity.leaseId !== settlement.taskLeaseId ||
      input.task.revision !== settlement.expectedTaskRevision
    )
      throw new E03RuntimeError(
        "execution_custody_commit_task_stale",
        "execution custody settlement task revision or lease changed",
      );
    if (!input.commitFence)
      throw new E03RuntimeError(
        "execution_custody_commit_fence_required",
        "execution custody settlement requires commit fence",
      );
    const priorReceiptId = this.receiptIdByCommitFence.get(input.commitFence);
    if (priorReceiptId)
      throw new E03RuntimeError(
        "execution_custody_commit_fence_reused",
        "execution custody commit fence is already bound",
      );
    const list = this.observations.get(settlement.settlementId) ?? [];
    const receiptPayload = {
      receiptId: createId("execution-custody-commit-receipt"),
      settlementId: settlement.settlementId,
      taskId: settlement.taskId,
      taskLeaseId: settlement.taskLeaseId,
      executionAttemptId: settlement.executionAttemptId,
      commitFence: input.commitFence,
      expectedTaskRevision: settlement.expectedTaskRevision,
      nextTaskRevision: settlement.expectedTaskRevision + 1,
      outcomeDigest: settlement.outcomeDigest,
      observationChainDigest: digest(list.map((value) => value.digest)),
      committedAt: this.clock.now(),
      revision: 1,
    };
    const receipt = { ...receiptPayload, digest: digest(receiptPayload) };
    assertExecutionCustodyCommitReceipt(receipt);
    this.receipts.set(receipt.receiptId, receipt);
    this.receiptIdByCommitFence.set(receipt.commitFence, receipt.receiptId);
    const next = this.transitionSettlement(settlement, {
      state: "committed",
      commitFence: input.commitFence,
      updatedAt: this.clock.now(),
    });
    return { settlement: next, receipt: structuredClone(receipt) };
  }

  abort(input: {
    settlementId: string;
    expectedRevision: number;
    reason: string;
  }): ExecutionCustodySettlement {
    const settlement = this.requireSettlement(input.settlementId);
    this.assertSettlementRevision(settlement, input.expectedRevision);
    if (settlement.state === "committed")
      throw new E03RuntimeError(
        "execution_custody_abort_committed",
        "committed execution custody settlement cannot abort",
      );
    if (!input.reason)
      throw new E03RuntimeError(
        "execution_custody_abort_reason_required",
        "execution custody abort requires reason",
      );
    if (settlement.state === "aborted") return structuredClone(settlement);
    return this.transitionSettlement(settlement, {
      state: "aborted",
      abortReason: input.reason,
      updatedAt: this.clock.now(),
    });
  }

  snapshot(): ExecutionCustodySettlementSnapshot {
    const payload = {
      settlements: [...this.settlements.values()].map((value) =>
        structuredClone(value),
      ),
      observations: [...this.observations.values()]
        .flat()
        .map((value) => structuredClone(value)),
      receipts: [...this.receipts.values()].map((value) =>
        structuredClone(value),
      ),
      receiptIdByCommitFence: [...this.receiptIdByCommitFence.entries()],
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(snapshot: ExecutionCustodySettlementSnapshot): void {
    const { digest: expected, ...payload } = snapshot;
    if (digest(payload) !== expected)
      throw new E03RuntimeError(
        "execution_custody_snapshot_corrupt",
        "execution custody settlement snapshot digest mismatch",
      );
    const settlements = new Map<string, ExecutionCustodySettlement>();
    const observations = new Map<string, ExecutionCustodyObservation[]>();
    const receipts = new Map<string, ExecutionCustodyCommitReceipt>();
    for (const value of payload.settlements) {
      assertExecutionCustodySettlement(value);
      if (settlements.has(value.settlementId))
        throw new E03RuntimeError(
          "execution_custody_snapshot_settlement_duplicate",
          `duplicate execution custody settlement ${value.settlementId}`,
        );
      settlements.set(value.settlementId, structuredClone(value));
      observations.set(value.settlementId, []);
    }
    for (const value of payload.observations) {
      assertExecutionCustodyObservation(value);
      const list = observations.get(value.settlementId);
      if (!list)
        throw new E03RuntimeError(
          "execution_custody_snapshot_observation_orphaned",
          `execution custody observation ${value.observationId} is orphaned`,
        );
      if (
        value.sequence !== list.length + 1 ||
        (list.length > 0 &&
          value.previousObservationDigest !== list.at(-1)!.digest)
      )
        throw new E03RuntimeError(
          "execution_custody_snapshot_observation_chain_corrupt",
          `execution custody observation ${value.observationId} breaks chain`,
        );
      list.push(structuredClone(value));
    }
    for (const value of payload.receipts) {
      assertExecutionCustodyCommitReceipt(value);
      if (!settlements.has(value.settlementId))
        throw new E03RuntimeError(
          "execution_custody_snapshot_receipt_orphaned",
          `execution custody receipt ${value.receiptId} is orphaned`,
        );
      receipts.set(value.receiptId, structuredClone(value));
    }
    const receiptIndex = new Map(payload.receiptIdByCommitFence);
    for (const receiptId of receiptIndex.values())
      if (!receipts.has(receiptId))
        throw new E03RuntimeError(
          "execution_custody_snapshot_receipt_index_corrupt",
          `execution custody receipt index references ${receiptId}`,
        );
    this.settlements = settlements;
    this.observations = observations;
    this.receipts = receipts;
    this.receiptIdByCommitFence = receiptIndex;
  }

  private requireSettlement(id: string): ExecutionCustodySettlement {
    const value = this.settlements.get(id);
    if (!value)
      throw new E03RuntimeError(
        "execution_custody_settlement_missing",
        `execution custody settlement ${id} does not exist`,
      );
    assertExecutionCustodySettlement(value);
    return value;
  }

  private assertSettlementRevision(
    value: ExecutionCustodySettlement,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "execution_custody_settlement_stale_revision",
        `execution custody settlement ${value.settlementId} revision is stale`,
      );
  }

  private transitionSettlement(
    value: ExecutionCustodySettlement,
    patch: Partial<
      Omit<ExecutionCustodySettlement, "settlementId" | "revision" | "digest">
    >,
  ): ExecutionCustodySettlement {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      settlementId: value.settlementId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertExecutionCustodySettlement(next);
    this.settlements.set(next.settlementId, next);
    return structuredClone(next);
  }
}
