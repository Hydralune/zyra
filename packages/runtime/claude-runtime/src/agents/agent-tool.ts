import {
  asBoolean,
  asObject,
  asString,
  type AgentMutationReceipt,
  type ArtifactReceipt,
  type JsonObject,
  type RuntimeRunInput,
  type ToolSpecContract,
} from "../contracts.ts";
import type {
  AgentBudget,
  AgentCapabilityResult,
  AgentContextSnapshot,
  AgentDefinition,
  AgentExecutionContext,
  AgentScope,
  AgentTask,
  AgentTaskStatus,
  AgentToolResult,
  AgentToolSurface,
} from "./contracts.ts";
import { AgentDefinitionRegistry, budgetPayload } from "./definitions.ts";
import { forkAgentContext } from "./fork.ts";
import { AgentLifecycle } from "./lifecycle.ts";
import { canonicalDigest } from "./memory.ts";
import { resumeArguments } from "./resume.ts";
import { runAgent } from "./run-agent.ts";
import { deriveAgentScope } from "./scope.ts";

const AGENT_TOOLS = new Set([
  "Agent",
  "Task",
  "agent_status",
  "agent_cancel",
  "agent_resume",
  "agent_message",
]);

type BackgroundWork = {
  taskId: string;
  argumentsValue: JsonObject;
  context: AgentExecutionContext;
};

export class TypeScriptAgentRuntime implements AgentToolSurface {
  private readonly definitions: AgentDefinitionRegistry;
  private readonly lifecycle = new AgentLifecycle();
  private readonly queuedBackground = new Map<string, BackgroundWork>();

  constructor(input: RuntimeRunInput) {
    this.definitions = AgentDefinitionRegistry.fromInput(input);
  }

  toolSpecs(): ToolSpecContract[] {
    const invocationSchema: JsonObject = {
      type: "object",
      required: ["prompt"],
      properties: {
        prompt: { type: "string", minLength: 1 },
        agent_type: { type: "string" },
        task_id: { type: "string" },
        idempotency_key: { type: "string" },
        background: { type: "boolean" },
        context_mode: { type: "string", enum: ["isolated", "fork", "resume"] },
        tools: { type: "array", items: { type: "string" } },
        required_tools: { type: "array", items: { type: "string" } },
        permission_mode: { type: "string" },
        turns: { type: "array" },
        requests: { type: "array" },
      },
      additionalProperties: true,
    };
    const controlSchema: JsonObject = {
      type: "object",
      required: ["task_id"],
      properties: {
        task_id: { type: "string" },
        expected_revision: { type: "integer", minimum: 0 },
        resume_correlation_id: { type: "string" },
        message: { type: "string" },
      },
      additionalProperties: true,
    };
    return [
      spec("Agent", "Create or fan out a logical TypeScript child CodeWorker.", invocationSchema),
      spec("Task", "Alias for Agent with identical ownership and lifecycle.", invocationSchema),
      spec("agent_status", "Read canonical child status.", controlSchema, true),
      spec("agent_cancel", "Cancel a child and fence late results.", controlSchema),
      spec("agent_resume", "Resume a child from exact correlated state.", controlSchema),
      spec("agent_message", "Queue an idempotent inbox message for a child.", controlSchema),
    ];
  }

  owns(toolName: string): boolean {
    return AGENT_TOOLS.has(toolName);
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    if (toolName === "Agent" || toolName === "Task") {
      const requests = Array.isArray(argumentsValue.requests)
        ? argumentsValue.requests.map((item) => asObject(item))
        : [];
      return requests.length > 0
        ? this.fanout(argumentsValue, requests, context)
        : this.spawn(argumentsValue, context);
    }
    const taskId = asString(argumentsValue.task_id);
    if (!taskId) {
      throw new Error(toolName + " requires task_id");
    }
    if (toolName === "agent_status") {
      return this.status(taskId, context);
    }
    if (toolName === "agent_cancel") {
      return this.cancel(taskId, argumentsValue, context);
    }
    if (toolName === "agent_resume") {
      return this.resume(taskId, argumentsValue, context);
    }
    if (toolName === "agent_message") {
      return this.message(taskId, argumentsValue, context);
    }
    throw new Error("unsupported agent tool: " + toolName);
  }

