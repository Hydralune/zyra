import {
  createId,
  digest,
  E03RuntimeError,
  isTerminal,
  sealTask,
  type E03Clock,
  type E03TaskState,
  SystemE03Clock,
} from "../e03/contracts.ts";

export interface FanoutTarget {
  key: string;
  agent: string;
  prompt: string;
  metadata?: import("../contracts.ts").JsonObject;
}

export interface FanoutPlan {
  planId: string;
  parentTaskId: string;
  targets: FanoutTarget[];
  maximumConcurrency: number;
  failureMode: "collect" | "fail-fast";
  idempotencyKey: string;
  digest: string;
}

export interface FanoutResult {
  planId: string;
  completed: E03TaskState[];
  failed: E03TaskState[];
  cancelled: E03TaskState[];
  pending: E03TaskState[];
  ok: boolean;
  summary: string;
}

export class TeamFanout {
  plan(
    parent: E03TaskState,
    targets: readonly FanoutTarget[],
    input: {
      maximumConcurrency?: number;
      failureMode?: "collect" | "fail-fast";
      idempotencyKey: string;
    },
  ): FanoutPlan {
    if (!parent.scope.allowFanout)
      throw new E03RuntimeError(
        "fanout_not_permitted",
        "parent scope denies fanout",
      );
    if (!targets.length)
      throw new E03RuntimeError(
        "empty_fanout",
        "fanout requires at least one target",
      );
    if (targets.length > parent.scope.maxChildren)
      throw new E03RuntimeError(
        "fanout_child_limit",
        `fanout ${targets.length} exceeds ${parent.scope.maxChildren}`,
      );
    const keys = new Set<string>();
    const normalized = targets.map((target, index) => {
      const key = target.key.trim();
      const agent = target.agent.trim();
      const prompt = target.prompt.trim();
      if (!key || !agent || !prompt)
        throw new E03RuntimeError(
          "invalid_fanout_target",
          `fanout target ${index} requires key, agent and prompt`,
        );
      if (keys.has(key))
        throw new E03RuntimeError(
          "duplicate_fanout_key",
          `duplicate fanout key ${key}`,
        );
      keys.add(key);
      return {
        key,
        agent,
        prompt,
        metadata: structuredClone(target.metadata ?? {}),
      };
    });
    const maximumConcurrency = Math.min(
      input.maximumConcurrency ?? parent.definition.budget.maxConcurrency,
      parent.definition.budget.maxConcurrency,
      normalized.length,
    );
    if (!Number.isSafeInteger(maximumConcurrency) || maximumConcurrency < 1)
      throw new E03RuntimeError(
        "invalid_fanout_concurrency",
        "fanout concurrency must be positive",
      );
    const payload = {
      planId: `fanout-${digest({ parent: parent.identity.taskId, idempotencyKey: input.idempotencyKey }).slice(0, 24)}`,
      parentTaskId: parent.identity.taskId,
      targets: normalized,
      maximumConcurrency,
      failureMode: input.failureMode ?? "collect",
      idempotencyKey: input.idempotencyKey,
    };
    return { ...payload, digest: digest(payload) };
  }

  async dispatch(
    plan: FanoutPlan,
    create: (target: FanoutTarget, index: number) => Promise<E03TaskState>,
    cancel?: (task: E03TaskState, reason: string) => Promise<E03TaskState>,
  ): Promise<FanoutResult> {
    this.validatePlan(plan);
    const completed: E03TaskState[] = [];
    const failed: E03TaskState[] = [];
    const cancelled: E03TaskState[] = [];
    const running = new Map<
      number,
      Promise<{ index: number; task: E03TaskState }>
    >();
    let next = 0;
    let failFast = false;
    const schedule = (): void => {
      while (
        !failFast &&
        next < plan.targets.length &&
        running.size < plan.maximumConcurrency
      ) {
        const index = next++;
        const target = plan.targets[index]!;
        running.set(
          index,
          create(target, index)
            .then((task) => ({ index, task }))
            .catch((error) => ({
              index,
              task: failurePlaceholder(plan, target, error),
            })),
        );
      }
    };
    schedule();
    while (running.size) {
      const settled = await Promise.race(running.values());
      running.delete(settled.index);
      if (settled.task.status === "completed") completed.push(settled.task);
      else if (
        settled.task.status === "cancelled" ||
        settled.task.status === "killed"
      )
        cancelled.push(settled.task);
      else failed.push(settled.task);
      if (
        plan.failureMode === "fail-fast" &&
        settled.task.status !== "completed"
      ) {
        failFast = true;
        if (cancel) {
          const active = await Promise.all([...running.values()]);
          running.clear();
          for (const item of active) {
            if (isTerminal(item.task.status)) {
              if (item.task.status === "completed") completed.push(item.task);
              else failed.push(item.task);
            } else
              cancelled.push(
                await cancel(
                  item.task,
                  `fanout_fail_fast:${settled.task.identity.taskId}`,
                ),
              );
          }
        }
      }
      schedule();
    }
    const pendingTargets = plan.targets.slice(next);
    const pending = pendingTargets.map((target) =>
      failurePlaceholder(
        plan,
        target,
        new Error("fanout_not_dispatched"),
        "cancelled",
      ),
    );
    cancelled.push(...pending);
    return this.collect(plan, [...completed, ...failed, ...cancelled]);
  }

  collect(plan: FanoutPlan, tasks: readonly E03TaskState[]): FanoutResult {
    this.validatePlan(plan);
    const targetKeys = new Set(plan.targets.map((target) => target.key));
    const uniqueTasks = new Map<string, E03TaskState>();
    for (const task of tasks) {
      const key = String(task.definition.metadata.fanout_key ?? "");
      if (key && !targetKeys.has(key))
        throw new E03RuntimeError(
          "fanout_result_mismatch",
          `task ${task.identity.taskId} has unknown fanout key ${key}`,
        );
      const existing = uniqueTasks.get(task.identity.taskId);
      if (existing && existing.checksum !== task.checksum)
        throw new E03RuntimeError(
          "fanout_result_conflict",
          `task ${task.identity.taskId} has conflicting results`,
        );
      uniqueTasks.set(task.identity.taskId, task);
    }
    const values = [...uniqueTasks.values()].sort(
      (left, right) =>
        String(left.definition.metadata.fanout_key ?? "").localeCompare(
          String(right.definition.metadata.fanout_key ?? ""),
        ) || left.identity.taskId.localeCompare(right.identity.taskId),
    );
    const completed = values.filter((task) => task.status === "completed");
    const failed = values.filter((task) => task.status === "failed");
    const cancelled = values.filter(
      (task) => task.status === "cancelled" || task.status === "killed",
    );
    const pending = values.filter((task) => !isTerminal(task.status));
    const ok =
      failed.length === 0 &&
      cancelled.length === 0 &&
      pending.length === 0 &&
      completed.length === plan.targets.length;
    return {
      planId: plan.planId,
      completed: structuredClone(completed),
      failed: structuredClone(failed),
      cancelled: structuredClone(cancelled),
      pending: structuredClone(pending),
      ok,
      summary: `${completed.length} completed, ${failed.length} failed, ${cancelled.length} cancelled, ${pending.length} pending`,
    };
  }

  failFast(
    plan: FanoutPlan,
    tasks: readonly E03TaskState[],
  ): { stop: boolean; reason: string; failingTaskIds: string[] } {
    this.validatePlan(plan);
    const failing = tasks.filter(
      (task) =>
        task.status === "failed" ||
        task.status === "cancelled" ||
        task.status === "killed",
    );
    return {
      stop: plan.failureMode === "fail-fast" && failing.length > 0,
      reason: failing.length
        ? `fanout member ended: ${failing.map((task) => task.status).join(",")}`
        : "",
      failingTaskIds: failing.map((task) => task.identity.taskId),
    };
  }

  private validatePlan(plan: FanoutPlan): void {
    const { digest: planDigest, ...payload } = plan;
    if (digest(payload) !== planDigest)
      throw new E03RuntimeError(
        "fanout_plan_checksum",
        `fanout plan ${plan.planId} checksum mismatch`,
      );
    if (
      plan.maximumConcurrency < 1 ||
      plan.maximumConcurrency > plan.targets.length
    )
      throw new E03RuntimeError(
        "invalid_fanout_plan",
        "fanout concurrency is invalid",
      );
  }
}

function failurePlaceholder(
  plan: FanoutPlan,
  target: FanoutTarget,
  error: unknown,
  status: "failed" | "cancelled" = "failed",
): E03TaskState {
  const now = new Date().toISOString();
  const seed = digest({ planId: plan.planId, key: target.key });
  const definition = {
    name: target.agent,
    description: "Fanout failure placeholder",
    version: "1",
    source: "user" as const,
    priority: 300,
    model: "inherit",
    effort: "inherit",
    systemPrompt: "",
    tools: [],
    deniedTools: [],
    skills: [],
    mcpServers: [],
    permissionMode: "inherit",
    isolation: "workspace" as const,
    background: true,
    memoryScope: "task",
    budget: {
      maxTurns: 1,
      maxToolCalls: 1,
      maxInputTokens: 1,
      maxOutputTokens: 1,
      maxResultChars: 1,
      maxWallTimeMs: 1,
      maxChildren: 0,
      maxDepth: 0,
      maxConcurrency: 1,
      consumedTurns: 0,
      consumedToolCalls: 0,
      consumedInputTokens: 0,
      consumedOutputTokens: 0,
      consumedResultChars: 0,
      startedAt: now,
      deadlineAt: now,
    },
    metadata: { fanout_key: target.key },
    digest: seed,
  };
  const scopePayload = {
    tools: [],
    deniedTools: [],
    skills: [],
    mcpServers: [],
    permissionMode: "inherit",
    permissionCeilingDigest: "",
    workspaceRoots: [],
    isolationModes: ["workspace" as const],
    allowBackground: false,
    allowTeamMessaging: false,
    allowFanout: false,
    allowKill: false,
    maxDepth: 0,
    maxChildren: 0,
  };
  const contextPayload = {
    snapshotId: `fanout-context-${seed}`,
    parentSnapshotId: null,
    sessionId: `fanout-session-${seed}`,
    taskId: `fanout-task-${seed}`,
    branchId: `fanout-branch-${seed}`,
    sequence: 0,
    messageRefs: [],
    artifactRefs: [],
    evidenceRefs: [],
    memoryRefs: [],
    compactBoundaryIds: [],
    permissionDigest: "",
    toolCatalogDigest: "",
    topologyRevision: 0,
    createdAt: now,
  };
  return sealTask({
    identity: {
      runId: `fanout-run-${seed}`,
      sessionId: contextPayload.sessionId,
      taskId: contextPayload.taskId,
      parentTaskId: plan.parentTaskId,
      parentSessionId: "",
      attemptId: `${contextPayload.taskId}:attempt:1`,
      attempt: 1,
      leaseId: `fanout-lease-${seed}`,
      lineage: [plan.parentTaskId],
    },
    definition,
    scope: { ...scopePayload, digest: digest(scopePayload) },
    context: { ...contextPayload, checksum: digest(contextPayload) },
    status,
    revision: 0,
    sequence: 0,
    prompt: target.prompt,
    promptDigest: digest(target.prompt),
    executionMode: "background",
    isolation: null,
    isolationReceipt: null,
    messages: [],
    deliveries: [],
    transitions: [],
    childTaskIds: [],
    result: null,
    artifacts: [],
    usage: {},
    error: error instanceof Error ? error.message : String(error),
    createdAt: now,
    updatedAt: now,
    terminalAt: now,
  });
}

export interface FanoutBudgetRequest {
  targetKey: string;
  weight: number;
  minimumTurns: number;
  maximumTurns: number;
  minimumToolCalls: number;
  maximumToolCalls: number;
  minimumInputTokens: number;
  maximumInputTokens: number;
  minimumOutputTokens: number;
  maximumOutputTokens: number;
  priority: number;
  optional: boolean;
}

export interface FanoutBudgetAllocation {
  allocationId: string;
  planId: string;
  parentTaskId: string;
  targetKey: string;
  turns: number;
  toolCalls: number;
  inputTokens: number;
  outputTokens: number;
  resultChars: number;
  priority: number;
  optional: boolean;
  truncated: boolean;
  reason: string;
  digest: string;
}

export interface FanoutBudgetEnvelope {
  envelopeId: string;
  planId: string;
  parentTaskId: string;
  allocations: FanoutBudgetAllocation[];
  unallocatedTurns: number;
  unallocatedToolCalls: number;
  unallocatedInputTokens: number;
  unallocatedOutputTokens: number;
  unallocatedResultChars: number;
  rejectedTargetKeys: string[];
  createdAt: string;
  digest: string;
}

function validBudgetBound(minimum: number, maximum: number): boolean {
  return (
    Number.isSafeInteger(minimum) &&
    Number.isSafeInteger(maximum) &&
    minimum >= 0 &&
    maximum >= minimum
  );
}

