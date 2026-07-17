import {
  createId,
  digest,
  E03RuntimeError,
  isTerminal,
  sealTask,
  type AgentTaskPhase,
  type E03AgentDefinition,
  type E03CapabilityScope,
  type E03Clock,
  type E03ContextSnapshot,
  type E03Identity,
  type E03TaskState,
  type E03Transition,
  SystemE03Clock,
} from "../e03/contracts.ts";

const ALLOWED: Readonly<Record<AgentTaskPhase, readonly AgentTaskPhase[]>> =
  Object.freeze({
    created: ["queued", "cancelled", "killed", "failed"],
    queued: ["running", "cancelled", "killed", "failed"],
    running: ["waiting", "completed", "failed", "cancelled", "killed"],
    waiting: [
      "queued",
      "running",
      "completed",
      "failed",
      "cancelled",
      "killed",
    ],
    completed: [],
    failed: ["queued"],
    cancelled: [],
    killed: [],
  });

export interface TaskCreationInput {
  identity: E03Identity;
  definition: E03AgentDefinition;
  scope: E03CapabilityScope;
  context: E03ContextSnapshot;
  prompt: string;
  executionMode: "foreground" | "background";
}

export class TaskStateMachine {
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  create(input: TaskCreationInput): E03TaskState {
    const prompt = input.prompt.trim();
    if (!prompt)
      throw new E03RuntimeError("empty_agent_prompt", "agent prompt is empty");
    if (prompt.length > 1_000_000)
      throw new E03RuntimeError(
        "agent_prompt_too_large",
        "agent prompt exceeds one million characters",
      );
    if (
      input.context.taskId !== input.identity.taskId ||
      input.context.sessionId !== input.identity.sessionId
    )
      throw new E03RuntimeError(
        "task_context_mismatch",
        "task context identity differs from task identity",
      );
    const now = this.clock.now();
    return sealTask({
      identity: structuredClone(input.identity),
      definition: structuredClone(input.definition),
      scope: structuredClone(input.scope),
      context: structuredClone(input.context),
      status: "created",
      revision: 1,
      sequence: 1,
      prompt,
      promptDigest: digest(prompt),
      executionMode: input.executionMode,
      isolation: null,
      isolationReceipt: null,
      messages: [],
      deliveries: [],
      transitions: [],
      childTaskIds: [],
      result: null,
      artifacts: [],
      usage: {},
      error: "",
      createdAt: now,
      updatedAt: now,
      terminalAt: null,
    });
  }

  transition(
    task: E03TaskState,
    target: AgentTaskPhase,
    input: {
      requestId: string;
      idempotencyKey: string;
      writerId: string;
      expectedRevision: number;
      eventType: string;
      result?: E03TaskState["result"];
      artifacts?: E03TaskState["artifacts"];
      usage?: E03TaskState["usage"];
      error?: string;
    },
  ): { state: E03TaskState; transition: E03Transition } {
    this.assertRevision(task, input.expectedRevision);
    if (!ALLOWED[task.status].includes(target))
      throw new E03RuntimeError(
        "invalid_task_transition",
        `${task.status} cannot transition to ${target}`,
        { taskId: task.identity.taskId },
      );
    if (target === "completed" && input.result === undefined)
      throw new E03RuntimeError(
        "missing_task_result",
        "completed task requires a result",
      );
    if (
      (target === "failed" || target === "cancelled" || target === "killed") &&
      !input.error?.trim()
    )
      throw new E03RuntimeError(
        "missing_terminal_reason",
        `${target} task requires a reason`,
      );
    const now = this.clock.now();
    const transition = this.transitionRecord(task, target, input, now);
    const next = sealTask({
      ...task,
      status: target,
      revision: task.revision + 1,
      sequence: task.sequence + 1,
      result: input.result ?? task.result,
      artifacts: input.artifacts
        ? mergeArtifacts(task.artifacts, input.artifacts)
        : task.artifacts,
      usage: input.usage ? { ...task.usage, ...input.usage } : task.usage,
      error: input.error?.trim() ?? "",
      transitions: [...task.transitions, transition],
      updatedAt: now,
      terminalAt: isTerminal(target) ? now : null,
      checksum: "",
    });
    return { state: next, transition };
  }

  cancel(
    task: E03TaskState,
    input: {
      requestId: string;
      idempotencyKey: string;
      writerId: string;
      expectedRevision: number;
      reason: string;
    },
  ): { state: E03TaskState; transition: E03Transition } {
    if (task.status === "cancelled")
      return {
        state: structuredClone(task),
        transition: task.transitions.at(-1)!,
      };
    if (isTerminal(task.status))
      throw new E03RuntimeError(
        "terminal_task",
        `cannot cancel ${task.status} task`,
      );
    return this.transition(task, "cancelled", {
      ...input,
      eventType: "agent_task_cancelled",
      error: input.reason,
    });
  }

  kill(
    task: E03TaskState,
    input: {
      requestId: string;
      idempotencyKey: string;
      writerId: string;
      expectedRevision: number;
      reason: string;
    },
  ): { state: E03TaskState; transition: E03Transition } {
    if (task.status === "killed")
      return {
        state: structuredClone(task),
        transition: task.transitions.at(-1)!,
      };
    if (isTerminal(task.status))
      throw new E03RuntimeError(
        "terminal_task",
        `cannot kill ${task.status} task`,
      );
    if (!task.scope.allowKill)
      throw new E03RuntimeError(
        "kill_not_permitted",
        "task scope does not permit kill",
      );
    return this.transition(task, "killed", {
      ...input,
      eventType: "agent_task_killed",
      error: input.reason,
    });
  }

  rejectLateResult(
    task: E03TaskState,
    expectedRevision: number,
    leaseId: string,
  ): void {
    if (task.identity.leaseId !== leaseId)
      throw new E03RuntimeError(
        "stale_lease",
        "late result uses a stale task lease",
        { expected: task.identity.leaseId, actual: leaseId },
      );
    if (task.revision !== expectedRevision)
      throw new E03RuntimeError(
        "stale_revision",
        "late result uses a stale revision",
        { expected: task.revision, actual: expectedRevision },
      );
    if (task.status === "cancelled" || task.status === "killed")
      throw new E03RuntimeError(
        "late_terminal_result",
        `late result cannot revive ${task.status} task`,
      );
    if (isTerminal(task.status))
      throw new E03RuntimeError(
        "duplicate_terminal_result",
        `task already ended as ${task.status}`,
      );
  }

  assertRevision(task: E03TaskState, expectedRevision: number): void {
    if (task.revision !== expectedRevision)
      throw new E03RuntimeError(
        "stale_revision",
        `expected revision ${expectedRevision}, current ${task.revision}`,
        {
          taskId: task.identity.taskId,
          expectedRevision,
          currentRevision: task.revision,
        },
      );
  }

  private transitionRecord(
    task: E03TaskState,
    target: AgentTaskPhase,
    input: {
      requestId: string;
      idempotencyKey: string;
      writerId: string;
      eventType: string;
    },
    now: string,
  ): E03Transition {
    const payload = {
      transitionId: createId("task-transition"),
      taskId: task.identity.taskId,
      requestId: input.requestId,
      idempotencyKey: input.idempotencyKey,
      phase: "prepare" as const,
      fromRevision: task.revision,
      toRevision: task.revision + 1,
      fromStatus: task.status,
      toStatus: target,
      eventType: input.eventType,
      effectId: null,
      receiptId: null,
      writerId: input.writerId,
      leaseId: task.identity.leaseId,
      preparedAt: now,
      committedAt: null,
      acknowledgedAt: null,
    };
    return { ...payload, digest: digest(payload) };
  }
}

function mergeArtifacts(
  left: E03TaskState["artifacts"],
  right: E03TaskState["artifacts"],
): E03TaskState["artifacts"] {
  const merged = new Map(
    left.map((artifact) => [artifact.artifact_id, artifact]),
  );
  for (const artifact of right) merged.set(artifact.artifact_id, artifact);
  return [...merged.values()];
}

export type LifecycleIntent =
  | "enqueue"
  | "start"
  | "wait"
  | "resume"
  | "succeed"
  | "fail"
  | "cancel"
  | "kill"
  | "retry";

export interface LifecycleDecisionInput {
  task: E03TaskState;
  intent: LifecycleIntent;
  actorTaskId: string;
  actorSessionId: string;
  expectedRevision: number;
  leaseId: string;
  reason: string;
  hasResult?: boolean;
  now?: string;
}