  async drainBackground(context: AgentExecutionContext): Promise<void> {
    for (const [taskId, queued] of [...this.queuedBackground.entries()]) {
      const task = this.lifecycle.require(taskId);
      if (task.status !== "cancelled") {
        await this.executeChild(task, queued.argumentsValue, queued.context ?? context);
      }
      this.queuedBackground.delete(taskId);
    }
  }

  snapshot(): JsonObject {
    return {
      version: "zyra.typescript-agent-runtime.v1",
      canonical_logical_owner: "typescript",
      durable_task_owner: "python-subagent-task-store-port",
      physical_owner: "zyra-workspace-scheduler-port",
      definition_registry: this.definitions.snapshot(),
      tasks: this.lifecycle.snapshot() as JsonObject[],
      queued_background_task_ids: [...this.queuedBackground.keys()].sort(),
      python_agent_fallback: false,
    };
  }

  private async spawn(
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    const prompt = asString(argumentsValue.prompt).trim();
    if (!prompt) {
      throw new Error("Agent requires a non-empty prompt");
    }
    const definition = this.definitions.get(asString(argumentsValue.agent_type, "general-purpose"));
    const scope = deriveAgentScope(context.parentInput, definition, argumentsValue);
    const idempotencyKey = asString(argumentsValue.idempotency_key)
      || canonicalDigest([
        context.parentInput.taskId,
        context.parentInput.sessionId,
        definition.digest,
        canonicalDigest(prompt),
      ]);
    const taskId = asString(argumentsValue.task_id)
      || "agenttask_" + canonicalDigest(idempotencyKey).slice(7, 31);
    const replay = await this.tryLoadTask(taskId, context);
    if (replay) {
      if (
        replay.idempotencyKey !== idempotencyKey
        || replay.promptDigest !== canonicalDigest(prompt)
        || replay.definition.digest !== definition.digest
      ) {
        throw new Error("agent task/idempotency identity was reused with different input");
      }
      return this.resultCapability(replay.result ?? queuedResult(replay));
    }
    const durableChildren = await this.durableTasks(context);
    const activeChildIds = new Set([
      ...this.lifecycle.list(context.parentInput.taskId)
        .filter((task) => !["completed", "failed", "cancelled"].includes(task.status))
        .map((task) => task.taskId),
      ...durableChildren
        .filter((task) => !["completed", "failed", "cancelled"].includes(asString(task.status)))
        .map((task) => asString(task.task_id))
        .filter(Boolean),
    ]);
    if (activeChildIds.size >= scope.budget.maxChildren) {
      throw new Error("agent child-count budget exceeded");
    }
    const now = new Date().toISOString();
    const executionMode = (
      asBoolean(argumentsValue.background, definition.background)
      ? "background"
      : "foreground"
    ) as "background" | "foreground";
    const task: AgentTask = {
      taskId,
      parentTaskId: context.parentInput.taskId,
      parentSessionId: context.parentInput.sessionId,
      childSessionId: "agentsession_" + canonicalDigest([context.parentInput.sessionId, taskId]).slice(7, 31),
      runId: context.parentInput.runId,
      definition,
      promptDigest: canonicalDigest(prompt),
      executionMode,
      status: "created",
      revision: 0,
      attempt: 1,
      sequence: 0,
      scope,
      context: forkAgentContext(context.parentInput, taskId, scope, argumentsValue),
      idempotencyKey,
      resultDigest: "",
      result: null,
      error: "",
      createdAt: now,
      updatedAt: now,
    };
    const created = this.lifecycle.create(task);
    if (created !== task) {
      return this.resultCapability(created.result ?? queuedResult(created));
    }
    await this.persist(context, "create", task, {
      idempotency_key: idempotencyKey,
      record: durableRecord(task, prompt, argumentsValue, context.parentInput),
    });
    await this.persist(context, "dispatch", task, {
      expected_revision: task.revision,
      execution_ref: "typescript-query-engine:" + task.taskId + ":" + String(task.attempt),
      dispatch_request: {
        agent_definition_digest: definition.digest,
        tool_catalog_digest: scope.toolCatalogDigest,
        permission_ceiling_digest: scope.permissionCeilingDigest,
        context_snapshot_ref: task.context.snapshotId,
        budget: budgetPayload(scope.budget),
        depth: scope.depth,
        cycle_lineage: scope.lineage,
        model_reference: definition.model,
        workspace_isolation_request: definition.isolation,
        idempotency_key: idempotencyKey,
      },
    });
    if (executionMode === "background") {
      this.queuedBackground.set(task.taskId, { taskId: task.taskId, argumentsValue, context });
      return this.resultCapability(queuedResult(task));
    }
    return this.resultCapability(await this.executeChild(task, argumentsValue, context));
  }