function assertFanoutBudgetRequest(request: FanoutBudgetRequest): void {
  if (!request.targetKey.trim())
    throw new E03RuntimeError(
      "fanout_budget_target_missing",
      "fanout budget target key is required",
    );
  if (!Number.isFinite(request.weight) || request.weight <= 0)
    throw new E03RuntimeError(
      "fanout_budget_weight_invalid",
      `fanout budget weight for ${request.targetKey} is invalid`,
    );
  if (!Number.isSafeInteger(request.priority) || request.priority < 0)
    throw new E03RuntimeError(
      "fanout_budget_priority_invalid",
      `fanout priority for ${request.targetKey} is invalid`,
    );
  if (!validBudgetBound(request.minimumTurns, request.maximumTurns))
    throw new E03RuntimeError(
      "fanout_turn_budget_invalid",
      `fanout turn budget for ${request.targetKey} is invalid`,
    );
  if (!validBudgetBound(request.minimumToolCalls, request.maximumToolCalls))
    throw new E03RuntimeError(
      "fanout_tool_budget_invalid",
      `fanout tool budget for ${request.targetKey} is invalid`,
    );
  if (!validBudgetBound(request.minimumInputTokens, request.maximumInputTokens))
    throw new E03RuntimeError(
      "fanout_input_budget_invalid",
      `fanout input budget for ${request.targetKey} is invalid`,
    );
  if (
    !validBudgetBound(request.minimumOutputTokens, request.maximumOutputTokens)
  )
    throw new E03RuntimeError(
      "fanout_output_budget_invalid",
      `fanout output budget for ${request.targetKey} is invalid`,
    );
}

function assertFanoutBudgetAllocation(
  allocation: FanoutBudgetAllocation,
): void {
  const { digest: checksum, ...payload } = allocation;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "fanout_budget_allocation_checksum",
      `allocation ${allocation.allocationId} checksum mismatch`,
    );
  if (
    allocation.turns < 0 ||
    allocation.toolCalls < 0 ||
    allocation.inputTokens < 0 ||
    allocation.outputTokens < 0 ||
    allocation.resultChars < 0
  )
    throw new E03RuntimeError(
      "fanout_budget_allocation_negative",
      `allocation ${allocation.allocationId} contains a negative budget`,
    );
}

export class FanoutBudgetAllocator {
  allocate(input: {
    plan: FanoutPlan;
    parent: E03TaskState;
    requests: readonly FanoutBudgetRequest[];
    resultChars: number;
    now?: string;
  }): FanoutBudgetEnvelope {
    if (input.plan.parentTaskId !== input.parent.identity.taskId)
      throw new E03RuntimeError(
        "fanout_budget_parent_mismatch",
        "fanout budget plan belongs to another parent",
      );
    if (!Number.isSafeInteger(input.resultChars) || input.resultChars < 0)
      throw new E03RuntimeError(
        "fanout_result_budget_invalid",
        "fanout result character budget is invalid",
      );
    const requestByKey = new Map<string, FanoutBudgetRequest>();
    for (const request of input.requests) {
      assertFanoutBudgetRequest(request);
      if (requestByKey.has(request.targetKey))
        throw new E03RuntimeError(
          "duplicate_fanout_budget_request",
          `fanout budget request ${request.targetKey} repeats`,
        );
      requestByKey.set(request.targetKey, structuredClone(request));
    }
    for (const target of input.plan.targets)
      if (!requestByKey.has(target.key))
        throw new E03RuntimeError(
          "fanout_budget_request_missing",
          `fanout budget request for ${target.key} is missing`,
        );
    const available = {
      turns: Math.max(
        0,
        input.parent.definition.budget.maxTurns -
          input.parent.definition.budget.consumedTurns,
      ),
      toolCalls: Math.max(
        0,
        input.parent.definition.budget.maxToolCalls -
          input.parent.definition.budget.consumedToolCalls,
      ),
      inputTokens: Math.max(
        0,
        input.parent.definition.budget.maxInputTokens -
          input.parent.definition.budget.consumedInputTokens,
      ),
      outputTokens: Math.max(
        0,
        input.parent.definition.budget.maxOutputTokens -
          input.parent.definition.budget.consumedOutputTokens,
      ),
      resultChars: input.resultChars,
    };
    const ordered = [...requestByKey.values()].sort(
      (left, right) =>
        left.priority - right.priority ||
        Number(left.optional) - Number(right.optional) ||
        left.targetKey.localeCompare(right.targetKey),
    );
    const minima = {
      turns: ordered.reduce((sum, request) => sum + request.minimumTurns, 0),
      toolCalls: ordered.reduce(
        (sum, request) => sum + request.minimumToolCalls,
        0,
      ),
      inputTokens: ordered.reduce(
        (sum, request) => sum + request.minimumInputTokens,
        0,
      ),
      outputTokens: ordered.reduce(
        (sum, request) => sum + request.minimumOutputTokens,
        0,
      ),
    };
    const admitted: FanoutBudgetRequest[] = [];
    const rejectedTargetKeys: string[] = [];
    const remainingMinima = { ...minima };
    for (const request of ordered) {
      const fits =
        remainingMinima.turns <= available.turns &&
        remainingMinima.toolCalls <= available.toolCalls &&
        remainingMinima.inputTokens <= available.inputTokens &&
        remainingMinima.outputTokens <= available.outputTokens;
      if (fits) {
        admitted.push(request);
        continue;
      }
      if (!request.optional)
        throw new E03RuntimeError(
          "fanout_required_budget_exhausted",
          `required fanout target ${request.targetKey} cannot receive its minimum budget`,
        );
      rejectedTargetKeys.push(request.targetKey);
      remainingMinima.turns -= request.minimumTurns;
      remainingMinima.toolCalls -= request.minimumToolCalls;
      remainingMinima.inputTokens -= request.minimumInputTokens;
      remainingMinima.outputTokens -= request.minimumOutputTokens;
    }
    const totalWeight = admitted.reduce(
      (sum, request) => sum + request.weight,
      0,
    );
    const allocateDimension = (
      key: "turns" | "toolCalls" | "inputTokens" | "outputTokens",
      minimumKey:
        | "minimumTurns"
        | "minimumToolCalls"
        | "minimumInputTokens"
        | "minimumOutputTokens",
      maximumKey:
        | "maximumTurns"
        | "maximumToolCalls"
        | "maximumInputTokens"
        | "maximumOutputTokens",
    ): Map<string, number> => {
      const result = new Map<string, number>();
      const minimumTotal = admitted.reduce(
        (sum, request) => sum + request[minimumKey],
        0,
      );
      const distributable = Math.max(0, available[key] - minimumTotal);
      let used = 0;
      for (const request of admitted) {
        const weighted =
          totalWeight === 0
            ? 0
            : Math.floor((distributable * request.weight) / totalWeight);
        const value = Math.min(
          request[maximumKey],
          request[minimumKey] + weighted,
        );
        result.set(request.targetKey, value);
        used += value;
      }
      let residual = Math.max(0, available[key] - used);
      for (const request of admitted) {
        if (residual === 0) break;
        const current = result.get(request.targetKey)!;
        const room = request[maximumKey] - current;
        const increment = Math.min(room, residual);
        result.set(request.targetKey, current + increment);
        residual -= increment;
      }
      return result;
    };
    const turns = allocateDimension("turns", "minimumTurns", "maximumTurns");
    const toolCalls = allocateDimension(
      "toolCalls",
      "minimumToolCalls",
      "maximumToolCalls",
    );
    const inputTokens = allocateDimension(
      "inputTokens",
      "minimumInputTokens",
      "maximumInputTokens",
    );
    const outputTokens = allocateDimension(
      "outputTokens",
      "minimumOutputTokens",
      "maximumOutputTokens",
    );
    const perTargetResultChars = admitted.length
      ? Math.floor(available.resultChars / admitted.length)
      : 0;
    const allocations = admitted.map((request) => {
      const values = {
        turns: turns.get(request.targetKey)!,
        toolCalls: toolCalls.get(request.targetKey)!,
        inputTokens: inputTokens.get(request.targetKey)!,
        outputTokens: outputTokens.get(request.targetKey)!,
        resultChars: perTargetResultChars,
      };
      const truncated =
        values.turns < request.maximumTurns ||
        values.toolCalls < request.maximumToolCalls ||
        values.inputTokens < request.maximumInputTokens ||
        values.outputTokens < request.maximumOutputTokens;
      const payload = {
        allocationId: `fanout-allocation-${digest({
          planId: input.plan.planId,
          targetKey: request.targetKey,
          values,
        }).slice(0, 32)}`,
        planId: input.plan.planId,
        parentTaskId: input.parent.identity.taskId,
        targetKey: request.targetKey,
        ...values,
        priority: request.priority,
        optional: request.optional,
        truncated,
        reason: truncated ? "weighted_parent_budget" : "requested_maximum",
      };
      const allocation = { ...payload, digest: digest(payload) };
      assertFanoutBudgetAllocation(allocation);
      return allocation;
    });
    const used = {
      turns: allocations.reduce((sum, value) => sum + value.turns, 0),
      toolCalls: allocations.reduce((sum, value) => sum + value.toolCalls, 0),
      inputTokens: allocations.reduce(
        (sum, value) => sum + value.inputTokens,
        0,
      ),
      outputTokens: allocations.reduce(
        (sum, value) => sum + value.outputTokens,
        0,
      ),
      resultChars: allocations.reduce(
        (sum, value) => sum + value.resultChars,
        0,
      ),
    };
    const payload = {
      envelopeId: `fanout-budget-${digest({
        planId: input.plan.planId,
        allocations: allocations.map((value) => value.digest),
      }).slice(0, 32)}`,
      planId: input.plan.planId,
      parentTaskId: input.parent.identity.taskId,
      allocations,
      unallocatedTurns: available.turns - used.turns,
      unallocatedToolCalls: available.toolCalls - used.toolCalls,
      unallocatedInputTokens: available.inputTokens - used.inputTokens,
      unallocatedOutputTokens: available.outputTokens - used.outputTokens,
      unallocatedResultChars: available.resultChars - used.resultChars,
      rejectedTargetKeys: rejectedTargetKeys.sort(),
      createdAt: input.now ?? new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }
}

export type FanoutSlotState =
  | "planned"
  | "ready"
  | "leased"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "cancelled"
  | "skipped";

export interface FanoutSlot {
  slotId: string;
  planId: string;
  targetKey: string;
  targetIndex: number;
  wave: number;
  state: FanoutSlotState;
  taskId: string | null;
  leaseId: string | null;
  workerId: string | null;
  allocationId: string;
  dependencies: string[];
  optional: boolean;
  priority: number;
  revision: number;
  createdAt: string;
  updatedAt: string;
  terminalAt: string | null;
  error: string;
  digest: string;
}

export interface FanoutWave {
  waveId: string;
  planId: string;
  wave: number;
  slotIds: string[];
  maximumConcurrency: number;
  readyCount: number;
  blockedCount: number;
  optionalCount: number;
  createdAt: string;
  digest: string;
}

export interface FanoutSchedule {
  scheduleId: string;
  planId: string;
  parentTaskId: string;
  slots: FanoutSlot[];
  waves: FanoutWave[];
  maximumConcurrency: number;
  failureMode: FanoutPlan["failureMode"];
  createdAt: string;
  digest: string;
}

function assertFanoutSlot(slot: FanoutSlot): void {
  const { digest: checksum, ...payload } = slot;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "fanout_slot_checksum",
      `fanout slot ${slot.slotId} checksum mismatch`,
    );
  if (slot.targetIndex < 0 || slot.wave < 0 || slot.revision < 1)
    throw new E03RuntimeError(
      "fanout_slot_position_invalid",
      `fanout slot ${slot.slotId} position is invalid`,
    );
  if (slot.state === "leased" && (!slot.leaseId || !slot.workerId))
    throw new E03RuntimeError(
      "fanout_slot_lease_missing",
      `leased fanout slot ${slot.slotId} has no lease`,
    );
  if (
    ["completed", "failed", "cancelled", "skipped"].includes(slot.state) &&
    !slot.terminalAt
  )
    throw new E03RuntimeError(
      "fanout_slot_terminal_time",
      `terminal fanout slot ${slot.slotId} has no terminal time`,
    );
}

function resealFanoutSlot(
  slot: FanoutSlot,
  patch: Partial<Omit<FanoutSlot, "slotId" | "planId" | "targetKey">>,
): FanoutSlot {
  const payload = {
    ...slot,
    ...patch,
    slotId: slot.slotId,
    planId: slot.planId,
    targetKey: slot.targetKey,
    digest: "",
  };
  const { digest: _, ...body } = payload;
  const next = { ...body, digest: digest(body) };
  assertFanoutSlot(next);
  return next;
}

export class FanoutWaveScheduler {
  private schedules = new Map<string, FanoutSchedule>();

