import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  type JsonValue,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  compareNumbers,
  deepClone,
  digestJson,
} from "../core/runtime-primitives.js";

export type ExecutionPlanStatus =
  | "created"
  | "reasoning"
  | "awaiting_permission"
  | "tool_ready"
  | "tool_running"
  | "observing"
  | "revising"
  | "suspended"
  | "completed"
  | "failed"
  | "cancelled";

export type ExecutionTransitionKind =
  | "plan.created"
  | "reason.started"
  | "reason.completed"
  | "tool.proposed"
  | "permission.allowed"
  | "permission.denied"
  | "tool.started"
  | "tool.completed"
  | "tool.failed"
  | "observation.recorded"
  | "revision.started"
  | "revision.completed"
  | "plan.suspended"
  | "plan.resumed"
  | "plan.completed"
  | "plan.failed"
  | "plan.cancelled";

export interface ExecutionBudget {
  maximumTurns: number;
  maximumToolCalls: number;
  maximumReasoningTokens: number;
  maximumOutputTokens: number;
  maximumWallMilliseconds: number;
  maximumConsecutiveToolFailures: number;
}

export interface ExecutionUsage {
  turns: number;
  toolCalls: number;
  reasoningTokens: number;
  outputTokens: number;
  consecutiveToolFailures: number;
}

export interface ProposedToolCall {
  toolCallId: string;
  toolName: string;
  input: JsonRecord;
  inputDigest: string;
  proposedAt: number;
  permissionDecisionId: string | null;
  idempotencyKey: string;
  effectId: string | null;
}

export interface ToolObservation {
  toolCallId: string;
  success: boolean;
  output: JsonValue;
  outputDigest: string;
  startedAt: number;
  finishedAt: number;
  effectId: string | null;
  errorCode: string | null;
  metadata: JsonRecord;
}

export interface ReasoningRecord {
  reasoningId: string;
  turn: number;
  startedAt: number;
  completedAt: number | null;
  inputDigest: string;
  conclusion: string | null;
  reasoningTokens: number;
  proposedToolCallId: string | null;
  terminalAnswer: string | null;
}

export interface RevisionRecord {
  revisionId: string;
  turn: number;
  observationDigest: string;
  startedAt: number;
  completedAt: number | null;
  assessment: string | null;
  nextAction: "reason" | "tool" | "complete" | "fail" | null;
}

export interface ExecutionTransition {
  transitionId: string;
  planId: string;
  sequence: number;
  revision: number;
  kind: ExecutionTransitionKind;
  previousStatus: ExecutionPlanStatus | null;
  nextStatus: ExecutionPlanStatus;
  occurredAt: number;
  correlationId: string;
  causationId: string | null;
  payload: JsonRecord;
  stateDigest: string;
}

export interface ExecutionPlan {
  planId: string;
  sessionId: string;
  runId: string;
  queryId: string;
  status: ExecutionPlanStatus;
  objective: string;
  contextDigest: string;
  budget: ExecutionBudget;
  usage: ExecutionUsage;
  createdAt: number;
  updatedAt: number;
  deadlineAt: number;
  revision: number;
  sequence: number;
  restartEpoch: number;
  activeReasoningId: string | null;
  activeToolCallId: string | null;
  activeRevisionId: string | null;
  suspensionReason: string | null;
  terminalReason: string | null;
  terminalAnswer: string | null;
  metadata: JsonRecord;
}

export interface ExecutionPlanSnapshot {
  version: "zyra.execution-plan/v1";
  plans: ExecutionPlan[];
  reasoning: ReasoningRecord[];
  proposedTools: ProposedToolCall[];
  observations: ToolObservation[];
  revisions: RevisionRecord[];
  transitions: ExecutionTransition[];
  completedEffects: Array<{ idempotencyKey: string; effectId: string }>;
  checksum: string;
}

export interface ExecutionPlanRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  maximumTransitionsPerPlan?: number;
}