  private async fanout(
    root: JsonObject,
    requests: JsonObject[],
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    const limit = integer(asObject(root.budget).max_children, requests.length);
    if (requests.length > Math.max(1, limit)) {
      throw new Error("agent fanout exceeds requested child budget");
    }
    const values = await Promise.all(requests.map(async (request, index) => {
      const merged: JsonObject = {
        ...root,
        ...request,
        requests: [],
        idempotency_key: asString(request.idempotency_key)
          || canonicalDigest([asString(root.idempotency_key, context.parentInput.workerRequestId), index]),
      };
      try {
        const result = await this.spawn(merged, context);
        return { index, ok: true, result: result.output };
      } catch (error) {
        return {
          index,
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        };
      }
    }));
    values.sort((left, right) => left.index - right.index);
    const failed = values.filter((item) => !item.ok);
    const allowPartial = asString(root.failure_policy, "collect") === "collect";
    return {
      summary: failed.length === 0
        ? "Agent fanout completed."
        : "Agent fanout completed with " + String(failed.length) + " failure(s).",
      output: {
        ok: failed.length === 0 || allowPartial,
        partial: failed.length > 0,
        results: values as unknown as JsonObject[],
        stable_order: true,
        fanin_owner: "typescript-agent-runtime",
      },
      metadata: {
        canonical_agent_owner: "typescript",
        fanout_count: String(values.length),
        failure_count: String(failed.length),
      },
    };
  }