  create(input: {
    plan: FanoutPlan;
    envelope: FanoutBudgetEnvelope;
    dependencies?: Readonly<Record<string, readonly string[]>>;
    now?: string;
  }): FanoutSchedule {
    if (input.envelope.planId !== input.plan.planId)
      throw new E03RuntimeError(
        "fanout_schedule_budget_mismatch",
        "fanout schedule budget belongs to another plan",
      );
    const existing = this.schedules.get(input.plan.planId);
    if (existing) return structuredClone(existing);
    const allocationByKey = new Map(
      input.envelope.allocations.map((allocation) => [
        allocation.targetKey,
        allocation,
      ]),
    );
    const rejected = new Set(input.envelope.rejectedTargetKeys);
    const targetKeys = new Set(input.plan.targets.map((target) => target.key));
    const depths = new Map<string, number>();
    const visit = (targetKey: string, stack = new Set<string>()): number => {
      const known = depths.get(targetKey);
      if (known !== undefined) return known;
      if (stack.has(targetKey))
        throw new E03RuntimeError(
          "fanout_dependency_cycle",
          `fanout target ${targetKey} creates a dependency cycle`,
        );
      stack.add(targetKey);
      const dependencies = [...(input.dependencies?.[targetKey] ?? [])].sort();
      let depth = 0;
      for (const dependency of dependencies) {
        if (!targetKeys.has(dependency))
          throw new E03RuntimeError(
            "fanout_dependency_missing",
            `fanout dependency ${dependency} for ${targetKey} is missing`,
          );
        depth = Math.max(depth, visit(dependency, new Set(stack)) + 1);
      }
      depths.set(targetKey, depth);
      return depth;
    };
    for (const target of input.plan.targets) visit(target.key);
    const createdAt = input.now ?? new Date().toISOString();
    const slots = input.plan.targets.map((target, targetIndex) => {
      const allocation = allocationByKey.get(target.key);
      const skipped = rejected.has(target.key);
      if (!allocation && !skipped)
        throw new E03RuntimeError(
          "fanout_schedule_allocation_missing",
          `fanout target ${target.key} has no budget allocation`,
        );
      const dependencies = [...(input.dependencies?.[target.key] ?? [])].sort();
      const payload = {
        slotId: `fanout-slot-${digest({
          planId: input.plan.planId,
          targetKey: target.key,
        }).slice(0, 32)}`,
        planId: input.plan.planId,
        targetKey: target.key,
        targetIndex,
        wave: depths.get(target.key)!,
        state: skipped
          ? ("skipped" as const)
          : dependencies.length
            ? ("planned" as const)
            : ("ready" as const),
        taskId: null,
        leaseId: null,
        workerId: null,
        allocationId: allocation?.allocationId ?? "",
        dependencies,
        optional: allocation?.optional ?? true,
        priority: allocation?.priority ?? Number.MAX_SAFE_INTEGER,
        revision: 1,
        createdAt,
        updatedAt: createdAt,
        terminalAt: skipped ? createdAt : null,
        error: skipped ? "budget_not_admitted" : "",
      };
      const slot = { ...payload, digest: digest(payload) };
      assertFanoutSlot(slot);
      return slot;
    });
    const maximumWave = Math.max(0, ...slots.map((slot) => slot.wave));
    const waves: FanoutWave[] = [];
    for (let wave = 0; wave <= maximumWave; wave += 1) {
      const waveSlots = slots.filter((slot) => slot.wave === wave);
      if (!waveSlots.length) continue;
      const payload = {
        waveId: `fanout-wave-${digest({
          planId: input.plan.planId,
          wave,
          slotIds: waveSlots.map((slot) => slot.slotId),
        }).slice(0, 32)}`,
        planId: input.plan.planId,
        wave,
        slotIds: waveSlots.map((slot) => slot.slotId),
        maximumConcurrency: Math.min(
          input.plan.maximumConcurrency,
          waveSlots.length,
        ),
        readyCount: waveSlots.filter((slot) => slot.state === "ready").length,
        blockedCount: waveSlots.filter((slot) => slot.state === "planned")
          .length,
        optionalCount: waveSlots.filter((slot) => slot.optional).length,
        createdAt,
      };
      waves.push({ ...payload, digest: digest(payload) });
    }
    const payload = {
      scheduleId: `fanout-schedule-${digest({
        planId: input.plan.planId,
        slots: slots.map((slot) => slot.digest),
      }).slice(0, 32)}`,
      planId: input.plan.planId,
      parentTaskId: input.plan.parentTaskId,
      slots,
      waves,
      maximumConcurrency: input.plan.maximumConcurrency,
      failureMode: input.plan.failureMode,
      createdAt,
    };
    const schedule = { ...payload, digest: digest(payload) };
    this.verify(schedule);
    this.schedules.set(input.plan.planId, schedule);
    return structuredClone(schedule);
  }

  acquire(input: {
    planId: string;
    workerId: string;
    maximum: number;
    now?: string;
  }): FanoutSlot[] {
    const schedule = this.require(input.planId);
    if (!input.workerId.trim())
      throw new E03RuntimeError(
        "fanout_worker_missing",
        "fanout worker id is required",
      );
    if (!Number.isSafeInteger(input.maximum) || input.maximum < 1)
      throw new E03RuntimeError(
        "fanout_acquire_limit_invalid",
        "fanout acquire maximum is invalid",
      );
    this.refreshReady(schedule);
    const active = schedule.slots.filter((slot) =>
      ["leased", "running", "waiting"].includes(slot.state),
    ).length;
    const available = Math.max(
      0,
      Math.min(input.maximum, schedule.maximumConcurrency - active),
    );
    const selected = schedule.slots
      .filter((slot) => slot.state === "ready")
      .sort(
        (left, right) =>
          left.wave - right.wave ||
          left.priority - right.priority ||
          left.targetIndex - right.targetIndex,
      )
      .slice(0, available);
    const now = input.now ?? new Date().toISOString();
    const acquired = selected.map((slot) => {
      const leaseId = `fanout-slot-lease-${digest({
        slotId: slot.slotId,
        revision: slot.revision,
        workerId: input.workerId,
      }).slice(0, 32)}`;
      const next = resealFanoutSlot(slot, {
        state: "leased",
        leaseId,
        workerId: input.workerId,
        revision: slot.revision + 1,
        updatedAt: now,
      });
      this.replace(schedule, next);
      return next;
    });
    this.resealSchedule(schedule);
    return acquired.map((slot) => structuredClone(slot));
  }

  bind(input: {
    planId: string;
    slotId: string;
    leaseId: string;
    task: E03TaskState;
    now?: string;
  }): FanoutSlot {
    const schedule = this.require(input.planId);
    const slot = this.slot(schedule, input.slotId);
    if (slot.state !== "leased" || slot.leaseId !== input.leaseId)
      throw new E03RuntimeError(
        "fanout_slot_stale_lease",
        `fanout slot ${slot.slotId} is not held by this lease`,
      );
    if (input.task.identity.parentTaskId !== schedule.parentTaskId)
      throw new E03RuntimeError(
        "fanout_slot_task_parent",
        "fanout task belongs to another parent",
      );
    const now = input.now ?? new Date().toISOString();
    const next = resealFanoutSlot(slot, {
      state: "running",
      taskId: input.task.identity.taskId,
      revision: slot.revision + 1,
      updatedAt: now,
    });
    this.replace(schedule, next);
    this.resealSchedule(schedule);
    return structuredClone(next);
  }

  settle(input: {
    planId: string;
    slotId: string;
    leaseId: string;
    task: E03TaskState;
    now?: string;
  }): FanoutSlot {
    const schedule = this.require(input.planId);
    const slot = this.slot(schedule, input.slotId);
    if (slot.leaseId !== input.leaseId)
      throw new E03RuntimeError(
        "fanout_settle_stale_lease",
        `fanout slot ${slot.slotId} lease changed`,
      );
    if (slot.taskId !== input.task.identity.taskId)
      throw new E03RuntimeError(
        "fanout_settle_task_mismatch",
        "fanout settlement belongs to another task",
      );
    const state: FanoutSlotState =
      input.task.status === "completed"
        ? "completed"
        : input.task.status === "failed"
          ? "failed"
          : input.task.status === "cancelled" || input.task.status === "killed"
            ? "cancelled"
            : input.task.status === "waiting"
              ? "waiting"
              : "running";
    const now = input.now ?? new Date().toISOString();
    const terminal = ["completed", "failed", "cancelled"].includes(state);
    const next = resealFanoutSlot(slot, {
      state,
      revision: slot.revision + 1,
      updatedAt: now,
      terminalAt: terminal ? now : null,
      error: input.task.error,
    });
    this.replace(schedule, next);
    if (schedule.failureMode === "fail-fast" && state === "failed")
      this.cancelPending(schedule, `fail_fast:${slot.targetKey}`, now);
    this.refreshReady(schedule);
    this.resealSchedule(schedule);
    return structuredClone(next);
  }

  restore(schedules: readonly FanoutSchedule[]): void {
    const next = new Map<string, FanoutSchedule>();
    for (const raw of schedules) {
      const schedule = structuredClone(raw);
      this.verify(schedule);
      if (next.has(schedule.planId))
        throw new E03RuntimeError(
          "duplicate_fanout_schedule",
          `fanout schedule ${schedule.planId} repeats`,
        );
      next.set(schedule.planId, schedule);
    }
    this.schedules = next;
  }

  snapshot(planId?: string): FanoutSchedule[] {
    return [...this.schedules.values()]
      .filter((schedule) => !planId || schedule.planId === planId)
      .sort((left, right) => left.planId.localeCompare(right.planId))
      .map((schedule) => structuredClone(schedule));
  }

  private require(planId: string): FanoutSchedule {
    const schedule = this.schedules.get(planId);
    if (!schedule)
      throw new E03RuntimeError(
        "fanout_schedule_missing",
        `fanout schedule ${planId} is missing`,
      );
    this.verify(schedule);
    return schedule;
  }

  private slot(schedule: FanoutSchedule, slotId: string): FanoutSlot {
    const slot = schedule.slots.find(
      (candidate) => candidate.slotId === slotId,
    );
    if (!slot)
      throw new E03RuntimeError(
        "fanout_slot_missing",
        `fanout slot ${slotId} is missing`,
      );
    return slot;
  }

  private replace(schedule: FanoutSchedule, slot: FanoutSlot): void {
    const index = schedule.slots.findIndex(
      (candidate) => candidate.slotId === slot.slotId,
    );
    if (index < 0)
      throw new E03RuntimeError(
        "fanout_slot_missing",
        `fanout slot ${slot.slotId} is missing`,
      );
    schedule.slots[index] = slot;
  }

  private refreshReady(schedule: FanoutSchedule): void {
    const byKey = new Map(schedule.slots.map((slot) => [slot.targetKey, slot]));
    for (const slot of schedule.slots) {
      if (slot.state !== "planned") continue;
      const prerequisites = slot.dependencies.map((key) => byKey.get(key));
      if (prerequisites.some((candidate) => !candidate))
        throw new E03RuntimeError(
          "fanout_dependency_lost",
          `fanout slot ${slot.slotId} lost a dependency`,
        );
      const failed = prerequisites.some((candidate) =>
        ["failed", "cancelled", "skipped"].includes(candidate!.state),
      );
      const completed = prerequisites.every(
        (candidate) => candidate!.state === "completed",
      );
      if (failed) {
        const next = resealFanoutSlot(slot, {
          state: "skipped",
          revision: slot.revision + 1,
          updatedAt: new Date().toISOString(),
          terminalAt: new Date().toISOString(),
          error: "dependency_failed",
        });
        this.replace(schedule, next);
      } else if (completed) {
        const next = resealFanoutSlot(slot, {
          state: "ready",
          revision: slot.revision + 1,
          updatedAt: new Date().toISOString(),
        });
        this.replace(schedule, next);
      }
    }
  }

  private cancelPending(
    schedule: FanoutSchedule,
    reason: string,
    now: string,
  ): void {
    for (const slot of [...schedule.slots]) {
      if (!["planned", "ready", "leased"].includes(slot.state)) continue;
      this.replace(
        schedule,
        resealFanoutSlot(slot, {
          state: "cancelled",
          revision: slot.revision + 1,
          updatedAt: now,
          terminalAt: now,
          error: reason,
        }),
      );
    }
  }

  private resealSchedule(schedule: FanoutSchedule): void {
    const payload = {
      ...schedule,
      slots: schedule.slots.map((slot) => structuredClone(slot)),
      digest: "",
    };
    const { digest: _, ...body } = payload;
    const next = { ...body, digest: digest(body) };
    this.verify(next);
    Object.assign(schedule, next);
    this.schedules.set(schedule.planId, schedule);
  }

  private verify(schedule: FanoutSchedule): void {
    const { digest: checksum, ...payload } = schedule;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "fanout_schedule_checksum",
        `fanout schedule ${schedule.scheduleId} checksum mismatch`,
      );
    const slotIds = new Set<string>();
    const targetKeys = new Set<string>();
    for (const slot of schedule.slots) {
      assertFanoutSlot(slot);
      if (slot.planId !== schedule.planId)
        throw new E03RuntimeError(
          "fanout_schedule_slot_plan",
          `slot ${slot.slotId} belongs to another plan`,
        );
      if (slotIds.has(slot.slotId) || targetKeys.has(slot.targetKey))
        throw new E03RuntimeError(
          "duplicate_fanout_schedule_slot",
          `fanout slot ${slot.slotId} or target ${slot.targetKey} repeats`,
        );
      slotIds.add(slot.slotId);
      targetKeys.add(slot.targetKey);
    }
    for (const wave of schedule.waves) {
      const { digest: waveChecksum, ...wavePayload } = wave;
      if (digest(wavePayload) !== waveChecksum)
        throw new E03RuntimeError(
          "fanout_wave_checksum",
          `fanout wave ${wave.waveId} checksum mismatch`,
        );
      for (const slotId of wave.slotIds)
        if (!slotIds.has(slotId))
          throw new E03RuntimeError(
            "fanout_wave_slot_missing",
            `fanout wave ${wave.waveId} references missing slot ${slotId}`,
          );
    }
  }
}

export interface FanoutOutcome {
  outcomeId: string;
  planId: string;
  targetKey: string;
  taskId: string;
  status: "completed" | "failed" | "cancelled" | "skipped";
  resultDigest: string;
  artifactDigests: string[];
  errorCode: string;
  errorDigest: string;
  attempts: number;
  durationMs: number;
  inputTokens: number;
  outputTokens: number;
  toolCalls: number;
  settledAt: string;
  digest: string;
}