export interface LifecycleDecision {
  accepted: boolean;
  code: string;
  source: AgentTaskPhase;
  target: AgentTaskPhase;
  taskId: string;
  actorTaskId: string;
  expectedRevision: number;
  currentRevision: number;
  leaseMatched: boolean;
  actorMatched: boolean;
  resultRequired: boolean;
  terminalFence: boolean;
  reason: string;
  decidedAt: string;
  digest: string;
}

const INTENT_TARGET: Readonly<Record<LifecycleIntent, AgentTaskPhase>> =
  Object.freeze({
    enqueue: "queued",
    start: "running",
    wait: "waiting",
    resume: "running",
    succeed: "completed",
    fail: "failed",
    cancel: "cancelled",
    kill: "killed",
    retry: "queued",
  });

export class TaskTransitionPolicy {
  decide(input: LifecycleDecisionInput): LifecycleDecision {
    const task = input.task;
    const target = INTENT_TARGET[input.intent];
    const leaseMatched = task.identity.leaseId === input.leaseId;
    const actorMatched =
      input.actorSessionId === task.identity.sessionId &&
      (input.actorTaskId === task.identity.taskId ||
        input.actorTaskId === task.identity.parentTaskId);
    const resultRequired = target === "completed";
    const terminalFence = isTerminal(task.status);
    let code = "transition_accepted";
    if (!leaseMatched) code = "transition_stale_lease";
    else if (task.revision !== input.expectedRevision)
      code = "transition_stale_revision";
    else if (!actorMatched) code = "transition_actor_mismatch";
    else if (terminalFence) code = "transition_terminal_fence";
    else if (!ALLOWED[task.status].includes(target))
      code = "transition_edge_denied";
    else if (resultRequired && !input.hasResult)
      code = "transition_result_required";
    else if (
      (target === "failed" || target === "cancelled" || target === "killed") &&
      !input.reason.trim()
    )
      code = "transition_reason_required";
    else if (target === "killed" && !task.scope.allowKill)
      code = "transition_kill_denied";
    const payload = {
      accepted: code === "transition_accepted",
      code,
      source: task.status,
      target,
      taskId: task.identity.taskId,
      actorTaskId: input.actorTaskId,
      expectedRevision: input.expectedRevision,
      currentRevision: task.revision,
      leaseMatched,
      actorMatched,
      resultRequired,
      terminalFence,
      reason: input.reason.trim(),
      decidedAt: input.now ?? new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(input: LifecycleDecisionInput): LifecycleDecision {
    const decision = this.decide(input);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `task ${decision.taskId} rejected ${input.intent}`,
        {
          source: decision.source,
          target: decision.target,
          currentRevision: decision.currentRevision,
          expectedRevision: decision.expectedRevision,
        },
      );
    return decision;
  }

  allowed(task: E03TaskState): AgentTaskPhase[] {
    return [...ALLOWED[task.status]];
  }

  can(task: E03TaskState, target: AgentTaskPhase): boolean {
    return ALLOWED[task.status].includes(target);
  }
}

export interface LifecycleAuditRecord {
  auditId: string;
  taskId: string;
  sessionId: string;
  leaseId: string;
  attempt: number;
  intent: LifecycleIntent;
  source: AgentTaskPhase;
  target: AgentTaskPhase;
  revisionBefore: number;
  revisionAfter: number;
  transitionId: string;
  requestId: string;
  idempotencyKey: string;
  writerId: string;
  eventType: string;
  accepted: boolean;
  code: string;
  recordedAt: string;
  previousDigest: string;
  digest: string;
}

function assertLifecycleAudit(record: LifecycleAuditRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "lifecycle_audit_checksum",
      `lifecycle audit ${record.auditId} checksum mismatch`,
    );
  if (!record.auditId || !record.taskId || !record.sessionId)
    throw new E03RuntimeError(
      "lifecycle_audit_identity",
      "lifecycle audit identity is incomplete",
    );
  if (record.revisionBefore < 1 || record.revisionAfter < record.revisionBefore)
    throw new E03RuntimeError(
      "lifecycle_audit_revision",
      "lifecycle audit revision is invalid",
    );
}

export class TaskLifecycleAudit {
  private records: LifecycleAuditRecord[] = [];
  private byTransition = new Map<string, LifecycleAuditRecord>();
  private byIdempotency = new Map<string, LifecycleAuditRecord>();

  record(
    before: E03TaskState,
    after: E03TaskState,
    transition: E03Transition,
    intent: LifecycleIntent,
    decision: LifecycleDecision,
  ): LifecycleAuditRecord {
    if (before.identity.taskId !== after.identity.taskId)
      throw new E03RuntimeError(
        "lifecycle_audit_task_swap",
        "lifecycle audit cannot span different tasks",
      );
    if (after.revision !== before.revision + 1)
      throw new E03RuntimeError(
        "lifecycle_audit_revision_gap",
        "lifecycle audit requires an adjacent revision",
      );
    if (transition.taskId !== before.identity.taskId)
      throw new E03RuntimeError(
        "lifecycle_audit_transition_task",
        "transition belongs to another task",
      );
    const prior = this.byIdempotency.get(transition.idempotencyKey);
    if (prior) {
      if (prior.transitionId !== transition.transitionId)
        throw new E03RuntimeError(
          "lifecycle_audit_idempotency_conflict",
          "idempotency key identifies another transition",
        );
      return structuredClone(prior);
    }
    const previousDigest = this.records.at(-1)?.digest ?? "";
    const payload = {
      auditId: `lifecycle-audit-${digest({
        taskId: before.identity.taskId,
        transitionId: transition.transitionId,
        previousDigest,
      }).slice(0, 32)}`,
      taskId: before.identity.taskId,
      sessionId: before.identity.sessionId,
      leaseId: before.identity.leaseId,
      attempt: before.identity.attempt,
      intent,
      source: before.status,
      target: after.status,
      revisionBefore: before.revision,
      revisionAfter: after.revision,
      transitionId: transition.transitionId,
      requestId: transition.requestId,
      idempotencyKey: transition.idempotencyKey,
      writerId: transition.writerId,
      eventType: transition.eventType,
      accepted: decision.accepted,
      code: decision.code,
      recordedAt: after.updatedAt,
      previousDigest,
    };
    const record = { ...payload, digest: digest(payload) };
    assertLifecycleAudit(record);
    this.records.push(record);
    this.byTransition.set(record.transitionId, record);
    this.byIdempotency.set(record.idempotencyKey, record);
    return structuredClone(record);
  }

  restore(records: readonly LifecycleAuditRecord[]): void {
    const next: LifecycleAuditRecord[] = [];
    const transitions = new Map<string, LifecycleAuditRecord>();
    const idempotency = new Map<string, LifecycleAuditRecord>();
    let previousDigest = "";
    for (const raw of records) {
      const record = structuredClone(raw);
      assertLifecycleAudit(record);
      if (record.previousDigest !== previousDigest)
        throw new E03RuntimeError(
          "lifecycle_audit_chain",
          `lifecycle audit ${record.auditId} breaks the digest chain`,
        );
      if (transitions.has(record.transitionId))
        throw new E03RuntimeError(
          "duplicate_lifecycle_transition",
          `transition ${record.transitionId} has duplicate audit records`,
        );
      if (idempotency.has(record.idempotencyKey))
        throw new E03RuntimeError(
          "duplicate_lifecycle_idempotency",
          `idempotency key ${record.idempotencyKey} repeats`,
        );
      next.push(record);
      transitions.set(record.transitionId, record);
      idempotency.set(record.idempotencyKey, record);
      previousDigest = record.digest;
    }
    this.records = next;
    this.byTransition = transitions;
    this.byIdempotency = idempotency;
  }

  snapshot(taskId?: string): LifecycleAuditRecord[] {
    return this.records
      .filter((record) => !taskId || record.taskId === taskId)
      .map((record) => structuredClone(record));
  }

  verifyTask(task: E03TaskState): void {
    const records = this.snapshot(task.identity.taskId);
    for (let index = 1; index < records.length; index += 1) {
      const prior = records[index - 1]!;
      const current = records[index]!;
      if (current.revisionBefore !== prior.revisionAfter)
        throw new E03RuntimeError(
          "lifecycle_audit_task_gap",
          `task ${task.identity.taskId} audit revisions are discontinuous`,
        );
    }
    const last = records.at(-1);
    if (last && last.revisionAfter > task.revision)
      throw new E03RuntimeError(
        "lifecycle_audit_ahead",
        "lifecycle audit is ahead of canonical task state",
      );
  }
}