export class QueryExecutionPlanRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly maximumTransitionsPerPlan: number;
  private readonly plans = new Map<string, ExecutionPlan>();
  private readonly reasoning = new Map<string, ReasoningRecord>();
  private readonly proposedTools = new Map<string, ProposedToolCall>();
  private readonly observations = new Map<string, ToolObservation>();
  private readonly revisions = new Map<string, RevisionRecord>();
  private readonly transitions = new Map<string, ExecutionTransition[]>();
  private readonly completedEffects = new Map<string, string>();

  constructor(options: ExecutionPlanRuntimeOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.maximumTransitionsPerPlan = options.maximumTransitionsPerPlan ?? 10_000;
    assertNonNegativeInteger(
      this.maximumTransitionsPerPlan,
      "maximumTransitionsPerPlan",
    );
  }

  create(input: {
    planId?: string;
    sessionId: string;
    runId: string;
    queryId: string;
    objective: string;
    contextDigest: string;
    budget: ExecutionBudget;
    metadata?: JsonRecord;
  }): ExecutionPlan {
    validateCreateInput(input);
    const planId = input.planId ?? this.ids.next("execution-plan");
    if (this.plans.has(planId)) {
      throw new RuntimeInvariantError("execution_plan_already_exists", {
        planId,
      });
    }
    const now = this.clock.now();
    const plan: ExecutionPlan = {
      planId,
      sessionId: input.sessionId,
      runId: input.runId,
      queryId: input.queryId,
      status: "created",
      objective: input.objective,
      contextDigest: input.contextDigest,
      budget: deepClone(input.budget),
      usage: emptyUsage(),
      createdAt: now,
      updatedAt: now,
      deadlineAt: now + input.budget.maximumWallMilliseconds,
      revision: 1,
      sequence: 1,
      restartEpoch: 0,
      activeReasoningId: null,
      activeToolCallId: null,
      activeRevisionId: null,
      suspensionReason: null,
      terminalReason: null,
      terminalAnswer: null,
      metadata: deepClone(input.metadata ?? {}),
    };
    this.plans.set(planId, plan);
    this.transitions.set(planId, []);
    this.appendTransition(plan, "plan.created", "created", {}, null);
    return this.get(planId);
  }

  beginReason(
    planId: string,
    inputDigest: string,
    correlationId: string,
    causationId: string | null = null,
  ): ReasoningRecord {
    const plan = this.requirePlan(planId);
    this.assertStatus(plan, ["created", "revising"]);
    this.assertWithinBudget(plan, "reason");
    assertNonEmpty(inputDigest, "inputDigest");
    const reasoningId = this.ids.next("reasoning");
    const record: ReasoningRecord = {
      reasoningId,
      turn: plan.usage.turns + 1,
      startedAt: this.clock.now(),
      completedAt: null,
      inputDigest,
      conclusion: null,
      reasoningTokens: 0,
      proposedToolCallId: null,
      terminalAnswer: null,
    };
    this.reasoning.set(reasoningId, record);
    plan.activeReasoningId = reasoningId;
    plan.usage.turns += 1;
    this.transition(plan, "reason.started", "reasoning", correlationId, {
      reasoningId,
      inputDigest,
      turn: record.turn,
    }, causationId);
    return deepClone(record);
  }

  completeReason(
    planId: string,
    input: {
      reasoningId: string;
      conclusion: string;
      reasoningTokens: number;
      terminalAnswer?: string;
      tool?: {
        toolCallId?: string;
        toolName: string;
        input: JsonRecord;
        idempotencyKey: string;
      };
      correlationId: string;
      causationId?: string | null;
    },
  ): ReasoningRecord {
    const plan = this.requirePlan(planId);
    this.assertStatus(plan, ["reasoning"]);
    if (plan.activeReasoningId !== input.reasoningId) {
      throw new RuntimeInvariantError("reasoning_not_active", {
        planId,
        expected: plan.activeReasoningId,
        actual: input.reasoningId,
      });
    }
    const record = this.requireReasoning(input.reasoningId);
    if (record.completedAt !== null) {
      throw new RuntimeInvariantError("reasoning_already_completed", {
        reasoningId: input.reasoningId,
      });
    }
    assertNonEmpty(input.conclusion, "conclusion");
    assertNonNegativeInteger(input.reasoningTokens, "reasoningTokens");
    if (
      plan.usage.reasoningTokens + input.reasoningTokens >
      plan.budget.maximumReasoningTokens
    ) {
      throw new RuntimeInvariantError("reasoning_token_budget_exceeded", {
        planId,
        consumed: plan.usage.reasoningTokens,
        requested: input.reasoningTokens,
        maximum: plan.budget.maximumReasoningTokens,
      });
    }
    if (input.terminalAnswer !== undefined && input.tool !== undefined) {
      throw new RuntimeInvariantError("reasoning_has_tool_and_terminal_answer", {
        reasoningId: input.reasoningId,
      });
    }
    record.completedAt = this.clock.now();
    record.conclusion = input.conclusion;
    record.reasoningTokens = input.reasoningTokens;
    record.terminalAnswer = input.terminalAnswer ?? null;
    plan.usage.reasoningTokens += input.reasoningTokens;
    plan.activeReasoningId = null;
    let nextStatus: ExecutionPlanStatus = "revising";
    const payload: JsonRecord = {
      reasoningId: record.reasoningId,
      reasoningTokens: input.reasoningTokens,
      conclusionDigest: digestJson(input.conclusion),
    };
    if (input.tool !== undefined) {
      const tool = this.proposeToolInternal(plan, input.tool);
      record.proposedToolCallId = tool.toolCallId;
      nextStatus = "awaiting_permission";
      payload.toolCallId = tool.toolCallId;
      payload.toolName = tool.toolName;
    } else if (input.terminalAnswer !== undefined) {
      nextStatus = "completed";
      plan.terminalAnswer = input.terminalAnswer;
      plan.terminalReason = "model_terminal_answer";
      payload.answerDigest = digestJson(input.terminalAnswer);
    }
    this.transition(
      plan,
      "reason.completed",
      nextStatus,
      input.correlationId,
      payload,
      input.causationId ?? null,
    );
    return deepClone(record);
  }

  decidePermission(input: {
    planId: string;
    toolCallId: string;
    permissionDecisionId: string;
    allowed: boolean;
    reason: string;
    correlationId: string;
    causationId?: string | null;
  }): ProposedToolCall {
    const plan = this.requirePlan(input.planId);
    this.assertStatus(plan, ["awaiting_permission"]);
    if (plan.activeToolCallId !== input.toolCallId) {
      throw new RuntimeInvariantError("tool_call_not_active", {
        expected: plan.activeToolCallId,
        actual: input.toolCallId,
      });
    }
    const tool = this.requireProposedTool(input.toolCallId);
    assertNonEmpty(input.permissionDecisionId, "permissionDecisionId");
    assertNonEmpty(input.reason, "reason");
    tool.permissionDecisionId = input.permissionDecisionId;
    if (input.allowed) {
      this.transition(
        plan,
        "permission.allowed",
        "tool_ready",
        input.correlationId,
        {
          toolCallId: input.toolCallId,
          permissionDecisionId: input.permissionDecisionId,
          reason: input.reason,
        },
        input.causationId ?? null,
      );
    } else {
      this.transition(
        plan,
        "permission.denied",
        "revising",
        input.correlationId,
        {
          toolCallId: input.toolCallId,
          permissionDecisionId: input.permissionDecisionId,
          reason: input.reason,
        },
        input.causationId ?? null,
      );
      plan.activeToolCallId = null;
    }
    return deepClone(tool);
  }

  beginTool(input: {
    planId: string;
    toolCallId: string;
    effectId: string;
    correlationId: string;
    causationId?: string | null;
  }): ProposedToolCall {
    const plan = this.requirePlan(input.planId);
    this.assertStatus(plan, ["tool_ready"]);
    this.assertWithinBudget(plan, "tool");
    const tool = this.requireProposedTool(input.toolCallId);
    if (plan.activeToolCallId !== tool.toolCallId) {
      throw new RuntimeInvariantError("tool_call_not_active", {
        expected: plan.activeToolCallId,
        actual: tool.toolCallId,
      });
    }
    assertNonEmpty(input.effectId, "effectId");
    const completedEffect = this.completedEffects.get(tool.idempotencyKey);
    if (completedEffect !== undefined) {
      throw new RuntimeInvariantError("tool_effect_already_completed", {
        toolCallId: tool.toolCallId,
        idempotencyKey: tool.idempotencyKey,
        effectId: completedEffect,
      });
    }
    tool.effectId = input.effectId;
    plan.usage.toolCalls += 1;
    this.transition(
      plan,
      "tool.started",
      "tool_running",
      input.correlationId,
      {
        toolCallId: tool.toolCallId,
        toolName: tool.toolName,
        effectId: input.effectId,
        idempotencyKey: tool.idempotencyKey,
      },
      input.causationId ?? null,
    );
    return deepClone(tool);
  }

  completeTool(input: {
    planId: string;
    toolCallId: string;
    output: JsonValue;
    startedAt: number;
    metadata?: JsonRecord;
    correlationId: string;
    causationId?: string | null;
  }): ToolObservation {
    return this.finishTool(input, true, null);
  }

  failTool(input: {
    planId: string;
    toolCallId: string;
    output: JsonValue;
    startedAt: number;
    errorCode: string;
    metadata?: JsonRecord;
    correlationId: string;
    causationId?: string | null;
  }): ToolObservation {
    assertNonEmpty(input.errorCode, "errorCode");
    return this.finishTool(input, false, input.errorCode);
  }

  recordObservation(input: {
    planId: string;
    toolCallId: string;
    correlationId: string;
    causationId?: string | null;
  }): ToolObservation {
    const plan = this.requirePlan(input.planId);
    this.assertStatus(plan, ["observing"]);
    const observation = this.requireObservation(input.toolCallId);
    this.transition(
      plan,
      "observation.recorded",
      "revising",
      input.correlationId,
      {
        toolCallId: observation.toolCallId,
        success: observation.success,
        outputDigest: observation.outputDigest,
        errorCode: observation.errorCode,
      },
      input.causationId ?? null,
    );
    plan.activeToolCallId = null;
    return deepClone(observation);
  }

  beginRevision(input: {
    planId: string;
    observationDigest: string;
    correlationId: string;
    causationId?: string | null;
  }): RevisionRecord {
    const plan = this.requirePlan(input.planId);
    this.assertStatus(plan, ["revising"]);
    const revisionId = this.ids.next("reason-revision");
    const record: RevisionRecord = {
      revisionId,
      turn: plan.usage.turns,
      observationDigest: input.observationDigest,
      startedAt: this.clock.now(),
      completedAt: null,
      assessment: null,
      nextAction: null,
    };
    this.revisions.set(revisionId, record);
    plan.activeRevisionId = revisionId;
    this.transition(
      plan,
      "revision.started",
      "revising",
      input.correlationId,
      { revisionId, observationDigest: input.observationDigest },
      input.causationId ?? null,
    );
    return deepClone(record);
  }

  completeRevision(input: {
    planId: string;
    revisionId: string;
    assessment: string;
    nextAction: "reason" | "complete" | "fail";
    terminalAnswer?: string;
    correlationId: string;
    causationId?: string | null;
  }): RevisionRecord {
    const plan = this.requirePlan(input.planId);
    if (plan.activeRevisionId !== input.revisionId) {
      throw new RuntimeInvariantError("revision_not_active", {
        expected: plan.activeRevisionId,
        actual: input.revisionId,
      });
    }
    const revision = this.requireRevision(input.revisionId);
    if (revision.completedAt !== null) {
      throw new RuntimeInvariantError("revision_already_completed", {
        revisionId: revision.revisionId,
      });
    }
    assertNonEmpty(input.assessment, "assessment");
    revision.completedAt = this.clock.now();
    revision.assessment = input.assessment;
    revision.nextAction = input.nextAction;
    plan.activeRevisionId = null;
    let status: ExecutionPlanStatus;
    if (input.nextAction === "reason") {
      status = "created";
    } else if (input.nextAction === "complete") {
      if (input.terminalAnswer === undefined) {
        throw new RuntimeInvariantError("terminal_answer_required", {
          planId: plan.planId,
        });
      }
      status = "completed";
      plan.terminalAnswer = input.terminalAnswer;
      plan.terminalReason = "revision_complete";
    } else {
      status = "failed";
      plan.terminalReason = input.assessment;
    }
    this.transition(
      plan,
      "revision.completed",
      status,
      input.correlationId,
      {
        revisionId: input.revisionId,
        nextAction: input.nextAction,
        assessmentDigest: digestJson(input.assessment),
      },
      input.causationId ?? null,
    );
    return deepClone(revision);
  }

  suspend(
    planId: string,
    reason: string,
    correlationId: string,
  ): ExecutionPlan {
    const plan = this.requireNonTerminalPlan(planId);
    assertNonEmpty(reason, "reason");
    plan.suspensionReason = reason;
    this.transition(plan, "plan.suspended", "suspended", correlationId, {
      reason,
      suspendedFrom: plan.status,
    }, null);
    return this.get(planId);
  }

  resume(
    planId: string,
    targetStatus: "created" | "awaiting_permission" | "tool_ready" | "revising",
    correlationId: string,
  ): ExecutionPlan {
    const plan = this.requirePlan(planId);
    this.assertStatus(plan, ["suspended"]);
    plan.restartEpoch += 1;
    plan.suspensionReason = null;
    this.transition(plan, "plan.resumed", targetStatus, correlationId, {
      restartEpoch: plan.restartEpoch,
    }, null);
    return this.get(planId);
  }

  cancel(planId: string, reason: string, correlationId: string): ExecutionPlan {
    const plan = this.requireNonTerminalPlan(planId);
    assertNonEmpty(reason, "reason");
    plan.terminalReason = reason;
    this.transition(plan, "plan.cancelled", "cancelled", correlationId, {
      reason,
    }, null);
    return this.get(planId);
  }

  get(planId: string): ExecutionPlan {
    return deepClone(this.requirePlan(planId));
  }

  listTransitions(planId: string, afterSequence = 0): ExecutionTransition[] {
    assertNonNegativeInteger(afterSequence, "afterSequence");
    this.requirePlan(planId);
    return (this.transitions.get(planId) ?? [])
      .filter((transition) => transition.sequence > afterSequence)
      .map((transition) => deepClone(transition));
  }

  snapshot(): ExecutionPlanSnapshot {
    const body = {
      version: "zyra.execution-plan/v1" as const,
      plans: sortedValues(this.plans, (value) => value.planId),
      reasoning: sortedValues(this.reasoning, (value) => value.reasoningId),
      proposedTools: sortedValues(this.proposedTools, (value) => value.toolCallId),
      observations: sortedValues(this.observations, (value) => value.toolCallId),
      revisions: sortedValues(this.revisions, (value) => value.revisionId),
      transitions: [...this.transitions.values()]
        .flat()
        .sort((left, right) =>
          left.planId.localeCompare(right.planId) ||
          compareNumbers(left.sequence, right.sequence),
        )
        .map((value) => deepClone(value)),
      completedEffects: [...this.completedEffects.entries()]
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([idempotencyKey, effectId]) => ({ idempotencyKey, effectId })),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: ExecutionPlanSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.execution-plan/v1") {
      throw new RuntimeInvariantError("unsupported_execution_plan_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("execution_plan_snapshot_checksum_mismatch");
    }
    this.plans.clear();
    this.reasoning.clear();
    this.proposedTools.clear();
    this.observations.clear();
    this.revisions.clear();
    this.transitions.clear();
    this.completedEffects.clear();
    for (const plan of snapshot.plans) {
      this.plans.set(plan.planId, deepClone(plan));
      this.transitions.set(plan.planId, []);
    }
    for (const record of snapshot.reasoning) {
      this.reasoning.set(record.reasoningId, deepClone(record));
    }
    for (const record of snapshot.proposedTools) {
      this.proposedTools.set(record.toolCallId, deepClone(record));
    }
    for (const record of snapshot.observations) {
      this.observations.set(record.toolCallId, deepClone(record));
    }
    for (const record of snapshot.revisions) {
      this.revisions.set(record.revisionId, deepClone(record));
    }
    for (const transition of snapshot.transitions) {
      const values = this.transitions.get(transition.planId);
      if (values === undefined) {
        throw new RuntimeInvariantError("transition_without_execution_plan", {
          transitionId: transition.transitionId,
          planId: transition.planId,
        });
      }
      values.push(deepClone(transition));
    }
    for (const effect of snapshot.completedEffects) {
      this.completedEffects.set(effect.idempotencyKey, effect.effectId);
    }
    for (const plan of this.plans.values()) {
      const transitions = this.transitions.get(plan.planId) ?? [];
      const last = transitions.at(-1);
      if (last !== undefined && last.stateDigest !== stateDigest(plan)) {
        throw new RuntimeInvariantError("execution_plan_state_digest_mismatch", {
          planId: plan.planId,
        });
      }
      plan.restartEpoch += 1;
    }
  }

  private finishTool(
    input: {
      planId: string;
      toolCallId: string;
      output: JsonValue;
      startedAt: number;
      metadata?: JsonRecord;
      correlationId: string;
      causationId?: string | null;
    },
    success: boolean,
    errorCode: string | null,
  ): ToolObservation {
    const plan = this.requirePlan(input.planId);
    const existing = this.observations.get(input.toolCallId);
    if (existing !== undefined) {
      if (existing.outputDigest !== digestJson(input.output)) {
        throw new RuntimeInvariantError("tool_observation_conflict", {
          toolCallId: input.toolCallId,
        });
      }
      return deepClone(existing);
    }
    this.assertStatus(plan, ["tool_running"]);
    const tool = this.requireProposedTool(input.toolCallId);
    if (tool.effectId === null) {
      throw new RuntimeInvariantError("tool_effect_not_started", {
        toolCallId: tool.toolCallId,
      });
    }
    const observation: ToolObservation = {
      toolCallId: tool.toolCallId,
      success,
      output: deepClone(input.output),
      outputDigest: digestJson(input.output),
      startedAt: input.startedAt,
      finishedAt: this.clock.now(),
      effectId: tool.effectId,
      errorCode,
      metadata: deepClone(input.metadata ?? {}),
    };
    this.observations.set(tool.toolCallId, observation);
    if (success) {
      this.completedEffects.set(tool.idempotencyKey, tool.effectId);
      plan.usage.consecutiveToolFailures = 0;
    } else {
      plan.usage.consecutiveToolFailures += 1;
    }
    this.transition(
      plan,
      success ? "tool.completed" : "tool.failed",
      "observing",
      input.correlationId,
      {
        toolCallId: tool.toolCallId,
        effectId: tool.effectId,
        outputDigest: observation.outputDigest,
        errorCode,
      },
      input.causationId ?? null,
    );
    return deepClone(observation);
  }

  private proposeToolInternal(
    plan: ExecutionPlan,
    input: {
      toolCallId?: string;
      toolName: string;
      input: JsonRecord;
      idempotencyKey: string;
    },
  ): ProposedToolCall {
    assertNonEmpty(input.toolName, "toolName");
    assertNonEmpty(input.idempotencyKey, "idempotencyKey");
    const toolCallId = input.toolCallId ?? this.ids.next("tool-call");
    if (this.proposedTools.has(toolCallId)) {
      throw new RuntimeInvariantError("tool_call_already_exists", {
        toolCallId,
      });
    }
    const record: ProposedToolCall = {
      toolCallId,
      toolName: input.toolName,
      input: deepClone(input.input),
      inputDigest: digestJson(input.input),
      proposedAt: this.clock.now(),
      permissionDecisionId: null,
      idempotencyKey: input.idempotencyKey,
      effectId: null,
    };
    this.proposedTools.set(toolCallId, record);
    plan.activeToolCallId = toolCallId;
    return record;
  }

  private transition(
    plan: ExecutionPlan,
    kind: ExecutionTransitionKind,
    nextStatus: ExecutionPlanStatus,
    correlationId: string,
    payload: JsonRecord,
    causationId: string | null,
  ): void {
    const previousStatus = plan.status;
    plan.status = nextStatus;
    plan.updatedAt = this.clock.now();
    plan.revision += 1;
    plan.sequence += 1;
    this.appendTransition(
      plan,
      kind,
      nextStatus,
      payload,
      causationId,
      previousStatus,
      correlationId,
    );
  }

  private appendTransition(
    plan: ExecutionPlan,
    kind: ExecutionTransitionKind,
    nextStatus: ExecutionPlanStatus,
    payload: JsonRecord,
    causationId: string | null,
    previousStatus: ExecutionPlanStatus | null = null,
    correlationId = "bootstrap",
  ): void {
    const values = this.transitions.get(plan.planId);
    if (values === undefined) {
      throw new RuntimeInvariantError("execution_transition_store_missing", {
        planId: plan.planId,
      });
    }
    const transition: ExecutionTransition = {
      transitionId: this.ids.next("execution-transition"),
      planId: plan.planId,
      sequence: plan.sequence,
      revision: plan.revision,
      kind,
      previousStatus,
      nextStatus,
      occurredAt: this.clock.now(),
      correlationId,
      causationId,
      payload: deepClone(payload),
      stateDigest: stateDigest(plan),
    };
    values.push(transition);
    if (values.length > this.maximumTransitionsPerPlan) {
      throw new RuntimeInvariantError("execution_transition_limit_exceeded", {
        planId: plan.planId,
        maximum: this.maximumTransitionsPerPlan,
      });
    }
  }

  private assertWithinBudget(plan: ExecutionPlan, action: "reason" | "tool"): void {
    if (this.clock.now() >= plan.deadlineAt) {
      throw new RuntimeInvariantError("execution_wall_budget_exceeded", {
        planId: plan.planId,
        deadlineAt: plan.deadlineAt,
      });
    }
    if (action === "reason" && plan.usage.turns >= plan.budget.maximumTurns) {
      throw new RuntimeInvariantError("execution_turn_budget_exceeded", {
        planId: plan.planId,
      });
    }
    if (action === "tool" && plan.usage.toolCalls >= plan.budget.maximumToolCalls) {
      throw new RuntimeInvariantError("execution_tool_budget_exceeded", {
        planId: plan.planId,
      });
    }
    if (
      plan.usage.consecutiveToolFailures >=
      plan.budget.maximumConsecutiveToolFailures
    ) {
      throw new RuntimeInvariantError("execution_tool_failure_budget_exceeded", {
        planId: plan.planId,
      });
    }
  }

  private assertStatus(
    plan: ExecutionPlan,
    allowed: readonly ExecutionPlanStatus[],
  ): void {
    if (!allowed.includes(plan.status)) {
      throw new RuntimeInvariantError("execution_plan_status_conflict", {
        planId: plan.planId,
        actual: plan.status,
        allowed: [...allowed],
      });
    }
  }

  private requireNonTerminalPlan(planId: string): ExecutionPlan {
    const plan = this.requirePlan(planId);
    if (["completed", "failed", "cancelled"].includes(plan.status)) {
      throw new RuntimeInvariantError("execution_plan_terminal", {
        planId,
        status: plan.status,
      });
    }
    return plan;
  }

  private requirePlan(planId: string): ExecutionPlan {
    const plan = this.plans.get(planId);
    if (plan === undefined) {
      throw new RuntimeInvariantError("unknown_execution_plan", { planId });
    }
    return plan;
  }

  private requireReasoning(reasoningId: string): ReasoningRecord {
    const record = this.reasoning.get(reasoningId);
    if (record === undefined) {
      throw new RuntimeInvariantError("unknown_reasoning_record", { reasoningId });
    }
    return record;
  }

  private requireProposedTool(toolCallId: string): ProposedToolCall {
    const record = this.proposedTools.get(toolCallId);
    if (record === undefined) {
      throw new RuntimeInvariantError("unknown_proposed_tool", { toolCallId });
    }
    return record;
  }

  private requireObservation(toolCallId: string): ToolObservation {
    const record = this.observations.get(toolCallId);
    if (record === undefined) {
      throw new RuntimeInvariantError("unknown_tool_observation", { toolCallId });
    }
    return record;
  }

  private requireRevision(revisionId: string): RevisionRecord {
    const record = this.revisions.get(revisionId);
    if (record === undefined) {
      throw new RuntimeInvariantError("unknown_revision_record", { revisionId });
    }
    return record;
  }
}