export interface FanoutAggregate {
  aggregateId: string;
  planId: string;
  parentTaskId: string;
  status: "pending" | "succeeded" | "partial" | "failed" | "cancelled";
  totalTargets: number;
  completedTargets: number;
  failedTargets: number;
  cancelledTargets: number;
  skippedTargets: number;
  pendingTargets: number;
  successfulTargetKeys: string[];
  failedTargetKeys: string[];
  cancelledTargetKeys: string[];
  skippedTargetKeys: string[];
  pendingTargetKeys: string[];
  resultDigests: string[];
  artifactDigests: string[];
  totalDurationMs: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  totalToolCalls: number;
  summary: string;
  finalizedAt: string | null;
  digest: string;
}

function assertFanoutOutcome(outcome: FanoutOutcome): void {
  const { digest: checksum, ...payload } = outcome;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "fanout_outcome_checksum",
      `fanout outcome ${outcome.outcomeId} checksum mismatch`,
    );
  if (!outcome.planId || !outcome.targetKey || !outcome.taskId)
    throw new E03RuntimeError(
      "fanout_outcome_identity",
      "fanout outcome identity is incomplete",
    );
  if (
    outcome.attempts < 1 ||
    outcome.durationMs < 0 ||
    outcome.inputTokens < 0 ||
    outcome.outputTokens < 0 ||
    outcome.toolCalls < 0
  )
    throw new E03RuntimeError(
      "fanout_outcome_metrics",
      `fanout outcome ${outcome.outcomeId} metrics are invalid`,
    );
  if (outcome.status === "completed" && !outcome.resultDigest)
    throw new E03RuntimeError(
      "fanout_outcome_result_missing",
      "completed fanout outcome requires a result digest",
    );
  if (outcome.status === "failed" && !outcome.errorCode)
    throw new E03RuntimeError(
      "fanout_outcome_error_missing",
      "failed fanout outcome requires an error code",
    );
}