  private async executeChild(
    task: AgentTask,
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentToolResult> {
    if (task.status === "cancelled") {
      return cancelledResult(task);
    }
    await this.persist(context, "running", task, {
      expected_revision: task.revision,
      attempt: task.attempt,
    });
    const started = Date.now();
    let result: AgentToolResult;
    try {
      const child = await runAgent(task, argumentsValue, context);
      const summary = child.stepSummaries.join("\n")
        || (child.ok ? "Child QueryEngine completed." : child.stoppedReason || "Child failed.");
      result = {
        ok: child.ok,
        taskId: task.taskId,
        status: child.ok ? "completed" : "failed",
        summary,
        artifacts: child.artifacts,
        usage: {
          turns: child.turnCount,
          tool_calls: child.toolCallCount,
          input_tokens: 0,
          output_tokens: 0,
          result_chars: summary.length,
          wall_time_ms: Date.now() - started,
          child_count: 0,
        },
        error: child.stoppedReason ?? "",
        metadata: {
          canonical_agent_owner: "typescript",
          child_session_id: task.childSessionId,
          child_runtime_owner: "zyra-typescript-claude-runtime",
          session_snapshot: child.sessionSnapshot,
          result_pending_before_commit: true,
        },
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      result = {
        ok: false,
        taskId: task.taskId,
        status: "failed",
        summary: message,
        artifacts: [],
        usage: {
          turns: 0,
          tool_calls: 0,
          input_tokens: 0,
          output_tokens: 0,
          result_chars: message.length,
          wall_time_ms: Date.now() - started,
          child_count: 0,
        },
        error: message,
        metadata: {
          canonical_agent_owner: "typescript",
          child_session_id: task.childSessionId,
          recoverable: true,
          result_pending_before_commit: true,
        },
      };
    }
    if (task.status === "cancelled") {
      throw new Error("late child result rejected after cancellation");
    }
    const digest = canonicalDigest({
      ok: result.ok,
      summary: result.summary,
      artifacts: result.artifacts,
      usage: result.usage,
      error: result.error,
    });
    this.lifecycle.commitResult(task.taskId, result, digest);
    await this.persist(context, result.ok ? "complete" : "fail", task, {
      expected_revision: task.revision,
      result_digest: digest,
      result: {
        ...result,
        metadata: {
          ...result.metadata,
          result_pending_before_commit: false,
          idempotency_fence: task.idempotencyKey,
        },
      },
    });
    result.status = task.status;
    result.metadata = {
      ...result.metadata,
      durable_revision: task.revision,
      result_digest: digest,
      result_pending_before_commit: false,
    };
    return result;
  }

  private async status(taskId: string, context: AgentExecutionContext): Promise<AgentCapabilityResult> {
    const task = await this.requireTask(taskId, context);
    return {
      summary: "Agent task " + taskId + " is " + task.status + ".",
      output: taskOutput(task),
      metadata: {
        canonical_agent_owner: "typescript",
        task_status: task.status,
        task_revision: String(task.revision),
      },
    };
  }

  private async cancel(
    taskId: string,
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    const task = await this.requireTask(taskId, context);
    const expected = integer(argumentsValue.expected_revision, task.revision);
    if (expected !== task.revision) {
      throw new Error("stale agent cancel revision");
    }
    await this.persist(context, "cancel", task, {
      expected_revision: expected,
      reason: asString(argumentsValue.reason, "agent_cancel"),
    });
    this.queuedBackground.delete(taskId);
    return this.status(taskId, context);
  }

  private async resume(
    taskId: string,
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    const task = await this.requireTask(taskId, context);
    if (!["waiting", "failed", "completed"].includes(task.status)) {
      throw new Error("agent task cannot resume from " + task.status);
    }
    const expected = integer(argumentsValue.expected_revision, task.revision);
    if (expected !== task.revision) {
      throw new Error("stale agent resume revision");
    }
    const resumed = resumeArguments(task, argumentsValue);
    await this.persist(context, "resume", task, {
      expected_revision: expected,
      resume_correlation_id: asString(resumed.resume_correlation_id),
    });
    task.attempt += 1;
    await this.persist(context, "dispatch", task, {
      expected_revision: task.revision,
      execution_ref: "typescript-query-engine:" + task.taskId + ":" + String(task.attempt),
      dispatch_request: {
        resume_correlation_id: asString(resumed.resume_correlation_id),
        attempt: task.attempt,
      },
    });
    return this.resultCapability(await this.executeChild(task, resumed, context));
  }

  private async message(
    taskId: string,
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    const task = await this.requireTask(taskId, context);
    const message = asString(argumentsValue.message).trim();
    if (!message) {
      throw new Error("agent_message requires message");
    }
    const receipt = await context.host.mutateAgent?.({
      action: "message",
      task_id: task.taskId,
      expected_revision: task.revision,
      message: {
        message_id: asString(argumentsValue.message_id)
          || "agentmsg_" + canonicalDigest([task.taskId, message]).slice(7, 31),
        sender_task_id: context.parentInput.taskId,
        target_task_id: task.taskId,
        intent: asString(argumentsValue.intent, "message"),
        summary: message,
        state_delta: asObject(argumentsValue.state_delta),
        evidence_refs: [],
        artifact_refs: [],
        metadata: { canonical_message_owner: "typescript-agent-runtime" },
      },
    });
    if (!receipt?.accepted) {
      throw new Error(asString(receipt?.error, "agent durable message rejected"));
    }
    this.lifecycle.acceptDurable(
      task.taskId,
      asString(receipt.status, task.status) as AgentTaskStatus,
      Number(receipt.revision),
    );
    return this.status(taskId, context);
  }

  private async durableTasks(context: AgentExecutionContext): Promise<JsonObject[]> {
    if (!context.host.mutateAgent) {
      throw new Error("agent durable/physical host port is unavailable");
    }
    const receipt = await context.host.mutateAgent({
      action: "list",
      task_id: context.parentInput.taskId,
      parent_task_id: context.parentInput.taskId,
      parent_session_id: context.parentInput.sessionId,
      run_id: context.parentInput.runId,
    });
    if (!receipt.accepted) {
      throw new Error(asString(receipt.error, "agent durable list rejected"));
    }
    return Array.isArray(receipt.tasks) ? receipt.tasks.map((item) => asObject(item)) : [];
  }

  private async requireTask(taskId: string, context: AgentExecutionContext): Promise<AgentTask> {
    return this.lifecycle.get(taskId) ?? await this.loadTask(taskId, context);
  }

  private async tryLoadTask(
    taskId: string,
    context: AgentExecutionContext,
  ): Promise<AgentTask | null> {
    try {
      return await this.loadTask(taskId, context);
    } catch (error) {
      if (error instanceof Error && error.message === "agent_task_not_found") {
        return null;
      }
      throw error;
    }
  }

  private async loadTask(taskId: string, context: AgentExecutionContext): Promise<AgentTask> {
    if (!context.host.mutateAgent) {
      throw new Error("agent durable/physical host port is unavailable");
    }
    const receipt = await context.host.mutateAgent({
      action: "load",
      task_id: taskId,
      parent_task_id: context.parentInput.taskId,
      parent_session_id: context.parentInput.sessionId,
      run_id: context.parentInput.runId,
    });
    if (!receipt.accepted) {
      if (asString(receipt.error_code) === "agent_task_not_found") {
        throw new Error("agent_task_not_found");
      }
      throw new Error(asString(receipt.error, "agent durable load rejected"));
    }
    const record = asObject(receipt.record);
    const definition = this.definitions.get(asString(record.agent_type, "general-purpose"));
    if (asString(record.definition_id) !== definition.digest) {
      throw new Error("durable agent definition digest no longer matches the active registry");
    }
    return this.lifecycle.hydrate(taskFromDurable(record, definition));
  }

  private async persist(
    context: AgentExecutionContext,
    action: string,
    task: AgentTask,
    payload: JsonObject,
  ): Promise<AgentMutationReceipt> {
    if (!context.host.mutateAgent) {
      throw new Error("agent durable/physical host port is unavailable");
    }
    const receipt = await context.host.mutateAgent({
      action,
      task_id: task.taskId,
      parent_task_id: task.parentTaskId,
      parent_session_id: task.parentSessionId,
      run_id: task.runId,
      authority_scope: {
        permission_ceiling_digest: task.scope.permissionCeilingDigest,
        tool_catalog_digest: task.scope.toolCatalogDigest,
        definition_digest: task.definition.digest,
        context_digest: task.context.digest,
        depth: task.scope.depth,
        lineage: task.scope.lineage,
      },
      ...payload,
    });
    if (!receipt.accepted) {
      throw new Error(asString(receipt.error, "agent durable mutation rejected"));
    }
    this.lifecycle.acceptDurable(
      task.taskId,
      asString(receipt.status, task.status) as AgentTaskStatus,
      Number(receipt.revision),
    );
    return receipt;
  }

  private resultCapability(result: AgentToolResult): AgentCapabilityResult {
    return {
      summary: result.summary,
      output: result as unknown as JsonObject,
      metadata: {
        canonical_agent_owner: "typescript",
        durable_task_owner: "python-subagent-task-store-port",
        physical_owner: "zyra-workspace-scheduler-port",
        task_id: result.taskId,
        task_status: result.status,
        python_agent_fallback: "false",
      },
    };
  }
}

function durableRecord(
  task: AgentTask,
  prompt: string,
  raw: JsonObject,
  parent: RuntimeRunInput,
): JsonObject {
  const constraints = asObject(parent.config.runtimeConstraints);
  const workspaceRoot = asString(constraints.workspaceRoot ?? constraints.workspace_root);
  return {
    run_id: task.runId,
    task_id: task.taskId,
    parent_task_id: task.parentTaskId,
    parent_session_id: task.parentSessionId,
    agent_type: task.definition.name,
    definition_id: task.definition.digest,
    status: "created",
    context_snapshot: {
      snapshot_id: task.context.snapshotId,
      parent_session_id: task.context.parentSessionId,
      parent_task_id: task.context.parentTaskId,
      parent_worker_request_id: task.context.parentWorkerRequestId,
      mode: task.context.mode,
      message_refs: task.context.messageRefs,
      artifact_refs: task.context.artifactRefs,
      evidence_refs: task.context.evidenceRefs,
      ancestry: task.context.ancestry,
      depth: task.context.depth,
      metadata: task.context.metadata,
    },
    tool_scope: {
      parent_tools: task.scope.parentTools,
      child_tools: task.scope.childTools,
      denied_tools: task.scope.deniedTools,
      required_tools: [],
      dynamic_tool_identities: {},
      digest: task.scope.digest,
    },
    permission: {
      parent_mode: task.scope.permissionMode,
      child_mode: task.scope.permissionMode,
      inherited_rule_ids: [],
      inherited_denials: [],
      exact_grants_inherited: false,
      mcp_servers: task.definition.mcpServers,
      monotonic: true,
      digest: task.scope.permissionCeilingDigest,
    },
    budget: budgetPayload(task.scope.budget),
    isolation_request: {
      run_id: task.runId,
      task_id: task.taskId,
      parent_task_id: task.parentTaskId,
      kind: task.definition.isolation,
      workspace_root: workspaceRoot,
      requested_cwd: asString(raw.requested_cwd),
      writable_paths: [],
      read_only_paths: [],
      network_allowed: false,
      cleanup_required: true,
      request_id: "agentisoreq_" + task.taskId,
      metadata: {
        logical_owner: "typescript-agent-runtime",
        physical_owner: "zyra-workspace-scheduler-port",
      },
    },
    execution_mode: task.executionMode,
    prompt_digest: canonicalDigest(prompt),
    revision: 0,
    attempt: 0,
    metadata: {
      child_session_id: task.childSessionId,
      canonical_logical_owner: "typescript",
      definition_digest: task.definition.digest,
      typescript_idempotency_key: task.idempotencyKey,
      typescript_tool_catalog_digest: task.scope.toolCatalogDigest,
      typescript_permission_ceiling_digest: task.scope.permissionCeilingDigest,
      typescript_scope_digest: task.scope.digest,
      typescript_cycle_key: task.scope.cycleKey,
      typescript_context_digest: task.context.digest,
      authority_scope_digest: canonicalDigest({
        permission: task.scope.permissionCeilingDigest,
        tools: task.scope.toolCatalogDigest,
        context: task.context.digest,
      }),
      python_logical_fallback: false,
    },
  };
}

function taskFromDurable(record: JsonObject, definition: AgentDefinition): AgentTask {
  const metadata = asObject(record.metadata);
  const rawScope = asObject(record.tool_scope);
  const rawPermission = asObject(record.permission);
  const rawContext = asObject(record.context_snapshot);
  const rawBudget = asObject(record.budget);
  const status = asString(record.status, "failed") as AgentTaskStatus;
  const budget: AgentBudget = {
    maxTurns: integer(rawBudget.max_turns, definition.budget.maxTurns),
    maxToolCalls: integer(rawBudget.max_tool_calls, definition.budget.maxToolCalls),
    maxInputTokens: integer(rawBudget.max_input_tokens, definition.budget.maxInputTokens),
    maxOutputTokens: integer(rawBudget.max_output_tokens, definition.budget.maxOutputTokens),
    maxResultChars: integer(rawBudget.max_result_chars, definition.budget.maxResultChars),
    maxWallTimeMs: integer(rawBudget.max_wall_time_ms, definition.budget.maxWallTimeMs),
    maxChildren: integer(rawBudget.max_children, definition.budget.maxChildren),
    maxDepth: integer(rawBudget.max_depth, definition.budget.maxDepth),
  };
  const context: AgentContextSnapshot = {
    snapshotId: asString(rawContext.snapshot_id),
    parentSessionId: asString(rawContext.parent_session_id),
    parentTaskId: asString(rawContext.parent_task_id),
    parentWorkerRequestId: asString(rawContext.parent_worker_request_id),
    mode: asString(rawContext.mode, "isolated") as AgentContextSnapshot["mode"],
    messageRefs: strings(rawContext.message_refs),
    artifactRefs: strings(rawContext.artifact_refs),
    evidenceRefs: strings(rawContext.evidence_refs),
    ancestry: strings(rawContext.ancestry),
    depth: integer(rawContext.depth, 1),
    digest: asString(metadata.typescript_context_digest),
    metadata: asObject(rawContext.metadata),
  };
  const scope: AgentScope = {
    parentTools: strings(rawScope.parent_tools),
    childTools: strings(rawScope.child_tools),
    deniedTools: strings(rawScope.denied_tools),
    permissionMode: asString(rawPermission.child_mode, "default"),
    permissionCeilingDigest: asString(
      metadata.typescript_permission_ceiling_digest,
      asString(rawPermission.digest),
    ),
    toolCatalogDigest: asString(metadata.typescript_tool_catalog_digest),
    depth: context.depth,
    lineage: context.ancestry,
    cycleKey: asString(metadata.typescript_cycle_key, definition.name),
    budget,
    digest: asString(metadata.typescript_scope_digest, asString(rawScope.digest)),
  };
  const rawResult = asObject(metadata.typescript_result);
  const result = Object.keys(rawResult).length > 0 ? resultFromDurable(rawResult, status) : null;
  return {
    taskId: asString(record.task_id),
    parentTaskId: asString(record.parent_task_id),
    parentSessionId: asString(record.parent_session_id),
    childSessionId: asString(metadata.child_session_id),
    runId: asString(record.run_id),
    definition,
    promptDigest: asString(record.prompt_digest),
    executionMode: asString(record.execution_mode, "foreground") as AgentTask["executionMode"],
    status,
    revision: integer(record.revision, 0),
    attempt: integer(record.attempt, 1),
    sequence: integer(metadata.typescript_progress_sequence, 0),
    scope,
    context,
    idempotencyKey: asString(metadata.typescript_idempotency_key, asString(record.task_id)),
    resultDigest: asString(metadata.result_digest),
    result,
    error: result?.error ?? "",
    createdAt: asString(record.created_at, new Date(0).toISOString()),
    updatedAt: asString(record.updated_at, new Date(0).toISOString()),
  };
}

function resultFromDurable(raw: JsonObject, status: AgentTaskStatus): AgentToolResult {
  return {
    ok: raw.ok === true,
    taskId: asString(raw.taskId ?? raw.task_id),
    status,
    summary: asString(raw.summary),
    artifacts: (Array.isArray(raw.artifacts) ? raw.artifacts.map((item) => asObject(item)) : []) as unknown as ArtifactReceipt[],
    usage: asObject(raw.usage),
    error: asString(raw.error),
    metadata: asObject(raw.metadata),
  };
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => asString(item)).filter(Boolean) : [];
}

function taskOutput(task: AgentTask): JsonObject {
  return {
    ok: task.result?.ok ?? !["failed", "cancelled"].includes(task.status),
    task_id: task.taskId,
    parent_task_id: task.parentTaskId,
    child_session_id: task.childSessionId,
    status: task.status,
    revision: task.revision,
    attempt: task.attempt,
    execution_mode: task.executionMode,
    definition_digest: task.definition.digest,
    scope_digest: task.scope.digest,
    context_digest: task.context.digest,
    result_digest: task.resultDigest,
    error: task.error,
  };
}

function queuedResult(task: AgentTask): AgentToolResult {
  return {
    ok: true,
    taskId: task.taskId,
    status: task.status,
    summary: "Agent task accepted for background execution.",
    artifacts: [],
    usage: {},
    error: "",
    metadata: {
      background: true,
      durable_revision: task.revision,
      canonical_agent_owner: "typescript",
    },
  };
}

function cancelledResult(task: AgentTask): AgentToolResult {
  return {
    ok: false,
    taskId: task.taskId,
    status: "cancelled",
    summary: "Agent task was cancelled.",
    artifacts: [],
    usage: {},
    error: "agent_cancelled",
    metadata: { canonical_agent_owner: "typescript", late_result_fenced: true },
  };
}

function spec(name: string, purpose: string, inputSchema: JsonObject, readOnly = false): ToolSpecContract {
  return {
    name,
    purpose,
    source: "claude-code-best AgentTool mechanisms internalized by Zyra TypeScript runtime",
    input_schema: inputSchema,
    output_schema: { type: "object" },
    metadata: {
      read_only: String(readOnly),
      concurrency_safe: String(readOnly),
      canonical_owner: "typescript-agent-runtime",
    },
    execution_provenance: {
      namespace: "agent",
      version: "zyra.typescript-agent-runtime.v1",
      state_owner: "typescript-logical/python-durable",
    },
  };
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) ? value : fallback;
}

export function isLegacyAgentTool(tool: ToolSpecContract): boolean {
  return AGENT_TOOLS.has(tool.name)
    || tool.source.includes("zyra_workers.subagents.agent_tool")
    || tool.source.includes("python-agent");
}