export interface LateResultEnvelope {
  resultId: string;
  taskId: string;
  sessionId: string;
  leaseId: string;
  attempt: number;
  expectedRevision: number;
  workerId: string;
  resultDigest: string;
  artifactDigests: string[];
  usageDigest: string;
  completedAt: string;
  idempotencyKey: string;
  digest: string;
}

export interface LateResultDecision {
  accepted: boolean;
  code: string;
  resultId: string;
  taskId: string;
  currentStatus: AgentTaskPhase;
  currentRevision: number;
  currentLeaseId: string;
  currentAttempt: number;
  decidedAt: string;
  digest: string;
}

function assertLateResultEnvelope(envelope: LateResultEnvelope): void {
  const { digest: checksum, ...payload } = envelope;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "late_result_checksum",
      `result ${envelope.resultId} checksum mismatch`,
    );
  if (!envelope.resultId || !envelope.taskId || !envelope.workerId)
    throw new E03RuntimeError(
      "late_result_identity",
      "result identity is incomplete",
    );
  if (envelope.expectedRevision < 1 || envelope.attempt < 1)
    throw new E03RuntimeError(
      "late_result_revision",
      "result revision or attempt is invalid",
    );
}

export class LateResultFence {
  private readonly decisions = new Map<string, LateResultDecision>();
  private readonly resultByIdempotency = new Map<string, string>();

  seal(
    input: Omit<LateResultEnvelope, "resultId" | "digest">,
  ): LateResultEnvelope {
    const payload = {
      ...structuredClone(input),
      resultId: `task-result-${digest({
        taskId: input.taskId,
        leaseId: input.leaseId,
        expectedRevision: input.expectedRevision,
        idempotencyKey: input.idempotencyKey,
      }).slice(0, 32)}`,
      artifactDigests: [...input.artifactDigests].sort(),
    };
    const envelope = { ...payload, digest: digest(payload) };
    assertLateResultEnvelope(envelope);
    return envelope;
  }

  decide(task: E03TaskState, envelope: LateResultEnvelope): LateResultDecision {
    assertLateResultEnvelope(envelope);
    const priorResultId = this.resultByIdempotency.get(envelope.idempotencyKey);
    if (priorResultId && priorResultId !== envelope.resultId)
      throw new E03RuntimeError(
        "result_idempotency_conflict",
        "result idempotency key identifies another result",
      );
    const prior = this.decisions.get(envelope.resultId);
    if (prior) return structuredClone(prior);
    let code = "result_accepted";
    if (task.identity.taskId !== envelope.taskId) code = "result_task_mismatch";
    else if (task.identity.sessionId !== envelope.sessionId)
      code = "result_session_mismatch";
    else if (task.identity.leaseId !== envelope.leaseId)
      code = "result_stale_lease";
    else if (task.identity.attempt !== envelope.attempt)
      code = "result_stale_attempt";
    else if (task.revision !== envelope.expectedRevision)
      code = "result_stale_revision";
    else if (task.status !== "running" && task.status !== "waiting")
      code = isTerminal(task.status)
        ? "result_terminal_fence"
        : "result_not_running";
    const payload = {
      accepted: code === "result_accepted",
      code,
      resultId: envelope.resultId,
      taskId: task.identity.taskId,
      currentStatus: task.status,
      currentRevision: task.revision,
      currentLeaseId: task.identity.leaseId,
      currentAttempt: task.identity.attempt,
      decidedAt: new Date().toISOString(),
    };
    const decision = { ...payload, digest: digest(payload) };
    this.decisions.set(envelope.resultId, decision);
    this.resultByIdempotency.set(envelope.idempotencyKey, envelope.resultId);
    return structuredClone(decision);
  }

  assert(task: E03TaskState, envelope: LateResultEnvelope): LateResultDecision {
    const decision = this.decide(task, envelope);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `result ${decision.resultId} rejected for task ${decision.taskId}`,
        {
          status: decision.currentStatus,
          revision: decision.currentRevision,
          leaseId: decision.currentLeaseId,
          attempt: decision.currentAttempt,
        },
      );
    return decision;
  }

  snapshot(): LateResultDecision[] {
    return [...this.decisions.values()]
      .sort((left, right) => left.resultId.localeCompare(right.resultId))
      .map((decision) => structuredClone(decision));
  }
}

export type CascadeAction = "cancel" | "kill";

export interface CascadeNode {
  taskId: string;
  parentTaskId: string | null;
  status: AgentTaskPhase;
  revision: number;
  leaseId: string;
  depth: number;
  childTaskIds: string[];
  checksum: string;
}

export interface CascadeInstruction {
  instructionId: string;
  rootTaskId: string;
  taskId: string;
  parentTaskId: string | null;
  action: CascadeAction;
  expectedRevision: number;
  leaseId: string;
  depth: number;
  order: number;
  reason: string;
  idempotencyKey: string;
  terminalNoop: boolean;
  digest: string;
}

export interface CascadePlan {
  planId: string;
  rootTaskId: string;
  action: CascadeAction;
  reason: string;
  requestedBy: string;
  instructions: CascadeInstruction[];
  terminalTaskIds: string[];
  missingTaskIds: string[];
  plannedAt: string;
  digest: string;
}

function cascadeNode(task: E03TaskState, depth: number): CascadeNode {
  return {
    taskId: task.identity.taskId,
    parentTaskId: task.identity.parentTaskId,
    status: task.status,
    revision: task.revision,
    leaseId: task.identity.leaseId,
    depth,
    childTaskIds: [...task.childTaskIds].sort(),
    checksum: task.checksum,
  };
}

function assertCascadePlan(plan: CascadePlan): void {
  const { digest: checksum, ...payload } = plan;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "cascade_plan_checksum",
      `cascade plan ${plan.planId} checksum mismatch`,
    );
  const taskIds = new Set<string>();
  const orders = new Set<number>();
  for (const instruction of plan.instructions) {
    const { digest: instructionChecksum, ...instructionPayload } = instruction;
    if (digest(instructionPayload) !== instructionChecksum)
      throw new E03RuntimeError(
        "cascade_instruction_checksum",
        `cascade instruction ${instruction.instructionId} checksum mismatch`,
      );
    if (taskIds.has(instruction.taskId))
      throw new E03RuntimeError(
        "duplicate_cascade_task",
        `cascade task ${instruction.taskId} repeats`,
      );
    if (orders.has(instruction.order))
      throw new E03RuntimeError(
        "duplicate_cascade_order",
        `cascade order ${instruction.order} repeats`,
      );
    taskIds.add(instruction.taskId);
    orders.add(instruction.order);
  }
}

export class TaskCancellationCascade {
  plan(input: {
    rootTaskId: string;
    action: CascadeAction;
    reason: string;
    requestedBy: string;
    tasks: readonly E03TaskState[];
  }): CascadePlan {
    const rootTaskId = input.rootTaskId.trim();
    const reason = input.reason.trim();
    const requestedBy = input.requestedBy.trim();
    if (!rootTaskId || !reason || !requestedBy)
      throw new E03RuntimeError(
        "cascade_input_invalid",
        "cascade root, reason and requester are required",
      );
    const byId = new Map(
      input.tasks.map((task) => [task.identity.taskId, structuredClone(task)]),
    );
    const root = byId.get(rootTaskId);
    if (!root)
      throw new E03RuntimeError(
        "cascade_root_missing",
        `cascade root ${rootTaskId} is missing`,
      );
    const nodes: CascadeNode[] = [];
    const missingTaskIds: string[] = [];
    const seen = new Set<string>();
    const queue: Array<{ task: E03TaskState; depth: number }> = [
      { task: root, depth: 0 },
    ];
    while (queue.length) {
      const current = queue.shift()!;
      if (seen.has(current.task.identity.taskId))
        throw new E03RuntimeError(
          "cascade_cycle",
          `task ${current.task.identity.taskId} appears twice in cascade`,
        );
      seen.add(current.task.identity.taskId);
      nodes.push(cascadeNode(current.task, current.depth));
      for (const childTaskId of [...current.task.childTaskIds].sort()) {
        const child = byId.get(childTaskId);
        if (!child) {
          missingTaskIds.push(childTaskId);
          continue;
        }
        if (child.identity.parentTaskId !== current.task.identity.taskId)
          throw new E03RuntimeError(
            "cascade_parent_mismatch",
            `child ${childTaskId} does not point to ${current.task.identity.taskId}`,
          );
        queue.push({ task: child, depth: current.depth + 1 });
      }
    }
    const ordered = nodes.sort(
      (left, right) =>
        right.depth - left.depth || left.taskId.localeCompare(right.taskId),
    );
    const terminalTaskIds: string[] = [];
    const instructions = ordered.map((node, order) => {
      const terminalNoop = isTerminal(node.status);
      if (terminalNoop) terminalTaskIds.push(node.taskId);
      const instructionPayload = {
        instructionId: `cascade-instruction-${digest({
          rootTaskId,
          taskId: node.taskId,
          action: input.action,
          revision: node.revision,
        }).slice(0, 32)}`,
        rootTaskId,
        taskId: node.taskId,
        parentTaskId: node.parentTaskId,
        action: input.action,
        expectedRevision: node.revision,
        leaseId: node.leaseId,
        depth: node.depth,
        order,
        reason,
        idempotencyKey: `cascade:${rootTaskId}:${node.taskId}:${input.action}:${node.revision}`,
        terminalNoop,
      };
      return { ...instructionPayload, digest: digest(instructionPayload) };
    });
    const payload = {
      planId: `cascade-plan-${digest({
        rootTaskId,
        action: input.action,
        requestedBy,
        taskChecksums: nodes.map((node) => node.checksum),
      }).slice(0, 32)}`,
      rootTaskId,
      action: input.action,
      reason,
      requestedBy,
      instructions,
      terminalTaskIds: terminalTaskIds.sort(),
      missingTaskIds: [...new Set(missingTaskIds)].sort(),
      plannedAt: new Date().toISOString(),
    };
    const plan = { ...payload, digest: digest(payload) };
    assertCascadePlan(plan);
    return plan;
  }