function numericFanoutUsage(task: E03TaskState, key: string): number {
  const value = task.usage[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : 0;
}

export class FanoutResultReducer {
  private outcomes = new Map<string, FanoutOutcome>();
  private targetOutcomes = new Map<string, string>();

  record(input: {
    plan: FanoutPlan;
    targetKey: string;
    task: E03TaskState;
    errorCode?: string;
    durationMs: number;
    now?: string;
  }): FanoutOutcome {
    const target = input.plan.targets.find(
      (candidate) => candidate.key === input.targetKey,
    );
    if (!target)
      throw new E03RuntimeError(
        "fanout_outcome_target_missing",
        `fanout target ${input.targetKey} is not in plan`,
      );
    if (input.task.identity.parentTaskId !== input.plan.parentTaskId)
      throw new E03RuntimeError(
        "fanout_outcome_parent_mismatch",
        "fanout outcome task belongs to another parent",
      );
    if (!Number.isSafeInteger(input.durationMs) || input.durationMs < 0)
      throw new E03RuntimeError(
        "fanout_outcome_duration_invalid",
        "fanout outcome duration is invalid",
      );
    const status: FanoutOutcome["status"] =
      input.task.status === "completed"
        ? "completed"
        : input.task.status === "failed"
          ? "failed"
          : input.task.status === "cancelled" || input.task.status === "killed"
            ? "cancelled"
            : (() => {
                throw new E03RuntimeError(
                  "fanout_outcome_non_terminal",
                  `task ${input.task.identity.taskId} is not terminal`,
                );
              })();
    const targetIdentity = `${input.plan.planId}:${input.targetKey}`;
    const priorOutcomeId = this.targetOutcomes.get(targetIdentity);
    if (priorOutcomeId) {
      const prior = this.outcomes.get(priorOutcomeId)!;
      if (
        prior.taskId !== input.task.identity.taskId ||
        prior.status !== status ||
        prior.resultDigest !== digest(input.task.result)
      )
        throw new E03RuntimeError(
          "fanout_outcome_conflict",
          `fanout target ${input.targetKey} already has another outcome`,
        );
      return structuredClone(prior);
    }
    const resultDigest =
      status === "completed" ? digest(input.task.result) : "";
    const errorCode =
      status === "failed" ? input.errorCode?.trim() || "task_failed" : "";
    const payload = {
      outcomeId: `fanout-outcome-${digest({
        planId: input.plan.planId,
        targetKey: input.targetKey,
        taskId: input.task.identity.taskId,
        revision: input.task.revision,
      }).slice(0, 32)}`,
      planId: input.plan.planId,
      targetKey: input.targetKey,
      taskId: input.task.identity.taskId,
      status,
      resultDigest,
      artifactDigests: input.task.artifacts
        .map((artifact) => digest(artifact))
        .sort(),
      errorCode,
      errorDigest: status === "failed" ? digest(input.task.error) : "",
      attempts: input.task.identity.attempt,
      durationMs: input.durationMs,
      inputTokens: numericFanoutUsage(input.task, "inputTokens"),
      outputTokens: numericFanoutUsage(input.task, "outputTokens"),
      toolCalls: numericFanoutUsage(input.task, "toolCalls"),
      settledAt: input.now ?? new Date().toISOString(),
    };
    const outcome = { ...payload, digest: digest(payload) };
    assertFanoutOutcome(outcome);
    this.outcomes.set(outcome.outcomeId, outcome);
    this.targetOutcomes.set(targetIdentity, outcome.outcomeId);
    return structuredClone(outcome);
  }

  skip(input: {
    plan: FanoutPlan;
    targetKey: string;
    reason: string;
    now?: string;
  }): FanoutOutcome {
    if (!input.plan.targets.some((target) => target.key === input.targetKey))
      throw new E03RuntimeError(
        "fanout_skip_target_missing",
        `fanout target ${input.targetKey} is not in plan`,
      );
    const targetIdentity = `${input.plan.planId}:${input.targetKey}`;
    const priorOutcomeId = this.targetOutcomes.get(targetIdentity);
    if (priorOutcomeId)
      return structuredClone(this.outcomes.get(priorOutcomeId)!);
    const payload = {
      outcomeId: `fanout-outcome-${digest({
        planId: input.plan.planId,
        targetKey: input.targetKey,
        status: "skipped",
      }).slice(0, 32)}`,
      planId: input.plan.planId,
      targetKey: input.targetKey,
      taskId: `skipped:${input.targetKey}`,
      status: "skipped" as const,
      resultDigest: "",
      artifactDigests: [],
      errorCode: input.reason.trim() || "fanout_target_skipped",
      errorDigest: digest(input.reason),
      attempts: 1,
      durationMs: 0,
      inputTokens: 0,
      outputTokens: 0,
      toolCalls: 0,
      settledAt: input.now ?? new Date().toISOString(),
    };
    const outcome = { ...payload, digest: digest(payload) };
    assertFanoutOutcome(outcome);
    this.outcomes.set(outcome.outcomeId, outcome);
    this.targetOutcomes.set(targetIdentity, outcome.outcomeId);
    return structuredClone(outcome);
  }

  aggregate(plan: FanoutPlan): FanoutAggregate {
    const outcomes = this.forPlan(plan.planId);
    const byTarget = new Map(
      outcomes.map((outcome) => [outcome.targetKey, outcome]),
    );
    const successfulTargetKeys: string[] = [];
    const failedTargetKeys: string[] = [];
    const cancelledTargetKeys: string[] = [];
    const skippedTargetKeys: string[] = [];
    const pendingTargetKeys: string[] = [];
    for (const target of plan.targets) {
      const outcome = byTarget.get(target.key);
      if (!outcome) pendingTargetKeys.push(target.key);
      else if (outcome.status === "completed")
        successfulTargetKeys.push(target.key);
      else if (outcome.status === "failed") failedTargetKeys.push(target.key);
      else if (outcome.status === "cancelled")
        cancelledTargetKeys.push(target.key);
      else skippedTargetKeys.push(target.key);
    }
    let status: FanoutAggregate["status"] = "pending";
    if (!pendingTargetKeys.length) {
      if (successfulTargetKeys.length === plan.targets.length)
        status = "succeeded";
      else if (!successfulTargetKeys.length && failedTargetKeys.length)
        status = "failed";
      else if (!successfulTargetKeys.length && cancelledTargetKeys.length)
        status = "cancelled";
      else status = "partial";
    } else if (plan.failureMode === "fail-fast" && failedTargetKeys.length)
      status = "failed";
    const final = status !== "pending";
    const artifactDigests = [
      ...new Set(outcomes.flatMap((item) => item.artifactDigests)),
    ].sort();
    const resultDigests = outcomes
      .filter((item) => item.resultDigest)
      .map((item) => item.resultDigest)
      .sort();
    const payload = {
      aggregateId: `fanout-aggregate-${digest({
        planId: plan.planId,
        outcomes: outcomes.map((outcome) => outcome.digest),
      }).slice(0, 32)}`,
      planId: plan.planId,
      parentTaskId: plan.parentTaskId,
      status,
      totalTargets: plan.targets.length,
      completedTargets: successfulTargetKeys.length,
      failedTargets: failedTargetKeys.length,
      cancelledTargets: cancelledTargetKeys.length,
      skippedTargets: skippedTargetKeys.length,
      pendingTargets: pendingTargetKeys.length,
      successfulTargetKeys: successfulTargetKeys.sort(),
      failedTargetKeys: failedTargetKeys.sort(),
      cancelledTargetKeys: cancelledTargetKeys.sort(),
      skippedTargetKeys: skippedTargetKeys.sort(),
      pendingTargetKeys: pendingTargetKeys.sort(),
      resultDigests,
      artifactDigests,
      totalDurationMs: outcomes.reduce((sum, item) => sum + item.durationMs, 0),
      totalInputTokens: outcomes.reduce(
        (sum, item) => sum + item.inputTokens,
        0,
      ),
      totalOutputTokens: outcomes.reduce(
        (sum, item) => sum + item.outputTokens,
        0,
      ),
      totalToolCalls: outcomes.reduce((sum, item) => sum + item.toolCalls, 0),
      summary: `${successfulTargetKeys.length}/${plan.targets.length} completed, ${failedTargetKeys.length} failed, ${cancelledTargetKeys.length} cancelled, ${skippedTargetKeys.length} skipped`,
      finalizedAt: final ? new Date().toISOString() : null,
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(outcomes: readonly FanoutOutcome[]): void {
    const next = new Map<string, FanoutOutcome>();
    const targets = new Map<string, string>();
    for (const raw of outcomes) {
      const outcome = structuredClone(raw);
      assertFanoutOutcome(outcome);
      const targetIdentity = `${outcome.planId}:${outcome.targetKey}`;
      if (next.has(outcome.outcomeId) || targets.has(targetIdentity))
        throw new E03RuntimeError(
          "duplicate_fanout_outcome",
          `fanout outcome ${outcome.outcomeId} repeats`,
        );
      next.set(outcome.outcomeId, outcome);
      targets.set(targetIdentity, outcome.outcomeId);
    }
    this.outcomes = next;
    this.targetOutcomes = targets;
  }

  forPlan(planId: string): FanoutOutcome[] {
    return [...this.outcomes.values()]
      .filter((outcome) => outcome.planId === planId)
      .sort((left, right) => left.targetKey.localeCompare(right.targetKey))
      .map((outcome) => structuredClone(outcome));
  }

  snapshot(): FanoutOutcome[] {
    return [...this.outcomes.values()]
      .sort((left, right) => left.outcomeId.localeCompare(right.outcomeId))
      .map((outcome) => structuredClone(outcome));
  }
}

export interface FanoutProviderHealth {
  providerId: string;
  state: "closed" | "open" | "half-open";
  consecutiveFailures: number;
  totalSuccesses: number;
  totalFailures: number;
  totalTimeouts: number;
  inFlight: number;
  maximumInFlight: number;
  openedAt: string | null;
  retryAt: string | null;
  lastSuccessAt: string | null;
  lastFailureAt: string | null;
  lastFailureCode: string;
  revision: number;
  digest: string;
}

function sealProviderHealth(
  health: Omit<FanoutProviderHealth, "digest">,
): FanoutProviderHealth {
  return { ...health, digest: digest(health) };
}

function assertProviderHealth(health: FanoutProviderHealth): void {
  const { digest: checksum, ...payload } = health;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "fanout_provider_health_checksum",
      `provider ${health.providerId} health checksum mismatch`,
    );
  if (
    health.consecutiveFailures < 0 ||
    health.totalSuccesses < 0 ||
    health.totalFailures < 0 ||
    health.totalTimeouts < 0 ||
    health.inFlight < 0 ||
    health.maximumInFlight < 1 ||
    health.revision < 1
  )
    throw new E03RuntimeError(
      "fanout_provider_health_counter",
      `provider ${health.providerId} health counters are invalid`,
    );
  if (health.inFlight > health.maximumInFlight)
    throw new E03RuntimeError(
      "fanout_provider_overcommitted",
      `provider ${health.providerId} exceeds its concurrency ceiling`,
    );
}

export class FanoutProviderCircuitRuntime {
  private providers = new Map<string, FanoutProviderHealth>();

  register(providerId: string, maximumInFlight: number): FanoutProviderHealth {
    const id = providerId.trim();
    if (!id)
      throw new E03RuntimeError(
        "fanout_provider_id_missing",
        "fanout provider id is required",
      );
    if (!Number.isSafeInteger(maximumInFlight) || maximumInFlight < 1)
      throw new E03RuntimeError(
        "fanout_provider_concurrency_invalid",
        "fanout provider concurrency ceiling is invalid",
      );
    const existing = this.providers.get(id);
    if (existing) return structuredClone(existing);
    const health = sealProviderHealth({
      providerId: id,
      state: "closed",
      consecutiveFailures: 0,
      totalSuccesses: 0,
      totalFailures: 0,
      totalTimeouts: 0,
      inFlight: 0,
      maximumInFlight,
      openedAt: null,
      retryAt: null,
      lastSuccessAt: null,
      lastFailureAt: null,
      lastFailureCode: "",
      revision: 1,
    });
    this.providers.set(id, health);
    return structuredClone(health);
  }

  acquire(
    providerId: string,
    now = new Date().toISOString(),
  ): FanoutProviderHealth {
    let health = this.require(providerId);
    if (
      health.state === "open" &&
      health.retryAt &&
      Date.parse(now) >= Date.parse(health.retryAt)
    )
      health = this.update(health, {
        state: "half-open",
        revision: health.revision + 1,
      });
    if (health.state === "open")
      throw new E03RuntimeError(
        "fanout_provider_circuit_open",
        `fanout provider ${providerId} circuit is open`,
        { retryAt: health.retryAt },
      );
    if (health.inFlight >= health.maximumInFlight)
      throw new E03RuntimeError(
        "fanout_provider_saturated",
        `fanout provider ${providerId} is saturated`,
      );
    health = this.update(health, {
      inFlight: health.inFlight + 1,
      revision: health.revision + 1,
    });
    return structuredClone(health);
  }

  success(
    providerId: string,
    now = new Date().toISOString(),
  ): FanoutProviderHealth {
    const health = this.require(providerId);
    if (health.inFlight < 1)
      throw new E03RuntimeError(
        "fanout_provider_success_without_lease",
        `fanout provider ${providerId} has no in-flight request`,
      );
    return structuredClone(
      this.update(health, {
        state: "closed",
        consecutiveFailures: 0,
        totalSuccesses: health.totalSuccesses + 1,
        inFlight: health.inFlight - 1,
        openedAt: null,
        retryAt: null,
        lastSuccessAt: now,
        lastFailureCode: "",
        revision: health.revision + 1,
      }),
    );
  }

  failure(input: {
    providerId: string;
    code: string;
    timeout?: boolean;
    failureThreshold: number;
    openMs: number;
    now?: string;
  }): FanoutProviderHealth {
    const health = this.require(input.providerId);
    if (health.inFlight < 1)
      throw new E03RuntimeError(
        "fanout_provider_failure_without_lease",
        `fanout provider ${input.providerId} has no in-flight request`,
      );
    if (
      !Number.isSafeInteger(input.failureThreshold) ||
      input.failureThreshold < 1
    )
      throw new E03RuntimeError(
        "fanout_provider_threshold_invalid",
        "fanout provider failure threshold is invalid",
      );
    if (!Number.isSafeInteger(input.openMs) || input.openMs < 1)
      throw new E03RuntimeError(
        "fanout_provider_open_window_invalid",
        "fanout provider open duration is invalid",
      );
    const now = input.now ?? new Date().toISOString();
    const failures = health.consecutiveFailures + 1;
    const opened =
      failures >= input.failureThreshold || health.state === "half-open";
    return structuredClone(
      this.update(health, {
        state: opened ? "open" : health.state,
        consecutiveFailures: failures,
        totalFailures: health.totalFailures + 1,
        totalTimeouts: health.totalTimeouts + Number(input.timeout ?? false),
        inFlight: health.inFlight - 1,
        openedAt: opened ? now : health.openedAt,
        retryAt: opened
          ? new Date(Date.parse(now) + input.openMs).toISOString()
          : health.retryAt,
        lastFailureAt: now,
        lastFailureCode: input.code.trim() || "provider_failure",
        revision: health.revision + 1,
      }),
    );
  }

  restore(providers: readonly FanoutProviderHealth[]): void {
    const next = new Map<string, FanoutProviderHealth>();
    for (const raw of providers) {
      const health = structuredClone(raw);
      assertProviderHealth(health);
      if (next.has(health.providerId))
        throw new E03RuntimeError(
          "duplicate_fanout_provider",
          `fanout provider ${health.providerId} repeats`,
        );
      next.set(health.providerId, health);
    }
    this.providers = next;
  }

  snapshot(): FanoutProviderHealth[] {
    return [...this.providers.values()]
      .sort((left, right) => left.providerId.localeCompare(right.providerId))
      .map((health) => structuredClone(health));
  }

  private require(providerId: string): FanoutProviderHealth {
    const health = this.providers.get(providerId);
    if (!health)
      throw new E03RuntimeError(
        "fanout_provider_missing",
        `fanout provider ${providerId} is missing`,
      );
    assertProviderHealth(health);
    return health;
  }

  private update(
    health: FanoutProviderHealth,
    patch: Partial<Omit<FanoutProviderHealth, "providerId" | "digest">>,
  ): FanoutProviderHealth {
    const { digest: _, ...current } = health;
    const next = sealProviderHealth({
      ...current,
      ...patch,
      providerId: health.providerId,
    });
    const normalized = (() => {
      const { digest: _, ...payload } = next;
      return { ...payload, digest: digest(payload) };
    })();
    assertProviderHealth(normalized);
    this.providers.set(normalized.providerId, normalized);
    return normalized;
  }
}

export type FanoutDispatchState =
  | "pending"
  | "admitted"
  | "leased"
  | "dispatched"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired";

export interface FanoutDispatchRecord {
  dispatchId: string;
  planId: string;
  parentTaskId: string;
  targetKey: string;
  agentName: string;
  promptDigest: string;
  state: FanoutDispatchState;
  priority: number;
  ordinal: number;
  workerId: string | null;
  childTaskId: string | null;
  childLeaseId: string | null;
  resultDigest: string | null;
  failureCode: string | null;
  failureMessage: string | null;
  queuedAt: string;
  admittedAt: string | null;
  leasedAt: string | null;
  dispatchedAt: string | null;
  finishedAt: string | null;
  leaseExpiresAt: string | null;
  revision: number;
  digest: string;
}

export interface FanoutDispatchProjection {
  planId: string;
  parentTaskId: string;
  total: number;
  pending: number;
  admitted: number;
  inFlight: number;
  completed: number;
  failed: number;
  cancelled: number;
  expired: number;
  activeWorkerIds: string[];
  childTaskIds: string[];
  terminal: boolean;
  digest: string;
}

function terminalFanoutDispatch(state: FanoutDispatchState): boolean {
  return ["completed", "failed", "cancelled", "expired"].includes(state);
}

function assertFanoutDispatchRecord(record: FanoutDispatchRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "fanout_dispatch_digest",
      `fanout dispatch ${record.dispatchId} digest is invalid`,
    );
  if (
    !record.dispatchId ||
    !record.planId ||
    !record.parentTaskId ||
    !record.targetKey ||
    !record.agentName ||
    !record.promptDigest
  )
    throw new E03RuntimeError(
      "fanout_dispatch_identity",
      "fanout dispatch identity is incomplete",
    );
  if (
    !Number.isSafeInteger(record.priority) ||
    !Number.isSafeInteger(record.ordinal)
  )
    throw new E03RuntimeError(
      "fanout_dispatch_order",
      "fanout dispatch order is invalid",
    );
  if (!Number.isSafeInteger(record.revision) || record.revision < 1)
    throw new E03RuntimeError(
      "fanout_dispatch_revision",
      "fanout dispatch revision is invalid",
    );
  if (record.state === "leased" && (!record.workerId || !record.leaseExpiresAt))
    throw new E03RuntimeError(
      "fanout_dispatch_lease",
      "leased fanout dispatch requires worker and expiry",
    );
  if (
    record.state === "dispatched" &&
    (!record.childTaskId || !record.childLeaseId)
  )
    throw new E03RuntimeError(
      "fanout_dispatch_child",
      "dispatched fanout requires child task identity",
    );
  if (record.state === "completed" && !record.resultDigest)
    throw new E03RuntimeError(
      "fanout_dispatch_result",
      "completed fanout dispatch requires a result digest",
    );
  if (
    record.state === "failed" &&
    (!record.failureCode || !record.failureMessage)
  )
    throw new E03RuntimeError(
      "fanout_dispatch_failure",
      "failed fanout dispatch requires failure metadata",
    );
}

export class FanoutDispatchRuntime {
  private records = new Map<string, FanoutDispatchRecord>();
  private byPlan = new Map<string, string[]>();
  private byTarget = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  enqueue(
    plan: FanoutPlan,
    priorities?: Readonly<Record<string, number>>,
  ): FanoutDispatchRecord[] {
    const prior = this.byPlan.get(plan.planId);
    if (prior)
      return prior.map((dispatchId) =>
        structuredClone(this.requireRecord(dispatchId)),
      );
    const now = this.clock.now();
    const output: FanoutDispatchRecord[] = [];
    for (let ordinal = 0; ordinal < plan.targets.length; ordinal += 1) {
      const target = plan.targets[ordinal]!;
      const payload = {
        dispatchId: createId("fanout-dispatch"),
        planId: plan.planId,
        parentTaskId: plan.parentTaskId,
        targetKey: target.key,
        agentName: target.agent,
        promptDigest: digest(target.prompt),
        state: "pending" as const,
        priority: priorities?.[target.key] ?? 0,
        ordinal,
        workerId: null,
        childTaskId: null,
        childLeaseId: null,
        resultDigest: null,
        failureCode: null,
        failureMessage: null,
        queuedAt: now,
        admittedAt: null,
        leasedAt: null,
        dispatchedAt: null,
        finishedAt: null,
        leaseExpiresAt: null,
        revision: 1,
      };
      const record = { ...payload, digest: digest(payload) };
      assertFanoutDispatchRecord(record);
      this.records.set(record.dispatchId, record);
      this.byTarget.set(`${plan.planId}\0${target.key}`, record.dispatchId);
      output.push(structuredClone(record));
    }
    this.byPlan.set(
      plan.planId,
      output.map((record) => record.dispatchId),
    );
    return output;
  }

  admit(planId: string, maximum: number): FanoutDispatchRecord[] {
    if (!Number.isSafeInteger(maximum) || maximum < 1)
      throw new E03RuntimeError(
        "fanout_dispatch_admission_limit",
        "fanout dispatch admission limit is invalid",
      );
    const records = this.recordsFor(planId);
    const active = records.filter((record) =>
      ["admitted", "leased", "dispatched"].includes(record.state),
    ).length;
    const available = Math.max(0, maximum - active);
    const candidates = records
      .filter((record) => record.state === "pending")
      .sort(
        (left, right) =>
          right.priority - left.priority ||
          left.ordinal - right.ordinal ||
          left.dispatchId.localeCompare(right.dispatchId),
      )
      .slice(0, available);
    return candidates.map((record) =>
      this.transition(record, {
        state: "admitted",
        admittedAt: this.clock.now(),
      }),
    );
  }

  lease(input: {
    dispatchId: string;
    expectedRevision: number;
    workerId: string;
    leaseExpiresAt: string;
  }): FanoutDispatchRecord {
    const record = this.requireRecord(input.dispatchId);
    this.assertRevision(record, input.expectedRevision);
    if (record.state !== "admitted")
      throw new E03RuntimeError(
        "fanout_dispatch_lease_state",
        `fanout dispatch ${record.dispatchId} is ${record.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(input.leaseExpiresAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "fanout_dispatch_lease_expiry",
        "fanout dispatch lease expiry must be in the future",
      );
    return this.transition(record, {
      state: "leased",
      workerId: input.workerId.trim(),
      leasedAt: now,
      leaseExpiresAt: input.leaseExpiresAt,
    });
  }

  dispatched(input: {
    dispatchId: string;
    expectedRevision: number;
    workerId: string;
    childTaskId: string;
    childLeaseId: string;
  }): FanoutDispatchRecord {
    const record = this.requireRecord(input.dispatchId);
    this.assertRevision(record, input.expectedRevision);
    if (record.state !== "leased" || record.workerId !== input.workerId)
      throw new E03RuntimeError(
        "fanout_dispatch_worker_custody",
        `fanout dispatch ${record.dispatchId} is not leased by ${input.workerId}`,
      );
    return this.transition(record, {
      state: "dispatched",
      childTaskId: input.childTaskId.trim(),
      childLeaseId: input.childLeaseId.trim(),
      dispatchedAt: this.clock.now(),
    });
  }

  complete(input: {
    dispatchId: string;
    expectedRevision: number;
    childTask: E03TaskState;
  }): FanoutDispatchRecord {
    const record = this.requireRecord(input.dispatchId);
    this.assertRevision(record, input.expectedRevision);
    if (
      record.state !== "dispatched" ||
      record.childTaskId !== input.childTask.identity.taskId ||
      record.childLeaseId !== input.childTask.identity.leaseId
    )
      throw new E03RuntimeError(
        "fanout_dispatch_completion_custody",
        `fanout dispatch ${record.dispatchId} child identity changed`,
      );
    if (!isTerminal(input.childTask.status))
      throw new E03RuntimeError(
        "fanout_dispatch_completion_phase",
        `fanout child ${input.childTask.identity.taskId} is ${input.childTask.status}`,
      );
    if (input.childTask.status !== "completed")
      return this.transition(record, {
        state: "failed",
        failureCode: `child_${input.childTask.status}`,
        failureMessage:
          input.childTask.error ?? `child ended ${input.childTask.status}`,
        finishedAt: this.clock.now(),
      });
    return this.transition(record, {
      state: "completed",
      resultDigest: digest(input.childTask.result ?? {}),
      finishedAt: this.clock.now(),
    });
  }

  fail(input: {
    dispatchId: string;
    expectedRevision: number;
    code: string;
    message: string;
  }): FanoutDispatchRecord {
    const record = this.requireRecord(input.dispatchId);
    this.assertRevision(record, input.expectedRevision);
    if (terminalFanoutDispatch(record.state)) return structuredClone(record);
    return this.transition(record, {
      state: "failed",
      failureCode: input.code.trim(),
      failureMessage: input.message.trim(),
      finishedAt: this.clock.now(),
    });
  }

  cancelPlan(planId: string, reason: string): FanoutDispatchRecord[] {
    return this.recordsFor(planId)
      .filter((record) => !terminalFanoutDispatch(record.state))
      .map((record) =>
        this.transition(record, {
          state: "cancelled",
          failureCode: "parent_cancelled",
          failureMessage: reason.trim() || "fanout plan cancelled",
          finishedAt: this.clock.now(),
        }),
      );
  }

  sweep(now = this.clock.now()): FanoutDispatchRecord[] {
    const expired: FanoutDispatchRecord[] = [];
    for (const record of this.records.values())
      if (
        record.state === "leased" &&
        record.leaseExpiresAt &&
        Date.parse(now) >= Date.parse(record.leaseExpiresAt)
      )
        expired.push(
          this.transition(record, {
            state: "expired",
            failureCode: "dispatch_lease_expired",
            failureMessage: "fanout dispatch lease expired",
            finishedAt: now,
          }),
        );
    return expired;
  }

  project(planId: string): FanoutDispatchProjection {
    const records = this.recordsFor(planId);
    const first = records[0];
    if (!first)
      throw new E03RuntimeError(
        "fanout_dispatch_plan_missing",
        `fanout dispatch plan ${planId} does not exist`,
      );
    const count = (states: readonly FanoutDispatchState[]): number =>
      records.filter((record) => states.includes(record.state)).length;
    const payload = {
      planId,
      parentTaskId: first.parentTaskId,
      total: records.length,
      pending: count(["pending"]),
      admitted: count(["admitted"]),
      inFlight: count(["leased", "dispatched"]),
      completed: count(["completed"]),
      failed: count(["failed"]),
      cancelled: count(["cancelled"]),
      expired: count(["expired"]),
      activeWorkerIds: [
        ...new Set(
          records
            .filter(
              (record) =>
                record.workerId && !terminalFanoutDispatch(record.state),
            )
            .map((record) => record.workerId!),
        ),
      ],
      childTaskIds: records
        .map((record) => record.childTaskId)
        .filter((value): value is string => value !== null),
      terminal: records.every((record) => terminalFanoutDispatch(record.state)),
    };
    return { ...payload, digest: digest(payload) };
  }

  snapshot(): FanoutDispatchRecord[] {
    return [...this.records.values()].map((value) => structuredClone(value));
  }

  restore(values: readonly FanoutDispatchRecord[]): void {
    const records = new Map<string, FanoutDispatchRecord>();
    const byPlan = new Map<string, string[]>();
    const byTarget = new Map<string, string>();
    for (const record of values) {
      assertFanoutDispatchRecord(record);
      const targetKey = `${record.planId}\0${record.targetKey}`;
      if (records.has(record.dispatchId) || byTarget.has(targetKey))
        throw new E03RuntimeError(
          "fanout_dispatch_restore_duplicate",
          `duplicate fanout dispatch ${record.dispatchId}`,
        );
      records.set(record.dispatchId, structuredClone(record));
      byTarget.set(targetKey, record.dispatchId);
      byPlan.set(record.planId, [
        ...(byPlan.get(record.planId) ?? []),
        record.dispatchId,
      ]);
    }
    for (const ids of byPlan.values())
      ids.sort(
        (left, right) =>
          records.get(left)!.ordinal - records.get(right)!.ordinal,
      );
    this.records = records;
    this.byPlan = byPlan;
    this.byTarget = byTarget;
  }

  private recordsFor(planId: string): FanoutDispatchRecord[] {
    return (this.byPlan.get(planId) ?? []).map((dispatchId) =>
      this.requireRecord(dispatchId),
    );
  }

  private requireRecord(dispatchId: string): FanoutDispatchRecord {
    const record = this.records.get(dispatchId);
    if (!record)
      throw new E03RuntimeError(
        "fanout_dispatch_missing",
        `fanout dispatch ${dispatchId} does not exist`,
      );
    assertFanoutDispatchRecord(record);
    return record;
  }

  private assertRevision(record: FanoutDispatchRecord, expected: number): void {
    if (record.revision !== expected)
      throw new E03RuntimeError(
        "fanout_dispatch_stale_revision",
        `fanout dispatch ${record.dispatchId} revision is stale`,
      );
  }

  private transition(
    record: FanoutDispatchRecord,
    patch: Partial<
      Omit<FanoutDispatchRecord, "dispatchId" | "revision" | "digest">
    >,
  ): FanoutDispatchRecord {
    const { digest: _, ...prior } = record;
    const payload = {
      ...prior,
      ...patch,
      dispatchId: record.dispatchId,
      revision: record.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertFanoutDispatchRecord(next);
    this.records.set(next.dispatchId, next);
    return structuredClone(next);
  }
}

export interface FanoutConsensusMember {
  taskId: string;
  weight: number;
  required: boolean;
}
export interface FanoutConsensusRound {
  roundId: string;
  planId: string;
  parentTaskId: string;
  topic: string;
  state: "open" | "decided" | "rejected" | "expired" | "cancelled";
  members: FanoutConsensusMember[];
  quorumWeight: number;
  proposalDigests: string[];
  winningDigest: string | null;
  totalAcceptedWeight: number;
  totalRejectedWeight: number;
  openedAt: string;
  decidedAt: string | null;
  expiresAt: string;
  rejectionReason: string | null;
  revision: number;
  digest: string;
}
export interface FanoutConsensusVote {
  voteId: string;
  roundId: string;
  taskId: string;
  proposalDigest: string;
  outcome: "accept" | "reject" | "abstain";
  weight: number;
  reason: string;
  votedAt: string;
  digest: string;
}
function assertConsensusRound(value: FanoutConsensusRound): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "fanout_consensus_round_digest",
      `fanout consensus round ${value.roundId} is corrupt`,
    );
  if (
    !value.roundId ||
    !value.planId ||
    !value.parentTaskId ||
    !value.topic ||
    !value.members.length ||
    value.members.some(
      (member) =>
        !member.taskId || !Number.isFinite(member.weight) || member.weight <= 0,
    ) ||
    !Number.isFinite(value.quorumWeight) ||
    value.quorumWeight <= 0 ||
    [value.totalAcceptedWeight, value.totalRejectedWeight].some(
      (number) => !Number.isFinite(number) || number < 0,
    ) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "fanout_consensus_round",
      `fanout consensus round ${value.roundId} is invalid`,
    );
  if (
    (value.state === "decided" ||
      value.state === "rejected" ||
      value.state === "expired" ||
      value.state === "cancelled") &&
    value.decidedAt === null
  )
    throw new E03RuntimeError(
      "fanout_consensus_decision_time",
      `closed consensus round ${value.roundId} lacks time`,
    );
}
function assertConsensusVote(value: FanoutConsensusVote): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "fanout_consensus_vote_digest",
      `fanout consensus vote ${value.voteId} is corrupt`,
    );
  if (
    !value.voteId ||
    !value.roundId ||
    !value.taskId ||
    !value.proposalDigest ||
    !Number.isFinite(value.weight) ||
    value.weight <= 0 ||
    !value.reason
  )
    throw new E03RuntimeError(
      "fanout_consensus_vote",
      `fanout consensus vote ${value.voteId} is invalid`,
    );
}
export interface TeamMembershipEpoch {
  epochId: string;
  teamId: string;
  generation: number;
  coordinatorId: string;
  quorum: number;
  state: "forming" | "active" | "draining" | "closed";
  memberIds: string[];
  createdAt: string;
  activatedAt: string;
  closedAt: string;
  previousEpochDigest: string;
  revision: number;
  digest: string;
}

export interface TeamMemberLease {
  leaseId: string;
  epochId: string;
  teamId: string;
  memberId: string;
  agentId: string;
  capabilities: string[];
  maximumConcurrency: number;
  activeAssignments: number;
  leaseExpiresAt: string;
  lastHeartbeatAt: string;
  state: "joining" | "active" | "draining" | "revoked" | "expired";
  revision: number;
  digest: string;
}

export interface TeamMemberAssignment {
  assignmentId: string;
  epochId: string;
  leaseId: string;
  taskId: string;
  fanoutPlanId: string;
  capability: string;
  state: "reserved" | "accepted" | "completed" | "failed" | "released";
  reservationToken: string;
  resultDigest: string;
  errorCode: string;
  reservedAt: string;
  settledAt: string;
  revision: number;
  digest: string;
}

export interface TeamMembershipSnapshot {
  epochs: TeamMembershipEpoch[];
  leases: TeamMemberLease[];
  assignments: TeamMemberAssignment[];
  activeEpochByTeam: [string, string][];
  leaseByEpochMember: [string, string][];
  assignmentByTask: [string, string][];
}

function assertTeamMembershipEpoch(value: TeamMembershipEpoch): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.epochId ||
    !value.teamId ||
    value.generation < 1 ||
    !value.coordinatorId ||
    value.quorum < 1 ||
    value.quorum > value.memberIds.length ||
    new Set(value.memberIds).size !== value.memberIds.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "team_membership_epoch_corrupt",
      `team membership epoch ${value.epochId || "<empty>"} is corrupt`,
    );
}

function assertTeamMemberLease(value: TeamMemberLease): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.leaseId ||
    !value.epochId ||
    !value.teamId ||
    !value.memberId ||
    !value.agentId ||
    value.maximumConcurrency < 1 ||
    value.activeAssignments < 0 ||
    value.activeAssignments > value.maximumConcurrency ||
    new Set(value.capabilities).size !== value.capabilities.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "team_member_lease_corrupt",
      `team member lease ${value.leaseId || "<empty>"} is corrupt`,
    );
}

function assertTeamMemberAssignment(value: TeamMemberAssignment): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.assignmentId ||
    !value.epochId ||
    !value.leaseId ||
    !value.taskId ||
    !value.fanoutPlanId ||
    !value.capability ||
    !value.reservationToken ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "team_member_assignment_corrupt",
      `team member assignment ${value.assignmentId || "<empty>"} is corrupt`,
    );
}

export class TeamMembershipRuntime {
  private epochs = new Map<string, TeamMembershipEpoch>();
  private leases = new Map<string, TeamMemberLease>();
  private assignments = new Map<string, TeamMemberAssignment>();
  private activeEpochByTeam = new Map<string, string>();
  private leaseByEpochMember = new Map<string, string>();
  private assignmentByTask = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  form(input: {
    epochId?: string;
    teamId: string;
    coordinatorId: string;
    quorum: number;
    memberIds: readonly string[];
  }): TeamMembershipEpoch {
    if (this.activeEpochByTeam.has(input.teamId))
      throw new E03RuntimeError(
        "team_membership_epoch_active",
        `team ${input.teamId} already has an active epoch`,
      );
    const memberIds = [...new Set(input.memberIds)].sort();
    if (!memberIds.includes(input.coordinatorId))
      throw new E03RuntimeError(
        "team_membership_coordinator_missing",
        "team membership coordinator must be a member",
      );
    if (input.quorum < 1 || input.quorum > memberIds.length)
      throw new E03RuntimeError(
        "team_membership_quorum",
        "team membership quorum is invalid",
      );
    const previous = [...this.epochs.values()]
      .filter((value) => value.teamId === input.teamId)
      .sort((a, b) => b.generation - a.generation)[0];
    const epochId = input.epochId ?? createId("team-membership-epoch");
    const payload = {
      epochId,
      teamId: input.teamId,
      generation: (previous?.generation ?? 0) + 1,
      coordinatorId: input.coordinatorId,
      quorum: input.quorum,
      state: "forming" as const,
      memberIds,
      createdAt: this.clock.now(),
      activatedAt: "",
      closedAt: "",
      previousEpochDigest: previous?.digest ?? "",
      revision: 1,
    };
    const epoch = { ...payload, digest: digest(payload) };
    assertTeamMembershipEpoch(epoch);
    this.epochs.set(epochId, epoch);
    this.activeEpochByTeam.set(input.teamId, epochId);
    return structuredClone(epoch);
  }

  join(input: {
    leaseId?: string;
    epochId: string;
    memberId: string;
    agentId: string;
    capabilities: readonly string[];
    maximumConcurrency: number;
    leaseMs: number;
  }): TeamMemberLease {
    const epoch = this.requireEpoch(input.epochId);
    if (epoch.state !== "forming")
      throw new E03RuntimeError(
        "team_member_join_epoch_state",
        `team membership epoch ${epoch.epochId} is ${epoch.state}`,
      );
    if (!epoch.memberIds.includes(input.memberId))
      throw new E03RuntimeError(
        "team_member_join_not_declared",
        `team member ${input.memberId} is not declared`,
      );
    const key = this.memberKey(epoch.epochId, input.memberId);
    const existingId = this.leaseByEpochMember.get(key);
    if (existingId) return structuredClone(this.requireLease(existingId));
    const capabilities = [...new Set(input.capabilities)].sort();
    if (
      !capabilities.length ||
      input.maximumConcurrency < 1 ||
      input.leaseMs < 1
    )
      throw new E03RuntimeError(
        "team_member_join_capacity",
        "team member join capacity is invalid",
      );
    const now = this.clock.now();
    const leaseId = input.leaseId ?? createId("team-member-lease");
    const payload = {
      leaseId,
      epochId: epoch.epochId,
      teamId: epoch.teamId,
      memberId: input.memberId,
      agentId: input.agentId,
      capabilities,
      maximumConcurrency: input.maximumConcurrency,
      activeAssignments: 0,
      leaseExpiresAt: new Date(Date.parse(now) + input.leaseMs).toISOString(),
      lastHeartbeatAt: now,
      state: "joining" as const,
      revision: 1,
    };
    const lease = { ...payload, digest: digest(payload) };
    assertTeamMemberLease(lease);
    this.leases.set(leaseId, lease);
    this.leaseByEpochMember.set(key, leaseId);
    return structuredClone(lease);
  }

  activate(epochId: string, expectedRevision: number): TeamMembershipEpoch {
    const epoch = this.requireEpoch(epochId);
    this.assertEpochRevision(epoch, expectedRevision);
    if (epoch.state !== "forming")
      throw new E03RuntimeError(
        "team_membership_activate_state",
        `team membership epoch ${epochId} is ${epoch.state}`,
      );
    const leases = [...this.leases.values()].filter(
      (value) => value.epochId === epochId && value.state === "joining",
    );
    if (leases.length < epoch.quorum)
      throw new E03RuntimeError(
        "team_membership_activate_quorum",
        `team membership epoch ${epochId} lacks quorum`,
      );
    for (const lease of leases)
      this.transitionLease(lease, { state: "active" });
    return this.transitionEpoch(epoch, {
      state: "active",
      activatedAt: this.clock.now(),
    });
  }

  heartbeat(
    leaseId: string,
    expectedRevision: number,
    leaseMs: number,
  ): TeamMemberLease {
    const lease = this.requireLease(leaseId);
    this.assertLeaseRevision(lease, expectedRevision);
    if (lease.state !== "active")
      throw new E03RuntimeError(
        "team_member_heartbeat_state",
        `team member lease ${leaseId} is ${lease.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(lease.leaseExpiresAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "team_member_heartbeat_expired",
        `team member lease ${leaseId} expired`,
      );
    return this.transitionLease(lease, {
      lastHeartbeatAt: now,
      leaseExpiresAt: new Date(Date.parse(now) + leaseMs).toISOString(),
    });
  }

  reserve(input: {
    assignmentId?: string;
    epochId: string;
    taskId: string;
    fanoutPlanId: string;
    capability: string;
  }): TeamMemberAssignment {
    const duplicateId = this.assignmentByTask.get(input.taskId);
    if (duplicateId)
      return structuredClone(this.requireAssignment(duplicateId));
    const epoch = this.requireEpoch(input.epochId);
    if (epoch.state !== "active")
      throw new E03RuntimeError(
        "team_assignment_epoch_state",
        `team membership epoch ${epoch.epochId} is ${epoch.state}`,
      );
    const lease = [...this.leases.values()]
      .filter(
        (value) =>
          value.epochId === epoch.epochId &&
          value.state === "active" &&
          value.capabilities.includes(input.capability) &&
          value.activeAssignments < value.maximumConcurrency,
      )
      .sort(
        (a, b) =>
          a.activeAssignments / a.maximumConcurrency -
            b.activeAssignments / b.maximumConcurrency ||
          a.memberId.localeCompare(b.memberId),
      )[0];
    if (!lease)
      throw new E03RuntimeError(
        "team_assignment_capacity_unavailable",
        `team ${epoch.teamId} has no ${input.capability} capacity`,
      );
    const assignmentId =
      input.assignmentId ?? createId("team-member-assignment");
    const payload = {
      assignmentId,
      epochId: epoch.epochId,
      leaseId: lease.leaseId,
      taskId: input.taskId,
      fanoutPlanId: input.fanoutPlanId,
      capability: input.capability,
      state: "reserved" as const,
      reservationToken: digest({
        assignmentId,
        leaseId: lease.leaseId,
        taskId: input.taskId,
        leaseRevision: lease.revision,
      }),
      resultDigest: "",
      errorCode: "",
      reservedAt: this.clock.now(),
      settledAt: "",
      revision: 1,
    };
    const assignment = { ...payload, digest: digest(payload) };
    assertTeamMemberAssignment(assignment);
    this.assignments.set(assignmentId, assignment);
    this.assignmentByTask.set(input.taskId, assignmentId);
    this.transitionLease(lease, {
      activeAssignments: lease.activeAssignments + 1,
    });
    return structuredClone(assignment);
  }

  accept(
    assignmentId: string,
    expectedRevision: number,
    reservationToken: string,
  ): TeamMemberAssignment {
    const assignment = this.requireAssignment(assignmentId);
    this.assertAssignmentRevision(assignment, expectedRevision);
    if (
      assignment.state !== "reserved" ||
      assignment.reservationToken !== reservationToken
    )
      throw new E03RuntimeError(
        "team_assignment_accept_token",
        `team assignment ${assignmentId} reservation is invalid`,
      );
    const lease = this.requireLease(assignment.leaseId);
    if (lease.state !== "active")
      throw new E03RuntimeError(
        "team_assignment_lease_inactive",
        `team assignment ${assignmentId} lease is ${lease.state}`,
      );
    return this.transitionAssignment(assignment, { state: "accepted" });
  }

  settle(
    assignmentId: string,
    expectedRevision: number,
    input: { accepted: boolean; resultDigest?: string; errorCode?: string },
  ): TeamMemberAssignment {
    const assignment = this.requireAssignment(assignmentId);
    this.assertAssignmentRevision(assignment, expectedRevision);
    if (assignment.state !== "accepted")
      throw new E03RuntimeError(
        "team_assignment_settle_state",
        `team assignment ${assignmentId} is ${assignment.state}`,
      );
    if (input.accepted && !input.resultDigest)
      throw new E03RuntimeError(
        "team_assignment_result_missing",
        `team assignment ${assignmentId} result is missing`,
      );
    const lease = this.requireLease(assignment.leaseId);
    if (lease.activeAssignments < 1)
      throw new E03RuntimeError(
        "team_assignment_capacity_underflow",
        `team member lease ${lease.leaseId} has no assignment`,
      );
    const next = this.transitionAssignment(assignment, {
      state: input.accepted ? "completed" : "failed",
      resultDigest: input.resultDigest ?? "",
      errorCode: input.errorCode ?? "",
      settledAt: this.clock.now(),
    });
    this.transitionLease(lease, {
      activeAssignments: lease.activeAssignments - 1,
    });
    return next;
  }

  expire(at = this.clock.now()): TeamMemberLease[] {
    const expired: TeamMemberLease[] = [];
    for (const lease of [...this.leases.values()]) {
      if (
        lease.state !== "active" ||
        Date.parse(lease.leaseExpiresAt) > Date.parse(at)
      )
        continue;
      const next = this.transitionLease(lease, { state: "expired" });
      expired.push(next);
      for (const assignment of [...this.assignments.values()])
        if (
          assignment.leaseId === lease.leaseId &&
          ["reserved", "accepted"].includes(assignment.state)
        )
          this.transitionAssignment(assignment, {
            state: "failed",
            errorCode: "member_lease_expired",
            settledAt: at,
          });
    }
    return expired;
  }

  drain(epochId: string, expectedRevision: number): TeamMembershipEpoch {
    const epoch = this.requireEpoch(epochId);
    this.assertEpochRevision(epoch, expectedRevision);
    if (epoch.state !== "active")
      throw new E03RuntimeError(
        "team_membership_drain_state",
        `team membership epoch ${epochId} is ${epoch.state}`,
      );
    for (const lease of [...this.leases.values()])
      if (lease.epochId === epochId && lease.state === "active")
        this.transitionLease(lease, { state: "draining" });
    return this.transitionEpoch(epoch, { state: "draining" });
  }

  close(epochId: string, expectedRevision: number): TeamMembershipEpoch {
    const epoch = this.requireEpoch(epochId);
    this.assertEpochRevision(epoch, expectedRevision);
    if (epoch.state !== "draining")
      throw new E03RuntimeError(
        "team_membership_close_state",
        `team membership epoch ${epochId} is ${epoch.state}`,
      );
    const active = [...this.assignments.values()].filter(
      (value) =>
        value.epochId === epochId &&
        ["reserved", "accepted"].includes(value.state),
    );
    if (active.length)
      throw new E03RuntimeError(
        "team_membership_close_assignments",
        `team membership epoch ${epochId} has active assignments`,
      );
    const next = this.transitionEpoch(epoch, {
      state: "closed",
      closedAt: this.clock.now(),
    });
    this.activeEpochByTeam.delete(epoch.teamId);
    return next;
  }

  snapshot(): TeamMembershipSnapshot {
    return {
      epochs: [...this.epochs.values()].map((value) => structuredClone(value)),
      leases: [...this.leases.values()].map((value) => structuredClone(value)),
      assignments: [...this.assignments.values()].map((value) =>
        structuredClone(value),
      ),
      activeEpochByTeam: [...this.activeEpochByTeam.entries()],
      leaseByEpochMember: [...this.leaseByEpochMember.entries()],
      assignmentByTask: [...this.assignmentByTask.entries()],
    };
  }

  restore(snapshot: TeamMembershipSnapshot): void {
    const epochs = new Map<string, TeamMembershipEpoch>();
    const leases = new Map<string, TeamMemberLease>();
    const assignments = new Map<string, TeamMemberAssignment>();
    for (const value of snapshot.epochs) {
      assertTeamMembershipEpoch(value);
      if (epochs.has(value.epochId))
        throw new E03RuntimeError(
          "team_membership_restore_duplicate",
          `duplicate epoch ${value.epochId}`,
        );
      epochs.set(value.epochId, structuredClone(value));
    }
    for (const value of snapshot.leases) {
      assertTeamMemberLease(value);
      const epoch = epochs.get(value.epochId);
      if (
        !epoch ||
        epoch.teamId !== value.teamId ||
        !epoch.memberIds.includes(value.memberId)
      )
        throw new E03RuntimeError(
          "team_member_restore_epoch",
          `lease ${value.leaseId} has invalid epoch`,
        );
      leases.set(value.leaseId, structuredClone(value));
    }
    for (const value of snapshot.assignments) {
      assertTeamMemberAssignment(value);
      const lease = leases.get(value.leaseId);
      if (!lease || lease.epochId !== value.epochId)
        throw new E03RuntimeError(
          "team_assignment_restore_lease",
          `assignment ${value.assignmentId} has invalid lease`,
        );
      assignments.set(value.assignmentId, structuredClone(value));
    }
    const activeEpochByTeam = new Map(snapshot.activeEpochByTeam);
    const leaseByEpochMember = new Map(snapshot.leaseByEpochMember);
    const assignmentByTask = new Map(snapshot.assignmentByTask);
    if (
      activeEpochByTeam.size !== snapshot.activeEpochByTeam.length ||
      leaseByEpochMember.size !== snapshot.leaseByEpochMember.length ||
      assignmentByTask.size !== snapshot.assignmentByTask.length
    )
      throw new E03RuntimeError(
        "team_membership_restore_index_duplicate",
        "team membership indexes duplicate",
      );
    for (const [teamId, epochId] of activeEpochByTeam) {
      const epoch = epochs.get(epochId);
      if (!epoch || epoch.teamId !== teamId || epoch.state === "closed")
        throw new E03RuntimeError(
          "team_membership_restore_active",
          `team epoch index ${teamId} invalid`,
        );
    }
    for (const [key, leaseId] of leaseByEpochMember) {
      const lease = leases.get(leaseId);
      if (!lease || key !== this.memberKey(lease.epochId, lease.memberId))
        throw new E03RuntimeError(
          "team_membership_restore_lease_index",
          `team lease index ${key} invalid`,
        );
    }
    for (const [taskId, assignmentId] of assignmentByTask) {
      const assignment = assignments.get(assignmentId);
      if (!assignment || assignment.taskId !== taskId)
        throw new E03RuntimeError(
          "team_membership_restore_assignment_index",
          `team task index ${taskId} invalid`,
        );
    }
    for (const lease of leases.values()) {
      const active = [...assignments.values()].filter(
        (value) =>
          value.leaseId === lease.leaseId &&
          ["reserved", "accepted"].includes(value.state),
      ).length;
      if (active !== lease.activeAssignments)
        throw new E03RuntimeError(
          "team_member_restore_capacity",
          `lease ${lease.leaseId} capacity invalid`,
        );
    }
    this.epochs = epochs;
    this.leases = leases;
    this.assignments = assignments;
    this.activeEpochByTeam = activeEpochByTeam;
    this.leaseByEpochMember = leaseByEpochMember;
    this.assignmentByTask = assignmentByTask;
  }

  private memberKey(epochId: string, memberId: string): string {
    return `${epochId}\u0000${memberId}`;
  }

  private requireEpoch(id: string): TeamMembershipEpoch {
    const value = this.epochs.get(id);
    if (!value)
      throw new E03RuntimeError(
        "team_membership_epoch_missing",
        `epoch ${id} does not exist`,
      );
    assertTeamMembershipEpoch(value);
    return value;
  }

  private requireLease(id: string): TeamMemberLease {
    const value = this.leases.get(id);
    if (!value)
      throw new E03RuntimeError(
        "team_member_lease_missing",
        `lease ${id} does not exist`,
      );
    assertTeamMemberLease(value);
    return value;
  }

  private requireAssignment(id: string): TeamMemberAssignment {
    const value = this.assignments.get(id);
    if (!value)
      throw new E03RuntimeError(
        "team_member_assignment_missing",
        `assignment ${id} does not exist`,
      );
    assertTeamMemberAssignment(value);
    return value;
  }

  private assertEpochRevision(
    value: TeamMembershipEpoch,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "team_membership_epoch_stale_revision",
        `epoch ${value.epochId} is stale`,
      );
  }

  private assertLeaseRevision(value: TeamMemberLease, expected: number): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "team_member_lease_stale_revision",
        `lease ${value.leaseId} is stale`,
      );
  }

  private assertAssignmentRevision(
    value: TeamMemberAssignment,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "team_assignment_stale_revision",
        `assignment ${value.assignmentId} is stale`,
      );
  }

  private transitionEpoch(
    value: TeamMembershipEpoch,
    patch: Partial<
      Omit<TeamMembershipEpoch, "epochId" | "revision" | "digest">
    >,
  ): TeamMembershipEpoch {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      epochId: value.epochId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTeamMembershipEpoch(next);
    this.epochs.set(next.epochId, next);
    return structuredClone(next);
  }

  private transitionLease(
    value: TeamMemberLease,
    patch: Partial<Omit<TeamMemberLease, "leaseId" | "revision" | "digest">>,
  ): TeamMemberLease {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      leaseId: value.leaseId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTeamMemberLease(next);
    this.leases.set(next.leaseId, next);
    return structuredClone(next);
  }

  private transitionAssignment(
    value: TeamMemberAssignment,
    patch: Partial<
      Omit<TeamMemberAssignment, "assignmentId" | "revision" | "digest">
    >,
  ): TeamMemberAssignment {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      assignmentId: value.assignmentId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTeamMemberAssignment(next);
    this.assignments.set(next.assignmentId, next);
    return structuredClone(next);
  }
}