function validateCreateInput(input: {
  sessionId: string;
  runId: string;
  queryId: string;
  objective: string;
  contextDigest: string;
  budget: ExecutionBudget;
}): void {
  assertNonEmpty(input.sessionId, "sessionId");
  assertNonEmpty(input.runId, "runId");
  assertNonEmpty(input.queryId, "queryId");
  assertNonEmpty(input.objective, "objective");
  assertNonEmpty(input.contextDigest, "contextDigest");
  for (const [name, value] of Object.entries(input.budget)) {
    assertNonNegativeInteger(value, `budget.${name}`);
  }
}

function emptyUsage(): ExecutionUsage {
  return {
    turns: 0,
    toolCalls: 0,
    reasoningTokens: 0,
    outputTokens: 0,
    consecutiveToolFailures: 0,
  };
}

function stateDigest(plan: ExecutionPlan): string {
  return digestJson({
    planId: plan.planId,
    status: plan.status,
    usage: plan.usage,
    revision: plan.revision,
    sequence: plan.sequence,
    restartEpoch: plan.restartEpoch,
    activeReasoningId: plan.activeReasoningId,
    activeToolCallId: plan.activeToolCallId,
    activeRevisionId: plan.activeRevisionId,
    terminalReason: plan.terminalReason,
    terminalAnswer: plan.terminalAnswer,
  });
}

function sortedValues<T>(
  map: ReadonlyMap<string, T>,
  key: (value: T) => string,
): T[] {
  return [...map.values()]
    .sort((left, right) => key(left).localeCompare(key(right)))
    .map((value) => deepClone(value));
}