  verify(plan: CascadePlan, tasks: readonly E03TaskState[]): void {
    assertCascadePlan(plan);
    const byId = new Map(tasks.map((task) => [task.identity.taskId, task]));
    for (const instruction of plan.instructions) {
      const task = byId.get(instruction.taskId);
      if (!task) {
        if (!plan.missingTaskIds.includes(instruction.taskId))
          throw new E03RuntimeError(
            "cascade_task_lost",
            `cascade task ${instruction.taskId} is unavailable`,
          );
        continue;
      }
      if (task.revision !== instruction.expectedRevision)
        throw new E03RuntimeError(
          "cascade_stale_revision",
          `cascade task ${instruction.taskId} changed after planning`,
        );
      if (task.identity.leaseId !== instruction.leaseId)
        throw new E03RuntimeError(
          "cascade_stale_lease",
          `cascade task ${instruction.taskId} lease changed after planning`,
        );
    }
  }

  remaining(
    plan: CascadePlan,
    tasks: readonly E03TaskState[],
  ): CascadeInstruction[] {
    assertCascadePlan(plan);
    const byId = new Map(tasks.map((task) => [task.identity.taskId, task]));
    return plan.instructions
      .filter((instruction) => {
        const task = byId.get(instruction.taskId);
        if (!task) return false;
        if (instruction.action === "cancel") return task.status !== "cancelled";
        return task.status !== "killed";
      })
      .map((instruction) => structuredClone(instruction));
  }
}

export interface RetryPolicyInput {
  task: E03TaskState;
  failureCode: string;
  failureMessage: string;
  retryable: boolean;
  maximumAttempts: number;
  baseDelayMs: number;
  maximumDelayMs: number;
  jitterSeed: string;
  now?: string;
}

export interface RetryDecision {
  accepted: boolean;
  code: string;
  taskId: string;
  currentAttempt: number;
  nextAttempt: number;
  failureCode: string;
  failureDigest: string;
  delayMs: number;
  retryAt: string | null;
  expectedRevision: number;
  currentLeaseId: string;
  idempotencyKey: string;
  decidedAt: string;
  digest: string;
}

export class TaskRetryPolicy {
  decide(input: RetryPolicyInput): RetryDecision {
    if (
      !Number.isSafeInteger(input.maximumAttempts) ||
      input.maximumAttempts < 1
    )
      throw new E03RuntimeError(
        "retry_maximum_invalid",
        "maximum retry attempts is invalid",
      );
    if (
      !Number.isSafeInteger(input.baseDelayMs) ||
      !Number.isSafeInteger(input.maximumDelayMs) ||
      input.baseDelayMs < 0 ||
      input.maximumDelayMs < input.baseDelayMs
    )
      throw new E03RuntimeError(
        "retry_delay_invalid",
        "retry delay bounds are invalid",
      );
    const task = input.task;
    let code = "retry_accepted";
    if (!input.retryable) code = "retry_failure_not_retryable";
    else if (task.status !== "failed") code = "retry_task_not_failed";
    else if (task.identity.attempt >= input.maximumAttempts)
      code = "retry_attempts_exhausted";
    else if (!input.failureCode.trim()) code = "retry_failure_code_missing";
    const exponent = Math.max(0, task.identity.attempt - 1);
    const nominal = Math.min(
      input.maximumDelayMs,
      input.baseDelayMs * 2 ** exponent,
    );
    const jitterUnit = Number.parseInt(
      digest({
        taskId: task.identity.taskId,
        attempt: task.identity.attempt,
        seed: input.jitterSeed,
      }).slice(0, 8),
      16,
    );
    const jitter = nominal === 0 ? 0 : jitterUnit % Math.max(1, nominal / 4);
    const delayMs = nominal + jitter;
    const decidedAt = input.now ?? new Date().toISOString();
    const retryAt =
      code === "retry_accepted"
        ? new Date(Date.parse(decidedAt) + delayMs).toISOString()
        : null;
    const payload = {
      accepted: code === "retry_accepted",
      code,
      taskId: task.identity.taskId,
      currentAttempt: task.identity.attempt,
      nextAttempt: task.identity.attempt + 1,
      failureCode: input.failureCode.trim(),
      failureDigest: digest({
        code: input.failureCode.trim(),
        message: input.failureMessage,
      }),
      delayMs,
      retryAt,
      expectedRevision: task.revision,
      currentLeaseId: task.identity.leaseId,
      idempotencyKey: `retry:${task.identity.taskId}:${task.identity.attempt + 1}:${task.revision}`,
      decidedAt,
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(input: RetryPolicyInput): RetryDecision {
    const decision = this.decide(input);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `task ${decision.taskId} cannot be retried`,
        {
          attempt: decision.currentAttempt,
          failureCode: decision.failureCode,
        },
      );
    return decision;
  }
}

export interface TaskDeadline {
  deadlineId: string;
  taskId: string;
  leaseId: string;
  attempt: number;
  expectedRevision: number;
  kind: "queue" | "execution" | "wait" | "total";
  startedAt: string;
  expiresAt: string;
  timeoutMs: number;
  reason: string;
  idempotencyKey: string;
  digest: string;
}

export interface DeadlineEvaluation {
  deadlineId: string;
  taskId: string;
  expired: boolean;
  stale: boolean;
  code: string;
  remainingMs: number;
  evaluatedAt: string;
  digest: string;
}

function assertDeadline(deadline: TaskDeadline): void {
  const { digest: checksum, ...payload } = deadline;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "deadline_checksum",
      `deadline ${deadline.deadlineId} checksum mismatch`,
    );
  if (Date.parse(deadline.expiresAt) <= Date.parse(deadline.startedAt))
    throw new E03RuntimeError(
      "deadline_window_invalid",
      "deadline expiry must follow its start",
    );
  if (!Number.isSafeInteger(deadline.timeoutMs) || deadline.timeoutMs < 1)
    throw new E03RuntimeError(
      "deadline_timeout_invalid",
      "deadline timeout is invalid",
    );
}

export class TaskDeadlinePolicyRuntime {
  private deadlines = new Map<string, TaskDeadline>();