export class FanoutConsensusRuntime {
  private rounds = new Map<string, FanoutConsensusRound>();
  private votes = new Map<string, FanoutConsensusVote[]>();
  private activeByPlanTopic = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  open(input: {
    plan: FanoutPlan;
    parentTaskId: string;
    topic: string;
    members: readonly FanoutConsensusMember[];
    quorumWeight?: number;
    ttlMs: number;
  }): FanoutConsensusRound {
    if (
      input.plan.parentTaskId !== input.parentTaskId ||
      !input.topic.trim() ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "fanout_consensus_open",
        "fanout consensus open input is invalid",
      );
    const members = [...input.members].sort((left, right) =>
      left.taskId.localeCompare(right.taskId),
    );
    if (
      !members.length ||
      new Set(members.map((member) => member.taskId)).size !== members.length
    )
      throw new E03RuntimeError(
        "fanout_consensus_members",
        "fanout consensus members are invalid",
      );
    const totalWeight = members.reduce((sum, member) => sum + member.weight, 0);
    const quorumWeight = input.quorumWeight ?? totalWeight / 2 + Number.EPSILON;
    if (
      !Number.isFinite(quorumWeight) ||
      quorumWeight <= 0 ||
      quorumWeight > totalWeight
    )
      throw new E03RuntimeError(
        "fanout_consensus_quorum",
        "fanout consensus quorum is invalid",
      );
    const key = `${input.plan.planId}:${input.topic.trim()}`;
    const activeId = this.activeByPlanTopic.get(key);
    if (activeId) return structuredClone(this.requireRound(activeId));
    const payload = {
      roundId: createId("fanout-consensus-round"),
      planId: input.plan.planId,
      parentTaskId: input.parentTaskId,
      topic: input.topic.trim(),
      state: "open" as const,
      members: structuredClone(members),
      quorumWeight,
      proposalDigests: [],
      winningDigest: null,
      totalAcceptedWeight: 0,
      totalRejectedWeight: 0,
      openedAt: this.clock.now(),
      decidedAt: null,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      rejectionReason: null,
      revision: 1,
    };
    const round = { ...payload, digest: digest(payload) };
    assertConsensusRound(round);
    this.rounds.set(round.roundId, round);
    this.activeByPlanTopic.set(key, round.roundId);
    return structuredClone(round);
  }
  vote(input: {
    roundId: string;
    expectedRevision: number;
    taskId: string;
    proposalDigest: string;
    outcome: FanoutConsensusVote["outcome"];
    reason: string;
  }): { round: FanoutConsensusRound; vote: FanoutConsensusVote } {
    const round = this.requireRound(input.roundId);
    this.assertRoundRevision(round, input.expectedRevision);
    if (round.state !== "open")
      throw new E03RuntimeError(
        "fanout_consensus_vote_state",
        `fanout consensus round ${round.roundId} is ${round.state}`,
      );
    if (Date.parse(round.expiresAt) <= Date.parse(this.clock.now()))
      return {
        round: this.expire(round.roundId, round.revision),
        vote: this.syntheticVote(round, input, 0, "abstain", "round expired"),
      };
    const member = round.members.find(
      (candidate) => candidate.taskId === input.taskId,
    );
    if (!member)
      throw new E03RuntimeError(
        "fanout_consensus_voter",
        `task ${input.taskId} is not a consensus member`,
      );
    if (!input.proposalDigest || !input.reason.trim())
      throw new E03RuntimeError(
        "fanout_consensus_vote_input",
        "fanout consensus vote input is invalid",
      );
    const entries = this.votes.get(round.roundId) ?? [];
    const prior = entries.find((value) => value.taskId === input.taskId);
    if (prior) {
      if (
        prior.proposalDigest !== input.proposalDigest ||
        prior.outcome !== input.outcome
      )
        throw new E03RuntimeError(
          "fanout_consensus_vote_conflict",
          `task ${input.taskId} already voted`,
        );
      return { round: structuredClone(round), vote: structuredClone(prior) };
    }
    const vote = this.syntheticVote(
      round,
      input,
      member.weight,
      input.outcome,
      input.reason.trim(),
    );
    entries.push(vote);
    this.votes.set(round.roundId, entries);
    const proposalDigests = [
      ...new Set([...round.proposalDigests, input.proposalDigest]),
    ].sort();
    const tallies = new Map<string, number>();
    let totalRejectedWeight = 0;
    for (const candidate of entries) {
      if (candidate.outcome === "accept")
        tallies.set(
          candidate.proposalDigest,
          (tallies.get(candidate.proposalDigest) ?? 0) + candidate.weight,
        );
      if (candidate.outcome === "reject")
        totalRejectedWeight += candidate.weight;
    }
    const winner = [...tallies.entries()].sort(
      (left, right) => right[1] - left[1] || left[0].localeCompare(right[0]),
    )[0];
    const requiredDenied = entries.some(
      (candidate) =>
        candidate.outcome === "reject" &&
        round.members.find(
          (memberValue) => memberValue.taskId === candidate.taskId,
        )?.required,
    );
    const remainingWeight = round.members
      .filter(
        (memberValue) =>
          !entries.some((candidate) => candidate.taskId === memberValue.taskId),
      )
      .reduce((sum, memberValue) => sum + memberValue.weight, 0);
    const canReach = (winner?.[1] ?? 0) + remainingWeight >= round.quorumWeight;
    const decided = !requiredDenied && (winner?.[1] ?? 0) >= round.quorumWeight;
    const rejected = requiredDenied || !canReach;
    const next = this.transitionRound(round, {
      state: decided ? "decided" : rejected ? "rejected" : "open",
      proposalDigests,
      winningDigest: decided ? winner![0] : null,
      totalAcceptedWeight: winner?.[1] ?? 0,
      totalRejectedWeight,
      decidedAt: decided || rejected ? this.clock.now() : null,
      rejectionReason: rejected
        ? requiredDenied
          ? "required member rejected proposal"
          : "quorum is unreachable"
        : null,
    });
    if (next.state !== "open")
      this.activeByPlanTopic.delete(`${next.planId}:${next.topic}`);
    return { round: next, vote: structuredClone(vote) };
  }
  expire(roundId: string, expectedRevision: number): FanoutConsensusRound {
    const round = this.requireRound(roundId);
    this.assertRoundRevision(round, expectedRevision);
    if (round.state !== "open") return structuredClone(round);
    const next = this.transitionRound(round, {
      state: "expired",
      decidedAt: this.clock.now(),
      rejectionReason: "consensus round expired",
    });
    this.activeByPlanTopic.delete(`${next.planId}:${next.topic}`);
    return next;
  }
  cancel(
    roundId: string,
    expectedRevision: number,
    reason: string,
  ): FanoutConsensusRound {
    const round = this.requireRound(roundId);
    this.assertRoundRevision(round, expectedRevision);
    if (round.state !== "open") return structuredClone(round);
    if (!reason.trim())
      throw new E03RuntimeError(
        "fanout_consensus_cancel_reason",
        "fanout consensus cancellation reason is required",
      );
    const next = this.transitionRound(round, {
      state: "cancelled",
      decidedAt: this.clock.now(),
      rejectionReason: reason.trim(),
    });
    this.activeByPlanTopic.delete(`${next.planId}:${next.topic}`);
    return next;
  }
  projection(roundId: string): {
    round: FanoutConsensusRound;
    votes: FanoutConsensusVote[];
    outstanding: string[];
    tallies: Record<string, number>;
  } {
    const round = this.requireRound(roundId);
    const votes = (this.votes.get(roundId) ?? []).map((value) =>
      structuredClone(value),
    );
    const tallies: Record<string, number> = {};
    for (const vote of votes)
      if (vote.outcome === "accept")
        tallies[vote.proposalDigest] =
          (tallies[vote.proposalDigest] ?? 0) + vote.weight;
    return {
      round: structuredClone(round),
      votes,
      outstanding: round.members
        .filter(
          (member) => !votes.some((vote) => vote.taskId === member.taskId),
        )
        .map((member) => member.taskId),
      tallies,
    };
  }
  snapshot(): {
    rounds: FanoutConsensusRound[];
    votes: FanoutConsensusVote[];
    activeByPlanTopic: Array<[string, string]>;
  } {
    return {
      rounds: [...this.rounds.values()].map((value) => structuredClone(value)),
      votes: [...this.votes.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeByPlanTopic: [...this.activeByPlanTopic.entries()].map(
        ([key, id]) => [key, id],
      ),
    };
  }
  restore(snapshot: {
    rounds: readonly FanoutConsensusRound[];
    votes: readonly FanoutConsensusVote[];
    activeByPlanTopic: ReadonlyArray<readonly [string, string]>;
  }): void {
    const rounds = new Map<string, FanoutConsensusRound>();
    const votes = new Map<string, FanoutConsensusVote[]>();
    const activeByPlanTopic = new Map<string, string>();
    for (const value of snapshot.rounds) {
      assertConsensusRound(value);
      if (rounds.has(value.roundId))
        throw new E03RuntimeError(
          "fanout_consensus_round_restore_duplicate",
          `duplicate fanout consensus round ${value.roundId}`,
        );
      rounds.set(value.roundId, structuredClone(value));
    }
    for (const value of snapshot.votes) {
      assertConsensusVote(value);
      const round = rounds.get(value.roundId);
      if (
        !round ||
        !round.members.some(
          (member) =>
            member.taskId === value.taskId && member.weight === value.weight,
        )
      )
        throw new E03RuntimeError(
          "fanout_consensus_vote_restore",
          `fanout consensus vote ${value.voteId} is invalid`,
        );
      const entries = votes.get(value.roundId) ?? [];
      if (entries.some((entry) => entry.taskId === value.taskId))
        throw new E03RuntimeError(
          "fanout_consensus_vote_restore_duplicate",
          `duplicate fanout consensus vote from ${value.taskId}`,
        );
      entries.push(structuredClone(value));
      votes.set(value.roundId, entries);
    }
    for (const [key, id] of snapshot.activeByPlanTopic) {
      const round = rounds.get(id);
      if (
        !round ||
        round.state !== "open" ||
        key !== `${round.planId}:${round.topic}` ||
        activeByPlanTopic.has(key)
      )
        throw new E03RuntimeError(
          "fanout_consensus_active_restore",
          `fanout consensus active index ${key} is invalid`,
        );
      activeByPlanTopic.set(key, id);
    }
    this.rounds = rounds;
    this.votes = votes;
    this.activeByPlanTopic = activeByPlanTopic;
  }
  private syntheticVote(
    round: FanoutConsensusRound,
    input: { taskId: string; proposalDigest: string },
    weight: number,
    outcome: FanoutConsensusVote["outcome"],
    reason: string,
  ): FanoutConsensusVote {
    const payload = {
      voteId: createId("fanout-consensus-vote"),
      roundId: round.roundId,
      taskId: input.taskId,
      proposalDigest: input.proposalDigest,
      outcome,
      weight: Math.max(weight, Number.EPSILON),
      reason,
      votedAt: this.clock.now(),
    };
    const vote = { ...payload, digest: digest(payload) };
    assertConsensusVote(vote);
    return vote;
  }
  private requireRound(id: string): FanoutConsensusRound {
    const value = this.rounds.get(id);
    if (!value)
      throw new E03RuntimeError(
        "fanout_consensus_round_missing",
        `fanout consensus round ${id} does not exist`,
      );
    assertConsensusRound(value);
    return value;
  }
  private assertRoundRevision(
    value: FanoutConsensusRound,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "fanout_consensus_round_stale_revision",
        `fanout consensus round ${value.roundId} revision is stale`,
      );
  }
  private transitionRound(
    value: FanoutConsensusRound,
    patch: Partial<
      Omit<FanoutConsensusRound, "roundId" | "revision" | "digest">
    >,
  ): FanoutConsensusRound {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      roundId: value.roundId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertConsensusRound(next);
    this.rounds.set(next.roundId, next);
    return structuredClone(next);
  }
}