  schedule(input: {
    task: E03TaskState;
    kind: TaskDeadline["kind"];
    timeoutMs: number;
    reason: string;
    now?: string;
  }): TaskDeadline {
    if (!Number.isSafeInteger(input.timeoutMs) || input.timeoutMs < 1)
      throw new E03RuntimeError(
        "deadline_timeout_invalid",
        "deadline timeout is invalid",
      );
    const startedAt = input.now ?? new Date().toISOString();
    const payload = {
      deadlineId: `task-deadline-${digest({
        taskId: input.task.identity.taskId,
        leaseId: input.task.identity.leaseId,
        revision: input.task.revision,
        kind: input.kind,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      leaseId: input.task.identity.leaseId,
      attempt: input.task.identity.attempt,
      expectedRevision: input.task.revision,
      kind: input.kind,
      startedAt,
      expiresAt: new Date(
        Date.parse(startedAt) + input.timeoutMs,
      ).toISOString(),
      timeoutMs: input.timeoutMs,
      reason: input.reason.trim() || `${input.kind}_timeout`,
      idempotencyKey: `deadline:${input.task.identity.taskId}:${input.kind}:${input.task.revision}`,
    };
    const deadline = { ...payload, digest: digest(payload) };
    assertDeadline(deadline);
    const existing = this.deadlines.get(deadline.deadlineId);
    if (existing) return structuredClone(existing);
    this.deadlines.set(deadline.deadlineId, deadline);
    return structuredClone(deadline);
  }

  evaluate(
    task: E03TaskState,
    deadlineId: string,
    now = new Date().toISOString(),
  ): DeadlineEvaluation {
    const deadline = this.deadlines.get(deadlineId);
    if (!deadline)
      throw new E03RuntimeError(
        "deadline_missing",
        `deadline ${deadlineId} is missing`,
      );
    assertDeadline(deadline);
    const stale =
      deadline.taskId !== task.identity.taskId ||
      deadline.leaseId !== task.identity.leaseId ||
      deadline.attempt !== task.identity.attempt ||
      deadline.expectedRevision > task.revision;
    const remainingMs = Date.parse(deadline.expiresAt) - Date.parse(now);
    let code = "deadline_pending";
    if (stale) code = "deadline_stale";
    else if (isTerminal(task.status)) code = "deadline_terminal_noop";
    else if (remainingMs <= 0) code = "deadline_expired";
    const payload = {
      deadlineId,
      taskId: task.identity.taskId,
      expired: code === "deadline_expired",
      stale,
      code,
      remainingMs: Math.max(0, remainingMs),
      evaluatedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  dismiss(deadlineId: string): TaskDeadline | null {
    const deadline = this.deadlines.get(deadlineId);
    if (!deadline) return null;
    this.deadlines.delete(deadlineId);
    return structuredClone(deadline);
  }

  restore(deadlines: readonly TaskDeadline[]): void {
    const next = new Map<string, TaskDeadline>();
    for (const raw of deadlines) {
      const deadline = structuredClone(raw);
      assertDeadline(deadline);
      if (next.has(deadline.deadlineId))
        throw new E03RuntimeError(
          "duplicate_deadline",
          `deadline ${deadline.deadlineId} repeats`,
        );
      next.set(deadline.deadlineId, deadline);
    }
    this.deadlines = next;
  }

  snapshot(taskId?: string): TaskDeadline[] {
    return [...this.deadlines.values()]
      .filter((deadline) => !taskId || deadline.taskId === taskId)
      .sort(
        (left, right) =>
          left.expiresAt.localeCompare(right.expiresAt) ||
          left.deadlineId.localeCompare(right.deadlineId),
      )
      .map((deadline) => structuredClone(deadline));
  }
}

export type DependencyKind =
  | "completion"
  | "artifact"
  | "approval"
  | "message"
  | "resource";

export interface TaskDependency {
  dependencyId: string;
  ownerTaskId: string;
  prerequisiteTaskId: string;
  kind: DependencyKind;
  requiredArtifactKind: string | null;
  requiredMessageKind: string | null;
  requiredResource: string | null;
  optional: boolean;
  createdAt: string;
  satisfiedAt: string | null;
  satisfactionDigest: string | null;
  digest: string;
}

export interface DependencyEvaluation {
  taskId: string;
  ready: boolean;
  pendingDependencyIds: string[];
  satisfiedDependencyIds: string[];
  failedDependencyIds: string[];
  optionalDependencyIds: string[];
  evaluatedAt: string;
  digest: string;
}

function assertDependency(dependency: TaskDependency): void {
  const { digest: checksum, ...payload } = dependency;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_dependency_checksum",
      `dependency ${dependency.dependencyId} checksum mismatch`,
    );
  if (
    !dependency.ownerTaskId ||
    !dependency.prerequisiteTaskId ||
    dependency.ownerTaskId === dependency.prerequisiteTaskId
  )
    throw new E03RuntimeError(
      "task_dependency_identity",
      "dependency identity is invalid",
    );
  if (
    dependency.kind === "artifact" &&
    !dependency.requiredArtifactKind?.trim()
  )
    throw new E03RuntimeError(
      "task_dependency_artifact_kind",
      "artifact dependency requires an artifact kind",
    );
  if (dependency.kind === "message" && !dependency.requiredMessageKind?.trim())
    throw new E03RuntimeError(
      "task_dependency_message_kind",
      "message dependency requires a message kind",
    );
  if (dependency.kind === "resource" && !dependency.requiredResource?.trim())
    throw new E03RuntimeError(
      "task_dependency_resource",
      "resource dependency requires a resource identifier",
    );
}

export class TaskDependencyGraph {
  private dependencies = new Map<string, TaskDependency>();
  private byOwner = new Map<string, Set<string>>();
  private byPrerequisite = new Map<string, Set<string>>();

  add(input: {
    owner: E03TaskState;
    prerequisite: E03TaskState;
    kind: DependencyKind;
    requiredArtifactKind?: string;
    requiredMessageKind?: string;
    requiredResource?: string;
    optional?: boolean;
  }): TaskDependency {
    if (
      input.owner.identity.sessionId !== input.prerequisite.identity.sessionId
    )
      throw new E03RuntimeError(
        "cross_session_dependency",
        "task dependencies cannot cross sessions",
      );
    if (isTerminal(input.owner.status))
      throw new E03RuntimeError(
        "terminal_dependency_owner",
        "terminal task cannot acquire a dependency",
      );
    const dependencyId = `task-dependency-${digest({
      ownerTaskId: input.owner.identity.taskId,
      prerequisiteTaskId: input.prerequisite.identity.taskId,
      kind: input.kind,
      requiredArtifactKind: input.requiredArtifactKind ?? null,
      requiredMessageKind: input.requiredMessageKind ?? null,
      requiredResource: input.requiredResource ?? null,
    }).slice(0, 32)}`;
    const existing = this.dependencies.get(dependencyId);
    if (existing) return structuredClone(existing);
    const payload = {
      dependencyId,
      ownerTaskId: input.owner.identity.taskId,
      prerequisiteTaskId: input.prerequisite.identity.taskId,
      kind: input.kind,
      requiredArtifactKind: input.requiredArtifactKind?.trim() || null,
      requiredMessageKind: input.requiredMessageKind?.trim() || null,
      requiredResource: input.requiredResource?.trim() || null,
      optional: input.optional ?? false,
      createdAt: new Date().toISOString(),
      satisfiedAt: null,
      satisfactionDigest: null,
    };
    const dependency = { ...payload, digest: digest(payload) };
    assertDependency(dependency);
    this.assertNoCycle(dependency.ownerTaskId, dependency.prerequisiteTaskId);
    this.dependencies.set(dependencyId, dependency);
    this.index(dependency);
    return structuredClone(dependency);
  }

  satisfy(
    dependencyId: string,
    input: { proof: unknown; task: E03TaskState; now?: string },
  ): TaskDependency {
    const current = this.dependencies.get(dependencyId);
    if (!current)
      throw new E03RuntimeError(
        "task_dependency_missing",
        `dependency ${dependencyId} is missing`,
      );
    assertDependency(current);
    if (current.satisfiedAt) return structuredClone(current);
    if (input.task.identity.taskId !== current.prerequisiteTaskId)
      throw new E03RuntimeError(
        "task_dependency_proof_owner",
        "dependency proof belongs to another task",
      );
    this.assertProof(current, input.task, input.proof);
    const nextPayload = {
      ...current,
      satisfiedAt: input.now ?? new Date().toISOString(),
      satisfactionDigest: digest({
        proof: input.proof,
        taskChecksum: input.task.checksum,
      }),
      digest: "",
    };
    const { digest: _, ...payload } = nextPayload;
    const next = { ...payload, digest: digest(payload) };
    assertDependency(next);
    this.dependencies.set(dependencyId, next);
    return structuredClone(next);
  }

  evaluate(
    task: E03TaskState,
    tasks: readonly E03TaskState[],
  ): DependencyEvaluation {
    const byId = new Map(tasks.map((item) => [item.identity.taskId, item]));
    byId.set(task.identity.taskId, task);
    const dependencies = this.forOwner(task.identity.taskId);
    const pendingDependencyIds: string[] = [];
    const satisfiedDependencyIds: string[] = [];
    const failedDependencyIds: string[] = [];
    const optionalDependencyIds: string[] = [];
    for (const dependency of dependencies) {
      if (dependency.optional)
        optionalDependencyIds.push(dependency.dependencyId);
      if (dependency.satisfiedAt) {
        satisfiedDependencyIds.push(dependency.dependencyId);
        continue;
      }
      const prerequisite = byId.get(dependency.prerequisiteTaskId);
      if (!prerequisite) {
        if (!dependency.optional)
          failedDependencyIds.push(dependency.dependencyId);
        continue;
      }
      if (
        prerequisite.status === "failed" ||
        prerequisite.status === "cancelled" ||
        prerequisite.status === "killed"
      ) {
        if (!dependency.optional)
          failedDependencyIds.push(dependency.dependencyId);
        continue;
      }
      pendingDependencyIds.push(dependency.dependencyId);
    }
    const payload = {
      taskId: task.identity.taskId,
      ready:
        failedDependencyIds.length === 0 &&
        pendingDependencyIds.filter(
          (dependencyId) => !optionalDependencyIds.includes(dependencyId),
        ).length === 0,
      pendingDependencyIds: pendingDependencyIds.sort(),
      satisfiedDependencyIds: satisfiedDependencyIds.sort(),
      failedDependencyIds: failedDependencyIds.sort(),
      optionalDependencyIds: optionalDependencyIds.sort(),
      evaluatedAt: new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  remove(dependencyId: string): TaskDependency | null {
    const dependency = this.dependencies.get(dependencyId);
    if (!dependency) return null;
    this.dependencies.delete(dependencyId);
    this.byOwner.get(dependency.ownerTaskId)?.delete(dependencyId);
    this.byPrerequisite
      .get(dependency.prerequisiteTaskId)
      ?.delete(dependencyId);
    return structuredClone(dependency);
  }

  forOwner(taskId: string): TaskDependency[] {
    return [...(this.byOwner.get(taskId) ?? new Set<string>())]
      .map((dependencyId) => this.dependencies.get(dependencyId)!)
      .sort(
        (left, right) =>
          left.createdAt.localeCompare(right.createdAt) ||
          left.dependencyId.localeCompare(right.dependencyId),
      )
      .map((dependency) => structuredClone(dependency));
  }

  forPrerequisite(taskId: string): TaskDependency[] {
    return [...(this.byPrerequisite.get(taskId) ?? new Set<string>())]
      .map((dependencyId) => this.dependencies.get(dependencyId)!)
      .sort((left, right) =>
        left.dependencyId.localeCompare(right.dependencyId),
      )
      .map((dependency) => structuredClone(dependency));
  }

  restore(dependencies: readonly TaskDependency[]): void {
    const next = new Map<string, TaskDependency>();
    for (const raw of dependencies) {
      const dependency = structuredClone(raw);
      assertDependency(dependency);
      if (next.has(dependency.dependencyId))
        throw new E03RuntimeError(
          "duplicate_task_dependency",
          `dependency ${dependency.dependencyId} repeats`,
        );
      next.set(dependency.dependencyId, dependency);
    }
    this.dependencies = next;
    this.byOwner.clear();
    this.byPrerequisite.clear();
    for (const dependency of next.values()) this.index(dependency);
    for (const dependency of next.values())
      this.assertNoCycle(
        dependency.ownerTaskId,
        dependency.prerequisiteTaskId,
        dependency.dependencyId,
      );
  }

  snapshot(): TaskDependency[] {
    return [...this.dependencies.values()]
      .sort((left, right) =>
        left.dependencyId.localeCompare(right.dependencyId),
      )
      .map((dependency) => structuredClone(dependency));
  }

  private index(dependency: TaskDependency): void {
    const owners =
      this.byOwner.get(dependency.ownerTaskId) ?? new Set<string>();
    owners.add(dependency.dependencyId);
    this.byOwner.set(dependency.ownerTaskId, owners);
    const prerequisites =
      this.byPrerequisite.get(dependency.prerequisiteTaskId) ??
      new Set<string>();
    prerequisites.add(dependency.dependencyId);
    this.byPrerequisite.set(dependency.prerequisiteTaskId, prerequisites);
  }

  private assertNoCycle(
    ownerTaskId: string,
    prerequisiteTaskId: string,
    ignoredDependencyId = "",
  ): void {
    const queue = [prerequisiteTaskId];
    const seen = new Set<string>();
    while (queue.length) {
      const current = queue.shift()!;
      if (current === ownerTaskId)
        throw new E03RuntimeError(
          "task_dependency_cycle",
          `dependency ${ownerTaskId}->${prerequisiteTaskId} creates a cycle`,
        );
      if (seen.has(current)) continue;
      seen.add(current);
      for (const dependency of this.forOwner(current)) {
        if (dependency.dependencyId === ignoredDependencyId) continue;
        queue.push(dependency.prerequisiteTaskId);
      }
    }
  }

  private assertProof(
    dependency: TaskDependency,
    task: E03TaskState,
    proof: unknown,
  ): void {
    if (dependency.kind === "completion" && task.status !== "completed")
      throw new E03RuntimeError(
        "dependency_completion_missing",
        "completion dependency requires a completed task",
      );
    if (
      dependency.kind === "artifact" &&
      !task.artifacts.some(
        (artifact) => artifact.kind === dependency.requiredArtifactKind,
      )
    )
      throw new E03RuntimeError(
        "dependency_artifact_missing",
        `artifact kind ${dependency.requiredArtifactKind} is missing`,
      );
    if (
      dependency.kind === "message" &&
      !task.messages.some(
        (message) => message.kind === dependency.requiredMessageKind,
      )
    )
      throw new E03RuntimeError(
        "dependency_message_missing",
        `message kind ${dependency.requiredMessageKind} is missing`,
      );
    if (dependency.kind === "approval" && !proof)
      throw new E03RuntimeError(
        "dependency_approval_missing",
        "approval dependency requires proof",
      );
    if (dependency.kind === "resource" && !proof)
      throw new E03RuntimeError(
        "dependency_resource_missing",
        "resource dependency requires proof",
      );
  }
}

export type ProgressActivityKind =
  | "queued"
  | "started"
  | "model"
  | "tool"
  | "message"
  | "artifact"
  | "waiting"
  | "resumed"
  | "completed"
  | "failed"
  | "cancelled"
  | "killed";

export interface TaskProgressActivity {
  activityId: string;
  taskId: string;
  leaseId: string;
  attempt: number;
  revision: number;
  sequence: number;
  kind: ProgressActivityKind;
  summary: string;
  detailDigest: string;
  tokensIn: number;
  tokensOut: number;
  toolCalls: number;
  artifacts: number;
  percent: number | null;
  occurredAt: string;
  previousDigest: string;
  digest: string;
}

export interface TaskProgressProjection {
  taskId: string;
  status: AgentTaskPhase;
  revision: number;
  attempt: number;
  activities: number;
  tokensIn: number;
  tokensOut: number;
  toolCalls: number;
  artifacts: number;
  percent: number | null;
  lastSummary: string;
  lastActivityAt: string | null;
  idleMs: number;
  terminal: boolean;
  digest: string;
}

function assertProgressActivity(activity: TaskProgressActivity): void {
  const { digest: checksum, ...payload } = activity;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "progress_activity_checksum",
      `activity ${activity.activityId} checksum mismatch`,
    );
  if (activity.sequence < 1 || activity.revision < 1 || activity.attempt < 1)
    throw new E03RuntimeError(
      "progress_activity_identity",
      "activity sequence, revision or attempt is invalid",
    );
  if (
    activity.percent !== null &&
    (activity.percent < 0 || activity.percent > 100)
  )
    throw new E03RuntimeError(
      "progress_activity_percent",
      "activity percent is outside 0..100",
    );
  if (
    activity.tokensIn < 0 ||
    activity.tokensOut < 0 ||
    activity.toolCalls < 0 ||
    activity.artifacts < 0
  )
    throw new E03RuntimeError(
      "progress_activity_counter",
      "activity counters cannot be negative",
    );
}

export class TaskProgressRuntime {
  private activities = new Map<string, TaskProgressActivity[]>();

  append(input: {
    task: E03TaskState;
    kind: ProgressActivityKind;
    summary: string;
    detail?: unknown;
    tokensIn?: number;
    tokensOut?: number;
    toolCalls?: number;
    artifacts?: number;
    percent?: number | null;
    now?: string;
  }): TaskProgressActivity {
    const summary = input.summary.trim();
    if (!summary)
      throw new E03RuntimeError(
        "progress_summary_missing",
        "progress activity summary is required",
      );
    const current = this.activities.get(input.task.identity.taskId) ?? [];
    const last = current.at(-1);
    if (last && last.leaseId !== input.task.identity.leaseId)
      throw new E03RuntimeError(
        "progress_stale_lease",
        "progress activity uses a lease different from prior activity",
      );
    if (last && last.revision > input.task.revision)
      throw new E03RuntimeError(
        "progress_stale_revision",
        "progress activity is behind prior activity revision",
      );
    const counter = (value: number | undefined, name: string): number => {
      const normalized = value ?? 0;
      if (!Number.isSafeInteger(normalized) || normalized < 0)
        throw new E03RuntimeError(
          "progress_counter_invalid",
          `${name} progress counter is invalid`,
        );
      return normalized;
    };
    const payload = {
      activityId: `task-activity-${digest({
        taskId: input.task.identity.taskId,
        leaseId: input.task.identity.leaseId,
        sequence: current.length + 1,
        kind: input.kind,
        detail: input.detail ?? null,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      leaseId: input.task.identity.leaseId,
      attempt: input.task.identity.attempt,
      revision: input.task.revision,
      sequence: current.length + 1,
      kind: input.kind,
      summary,
      detailDigest: digest(input.detail ?? null),
      tokensIn: counter(input.tokensIn, "tokensIn"),
      tokensOut: counter(input.tokensOut, "tokensOut"),
      toolCalls: counter(input.toolCalls, "toolCalls"),
      artifacts: counter(input.artifacts, "artifacts"),
      percent: input.percent ?? null,
      occurredAt: input.now ?? new Date().toISOString(),
      previousDigest: last?.digest ?? "",
    };
    const activity = { ...payload, digest: digest(payload) };
    assertProgressActivity(activity);
    current.push(activity);
    this.activities.set(input.task.identity.taskId, current);
    return structuredClone(activity);
  }

  project(
    task: E03TaskState,
    now = new Date().toISOString(),
  ): TaskProgressProjection {
    const activities = this.snapshot(task.identity.taskId);
    const last = activities.at(-1);
    const payload = {
      taskId: task.identity.taskId,
      status: task.status,
      revision: task.revision,
      attempt: task.identity.attempt,
      activities: activities.length,
      tokensIn: activities.reduce(
        (sum, activity) => sum + activity.tokensIn,
        0,
      ),
      tokensOut: activities.reduce(
        (sum, activity) => sum + activity.tokensOut,
        0,
      ),
      toolCalls: activities.reduce(
        (sum, activity) => sum + activity.toolCalls,
        0,
      ),
      artifacts: activities.reduce(
        (sum, activity) => sum + activity.artifacts,
        0,
      ),
      percent: last?.percent ?? null,
      lastSummary: last?.summary ?? "",
      lastActivityAt: last?.occurredAt ?? null,
      idleMs: last
        ? Math.max(0, Date.parse(now) - Date.parse(last.occurredAt))
        : Math.max(0, Date.parse(now) - Date.parse(task.createdAt)),
      terminal: isTerminal(task.status),
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(activities: readonly TaskProgressActivity[]): void {
    const next = new Map<string, TaskProgressActivity[]>();
    for (const raw of activities) {
      const activity = structuredClone(raw);
      assertProgressActivity(activity);
      const current = next.get(activity.taskId) ?? [];
      const last = current.at(-1);
      if (activity.sequence !== current.length + 1)
        throw new E03RuntimeError(
          "progress_sequence_gap",
          `task ${activity.taskId} progress sequence is discontinuous`,
        );
      if (activity.previousDigest !== (last?.digest ?? ""))
        throw new E03RuntimeError(
          "progress_digest_chain",
          `task ${activity.taskId} progress digest chain is broken`,
        );
      current.push(activity);
      next.set(activity.taskId, current);
    }
    this.activities = next;
  }

  snapshot(taskId?: string): TaskProgressActivity[] {
    const values = taskId
      ? (this.activities.get(taskId) ?? [])
      : [...this.activities.values()].flat();
    return values
      .slice()
      .sort(
        (left, right) =>
          left.occurredAt.localeCompare(right.occurredAt) ||
          left.taskId.localeCompare(right.taskId) ||
          left.sequence - right.sequence,
      )
      .map((activity) => structuredClone(activity));
  }
}

export type TaskSuspensionReason =
  | "waiting-input"
  | "waiting-tool"
  | "waiting-child"
  | "waiting-permission"
  | "waiting-resource"
  | "manual-pause"
  | "fault-recovery";

export interface TaskResumeCheckpoint {
  checkpointId: string;
  taskId: string;
  leaseId: string;
  attempt: number;
  taskRevision: number;
  reason: TaskSuspensionReason;
  contextSnapshotId: string;
  contextChecksum: string;
  pendingMessageIds: string[];
  pendingDeliveryIds: string[];
  pendingEffectIds: string[];
  pendingChildTaskIds: string[];
  permissionDigest: string;
  toolCatalogDigest: string;
  topologyRevision: number;
  state: "captured" | "validated" | "consumed" | "superseded" | "invalid";
  capturedAt: string;
  validatedAt: string | null;
  consumedAt: string | null;
  resumeTaskRevision: number | null;
  revision: number;
  digest: string;
}

export interface TaskResumeDecision {
  decisionId: string;
  checkpointId: string;
  taskId: string;
  decision: "resume" | "reject" | "retry";
  reason: string;
  expectedTaskRevision: number;
  expectedContextChecksum: string;
  decidedAt: string;
  digest: string;
}

function assertTaskResumeCheckpoint(checkpoint: TaskResumeCheckpoint): void {
  const { digest: checksum, ...payload } = checkpoint;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_resume_checkpoint_digest",
      `task resume checkpoint ${checkpoint.checkpointId} digest is invalid`,
    );
  if (
    !checkpoint.checkpointId ||
    !checkpoint.taskId ||
    !checkpoint.leaseId ||
    !checkpoint.contextSnapshotId ||
    !checkpoint.contextChecksum ||
    !checkpoint.permissionDigest ||
    !checkpoint.toolCatalogDigest
  )
    throw new E03RuntimeError(
      "task_resume_checkpoint_identity",
      "task resume checkpoint identity is incomplete",
    );
  for (const value of [
    checkpoint.attempt,
    checkpoint.taskRevision,
    checkpoint.topologyRevision,
  ])
    if (!Number.isSafeInteger(value) || value < 0)
      throw new E03RuntimeError(
        "task_resume_checkpoint_counter",
        "task resume checkpoint counter is invalid",
      );
  if (!Number.isSafeInteger(checkpoint.revision) || checkpoint.revision < 1)
    throw new E03RuntimeError(
      "task_resume_checkpoint_revision",
      "task resume checkpoint revision is invalid",
    );
  if (checkpoint.state === "validated" && checkpoint.validatedAt === null)
    throw new E03RuntimeError(
      "task_resume_checkpoint_state",
      "validated task resume checkpoint requires validatedAt",
    );
  if (
    checkpoint.state === "consumed" &&
    (checkpoint.consumedAt === null || checkpoint.resumeTaskRevision === null)
  )
    throw new E03RuntimeError(
      "task_resume_checkpoint_state",
      "consumed task resume checkpoint requires resume metadata",
    );
}

export class TaskResumeCheckpointRuntime {
  private checkpoints = new Map<string, TaskResumeCheckpoint>();
  private decisions = new Map<string, TaskResumeDecision>();
  private latestByTask = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  capture(input: {
    task: E03TaskState;
    reason: TaskSuspensionReason;
    pendingMessageIds?: readonly string[];
    pendingDeliveryIds?: readonly string[];
    pendingEffectIds?: readonly string[];
    pendingChildTaskIds?: readonly string[];
  }): TaskResumeCheckpoint {
    if (input.task.status !== "waiting" && input.task.status !== "running")
      throw new E03RuntimeError(
        "task_resume_checkpoint_phase",
        `cannot capture resume checkpoint for ${input.task.status} task`,
      );
    const priorId = this.latestByTask.get(input.task.identity.taskId);
    if (priorId) {
      const prior = this.requireCheckpoint(priorId);
      if (!["consumed", "superseded", "invalid"].includes(prior.state)) {
        const superseded = this.transition(prior, { state: "superseded" });
        this.checkpoints.set(superseded.checkpointId, superseded);
      }
    }
    const payload = {
      checkpointId: createId("task-resume-checkpoint"),
      taskId: input.task.identity.taskId,
      leaseId: input.task.identity.leaseId,
      attempt: input.task.identity.attempt,
      taskRevision: input.task.revision,
      reason: input.reason,
      contextSnapshotId: input.task.context.snapshotId,
      contextChecksum: input.task.context.checksum,
      pendingMessageIds: [...new Set(input.pendingMessageIds ?? [])],
      pendingDeliveryIds: [...new Set(input.pendingDeliveryIds ?? [])],
      pendingEffectIds: [...new Set(input.pendingEffectIds ?? [])],
      pendingChildTaskIds: [...new Set(input.pendingChildTaskIds ?? [])],
      permissionDigest: input.task.context.permissionDigest,
      toolCatalogDigest: input.task.context.toolCatalogDigest,
      topologyRevision: input.task.context.topologyRevision,
      state: "captured" as const,
      capturedAt: this.clock.now(),
      validatedAt: null,
      consumedAt: null,
      resumeTaskRevision: null,
      revision: 1,
    };
    const checkpoint = { ...payload, digest: digest(payload) };
    assertTaskResumeCheckpoint(checkpoint);
    this.checkpoints.set(checkpoint.checkpointId, checkpoint);
    this.latestByTask.set(checkpoint.taskId, checkpoint.checkpointId);
    return structuredClone(checkpoint);
  }

  validate(
    checkpointId: string,
    task: E03TaskState,
    expectedRevision: number,
  ): TaskResumeCheckpoint {
    const checkpoint = this.requireCheckpoint(checkpointId);
    this.assertRevision(checkpoint, expectedRevision);
    if (checkpoint.state !== "captured")
      throw new E03RuntimeError(
        "task_resume_checkpoint_validate_state",
        `task resume checkpoint ${checkpointId} is ${checkpoint.state}`,
      );
    const mismatches: string[] = [];
    if (task.identity.taskId !== checkpoint.taskId) mismatches.push("task-id");
    if (task.identity.leaseId !== checkpoint.leaseId)
      mismatches.push("lease-id");
    if (task.identity.attempt !== checkpoint.attempt)
      mismatches.push("attempt");
    if (task.revision !== checkpoint.taskRevision)
      mismatches.push("task-revision");
    if (task.context.snapshotId !== checkpoint.contextSnapshotId)
      mismatches.push("context-snapshot");
    if (task.context.checksum !== checkpoint.contextChecksum)
      mismatches.push("context-checksum");
    if (task.context.permissionDigest !== checkpoint.permissionDigest)
      mismatches.push("permission-digest");
    if (task.context.toolCatalogDigest !== checkpoint.toolCatalogDigest)
      mismatches.push("tool-catalog-digest");
    if (task.context.topologyRevision !== checkpoint.topologyRevision)
      mismatches.push("topology-revision");
    if (mismatches.length)
      return this.transition(checkpoint, { state: "invalid" });
    return this.transition(checkpoint, {
      state: "validated",
      validatedAt: this.clock.now(),
    });
  }

  decide(checkpointId: string, task: E03TaskState): TaskResumeDecision {
    const checkpoint = this.requireCheckpoint(checkpointId);
    let decision: TaskResumeDecision["decision"] = "resume";
    let reason = "checkpoint matches current task custody";
    if (checkpoint.state === "invalid" || checkpoint.state === "superseded") {
      decision = "reject";
      reason = `checkpoint is ${checkpoint.state}`;
    } else if (checkpoint.state !== "validated") {
      decision = "retry";
      reason = `checkpoint requires validation from ${checkpoint.state}`;
    } else if (isTerminal(task.status)) {
      decision = "reject";
      reason = `task is terminal (${task.status})`;
    } else if (task.revision !== checkpoint.taskRevision) {
      decision = "retry";
      reason = "task revision advanced after checkpoint validation";
    }
    const payload = {
      decisionId: createId("task-resume-decision"),
      checkpointId: checkpoint.checkpointId,
      taskId: checkpoint.taskId,
      decision,
      reason,
      expectedTaskRevision: checkpoint.taskRevision,
      expectedContextChecksum: checkpoint.contextChecksum,
      decidedAt: this.clock.now(),
    };
    const result = { ...payload, digest: digest(payload) };
    this.decisions.set(result.decisionId, result);
    return structuredClone(result);
  }

  consume(
    checkpointId: string,
    task: E03TaskState,
    expectedRevision: number,
  ): TaskResumeCheckpoint {
    const checkpoint = this.requireCheckpoint(checkpointId);
    this.assertRevision(checkpoint, expectedRevision);
    if (checkpoint.state !== "validated")
      throw new E03RuntimeError(
        "task_resume_checkpoint_consume_state",
        `task resume checkpoint ${checkpointId} is ${checkpoint.state}`,
      );
    if (
      task.identity.taskId !== checkpoint.taskId ||
      task.identity.leaseId !== checkpoint.leaseId ||
      task.revision < checkpoint.taskRevision
    )
      throw new E03RuntimeError(
        "task_resume_checkpoint_consume_custody",
        `task resume checkpoint ${checkpointId} custody changed`,
      );
    return this.transition(checkpoint, {
      state: "consumed",
      consumedAt: this.clock.now(),
      resumeTaskRevision: task.revision,
    });
  }

  latest(taskId: string): TaskResumeCheckpoint | null {
    const checkpointId = this.latestByTask.get(taskId);
    return checkpointId
      ? structuredClone(this.requireCheckpoint(checkpointId))
      : null;
  }

  snapshot(): {
    checkpoints: TaskResumeCheckpoint[];
    decisions: TaskResumeDecision[];
  } {
    return {
      checkpoints: [...this.checkpoints.values()].map((value) =>
        structuredClone(value),
      ),
      decisions: [...this.decisions.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    checkpoints: readonly TaskResumeCheckpoint[];
    decisions: readonly TaskResumeDecision[];
  }): void {
    const checkpoints = new Map<string, TaskResumeCheckpoint>();
    const decisions = new Map<string, TaskResumeDecision>();
    const latestByTask = new Map<string, string>();
    for (const checkpoint of input.checkpoints) {
      assertTaskResumeCheckpoint(checkpoint);
      if (checkpoints.has(checkpoint.checkpointId))
        throw new E03RuntimeError(
          "task_resume_checkpoint_restore_duplicate",
          `duplicate task resume checkpoint ${checkpoint.checkpointId}`,
        );
      checkpoints.set(checkpoint.checkpointId, structuredClone(checkpoint));
      const priorId = latestByTask.get(checkpoint.taskId);
      const prior = priorId ? checkpoints.get(priorId) : undefined;
      if (!prior || checkpoint.capturedAt > prior.capturedAt)
        latestByTask.set(checkpoint.taskId, checkpoint.checkpointId);
    }
    for (const decision of input.decisions) {
      const { digest: checksum, ...payload } = decision;
      if (digest(payload) !== checksum)
        throw new E03RuntimeError(
          "task_resume_decision_restore_digest",
          `task resume decision ${decision.decisionId} digest is invalid`,
        );
      const checkpoint = checkpoints.get(decision.checkpointId);
      if (!checkpoint || checkpoint.taskId !== decision.taskId)
        throw new E03RuntimeError(
          "task_resume_decision_restore_checkpoint",
          `task resume decision ${decision.decisionId} has invalid checkpoint`,
        );
      if (decisions.has(decision.decisionId))
        throw new E03RuntimeError(
          "task_resume_decision_restore_duplicate",
          `duplicate task resume decision ${decision.decisionId}`,
        );
      decisions.set(decision.decisionId, structuredClone(decision));
    }
    this.checkpoints = checkpoints;
    this.decisions = decisions;
    this.latestByTask = latestByTask;
  }

  private requireCheckpoint(checkpointId: string): TaskResumeCheckpoint {
    const checkpoint = this.checkpoints.get(checkpointId);
    if (!checkpoint)
      throw new E03RuntimeError(
        "task_resume_checkpoint_missing",
        `task resume checkpoint ${checkpointId} does not exist`,
      );
    assertTaskResumeCheckpoint(checkpoint);
    return checkpoint;
  }

  private assertRevision(
    checkpoint: TaskResumeCheckpoint,
    expected: number,
  ): void {
    if (checkpoint.revision !== expected)
      throw new E03RuntimeError(
        "task_resume_checkpoint_stale_revision",
        `task resume checkpoint ${checkpoint.checkpointId} revision is stale`,
      );
  }

  private transition(
    checkpoint: TaskResumeCheckpoint,
    patch: Partial<
      Omit<TaskResumeCheckpoint, "checkpointId" | "revision" | "digest">
    >,
  ): TaskResumeCheckpoint {
    const { digest: _, ...prior } = checkpoint;
    const payload = {
      ...prior,
      ...patch,
      checkpointId: checkpoint.checkpointId,
      revision: checkpoint.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTaskResumeCheckpoint(next);
    this.checkpoints.set(next.checkpointId, next);
    return structuredClone(next);
  }
}
