import {
  asObject,
  asString,
  type JsonObject,
  type RuntimeRunInput,
  type RuntimeRunResult,
} from "../contracts.ts";
import type { AgentExecutionContext, AgentTask } from "./contracts.ts";
import {
  E03RuntimeError,
  type E03TaskState,
} from "../e03/contracts.ts";

export interface AgentRunContext {
  parentInput: RuntimeRunInput;
  runChild(input: RuntimeRunInput): Promise<RuntimeRunResult>;
}

export function assertAgentSourceRuntimeEnabled(): void {
  if (process.env.ZYRA_DISABLE_E04_AGENT_SOURCE_RUNTIME === "1") {
    throw new E03RuntimeError(
      "agent_source_runtime_disabled",
      "Agent run/resume source runtime is disabled; no legacy or Python logical fallback is permitted",
    );
  }
}

export function childRunInput(
  task: AgentTask,
  parent: RuntimeRunInput,
  argumentsValue: JsonObject,
): RuntimeRunInput {
  const turns = Array.isArray(argumentsValue.turns) ? argumentsValue.turns : [];
  const messages = Array.isArray(argumentsValue.messages)
    ? argumentsValue.messages.map((item) => asObject(item))
    : [
        {
          role: "user",
          content: asString(argumentsValue.prompt),
          metadata: {
            agent_task_id: task.taskId,
            parent_task_id: task.parentTaskId,
          },
        },
      ];
  const childTools = parent.tools.filter((tool) =>
    task.scope.childTools.includes(tool.name),
  );
  return {
    runId: parent.runId,
    taskId: task.taskId,
    nodeId: parent.nodeId,
    workerRequestId: "agent-worker-" + task.taskId + "-" + String(task.attempt),
    sessionId: task.childSessionId,
    messages,
    turns,
    tools: childTools,
    config: {
      ...parent.config,
      maxTurns: task.scope.budget.maxTurns,
      maxToolResultChars: task.scope.budget.maxResultChars,
      modelName:
        task.definition.model === "inherit"
          ? asString(parent.config.modelName, "zyra-local-code-model")
          : task.definition.model,
      runtimeConstraints: {
        ...asObject(parent.config.runtimeConstraints),
        agentDepth: task.scope.depth,
        agentLineage: task.scope.lineage,
        agentBudget: budgetPayload(task.scope.budget),
        agentParentTaskId: task.parentTaskId,
        agentTaskId: task.taskId,
      },
      permissionPolicy: {
        ...asObject(parent.config.permissionPolicy),
        mode: task.scope.permissionMode,
        parent_ceiling_digest: task.scope.permissionCeilingDigest,
      },
      controlCommands: [],
    },
    contextSnapshot: {
      version: "zyra.typescript-agent-context.v1",
      snapshot_id: task.context.snapshotId,
      parent_session_id: task.context.parentSessionId,
      parent_task_id: task.context.parentTaskId,
      ancestry: task.context.ancestry,
      depth: task.context.depth,
      digest: task.context.digest,
    },
    restoredState: asObject(argumentsValue.restored_state),
    metadata: {
      ...asObject(parent.metadata),
      agent_task_id: task.taskId,
      agent_parent_task_id: task.parentTaskId,
      agent_depth: task.scope.depth,
      agent_lineage: task.scope.lineage,
      agent_definition_digest: task.definition.digest,
      canonical_agent_owner: "typescript",
      runtime_lineage: {
        schema: "zyra.runtime-lineage/v1",
        relation: "agent",
        relation_id: task.taskId,
        parent_run_id: parent.runId,
        parent_task_id: parent.taskId,
        parent_session_id: parent.sessionId,
        parent_worker_request_id: parent.workerRequestId,
      },
    },
  };
}

export async function runAgent(
  task: AgentTask | E03TaskState,
  argumentsValue: JsonObject,
  context: AgentExecutionContext | AgentRunContext,
): Promise<RuntimeRunResult> {
  assertAgentSourceRuntimeEnabled();
  const input = isE03Task(task)
    ? e03ChildRunInput(task, context.parentInput, argumentsValue)
    : childRunInput(task, context.parentInput, argumentsValue);
  const startedAt = Date.now();
  const result = await context.runChild(input);
  if (isE03Task(task)) assertE03RunSettlement(task, result, startedAt);
  return result;
}

export function e03ChildRunInput(
  task: E03TaskState,
  parent: RuntimeRunInput,
  argumentsValue: JsonObject,
): RuntimeRunInput {
  const messages = Array.isArray(argumentsValue.messages)
    ? argumentsValue.messages.map((item) => asObject(item))
    : [
        {
          role: "user",
          content: asString(argumentsValue.prompt, task.prompt),
          metadata: {
            agent_task_id: task.identity.taskId,
            parent_task_id: task.identity.parentTaskId,
            agent_attempt_id: task.identity.attemptId,
          },
        },
      ];
  const tools = parent.tools
    .filter(
      (tool) =>
        task.scope.tools.length === 0 || task.scope.tools.includes(tool.name),
    )
    .filter((tool) => !task.scope.deniedTools.includes(tool.name));
  const restored = asObject(argumentsValue.restored_state);
  const restoredTaskId = asString(restored.agent_task_id);
  const restoredLeaseId = asString(restored.agent_lease_id);
  if (restoredTaskId && restoredTaskId !== task.identity.taskId)
    throw new E03RuntimeError(
      "agent_restore_task_mismatch",
      "restored agent state belongs to another task",
    );
  if (restoredLeaseId && restoredLeaseId !== task.identity.leaseId)
    throw new E03RuntimeError(
      "agent_restore_stale_lease",
      "restored agent state belongs to a stale execution lease",
    );
  const budget = task.definition.budget;
  return {
    ...parent,
    taskId: task.identity.taskId,
    sessionId: task.identity.sessionId,
    workerRequestId: `agent-worker:${task.identity.attemptId}`,
    messages,
    turns: Array.isArray(argumentsValue.turns) ? argumentsValue.turns : [],
    tools,
    config: {
      ...parent.config,
      maxTurns: budget.maxTurns,
      maxToolResultChars: budget.maxResultChars,
      modelName:
        task.definition.model === "inherit"
          ? asString(parent.config.modelName, "zyra-local-code-model")
          : task.definition.model,
      permissionPolicy: {
        ...asObject(parent.config.permissionPolicy),
        mode: task.scope.permissionMode,
        parent_ceiling_digest: task.scope.permissionCeilingDigest,
      },
      runtimeConstraints: {
        ...asObject(parent.config.runtimeConstraints),
        e03_agent_task_id: task.identity.taskId,
        e03_parent_task_id: task.identity.parentTaskId,
        e03_attempt_id: task.identity.attemptId,
        e03_attempt: task.identity.attempt,
        e03_lease_id: task.identity.leaseId,
        e03_expected_revision: task.revision,
        e03_lineage: task.identity.lineage,
        e03_scope_digest: task.scope.digest,
        e03_context_checksum: task.context.checksum,
        e03_agent_budget: e03BudgetPayload(budget),
        allowed_skills: task.scope.skills,
        allowed_mcp_servers: task.scope.mcpServers,
        python_agent_logical_fallback: false,
      },
      controlCommands: [],
    },
    contextSnapshot: {
      version: "zyra.e03-agent-context/v2",
      snapshot_id: task.context.snapshotId,
      parent_snapshot_id: task.context.parentSnapshotId,
      branch_id: task.context.branchId,
      sequence: task.context.sequence,
      message_refs: task.context.messageRefs,
      artifact_refs: task.context.artifactRefs,
      evidence_refs: task.context.evidenceRefs,
      memory_refs: task.context.memoryRefs,
      compact_boundary_ids: task.context.compactBoundaryIds,
      permission_digest: task.context.permissionDigest,
      tool_catalog_digest: task.context.toolCatalogDigest,
      checksum: task.context.checksum,
    },
    restoredState: {
      ...restored,
      agent_task_id: task.identity.taskId,
      agent_lease_id: task.identity.leaseId,
      agent_attempt_id: task.identity.attemptId,
      expected_revision: task.revision,
    },
    metadata: {
      ...asObject(parent.metadata),
      e03_agent_task_id: task.identity.taskId,
      e03_parent_task_id: task.identity.parentTaskId,
      e03_agent_lease_id: task.identity.leaseId,
      e03_agent_attempt: task.identity.attempt,
      e03_lineage: task.identity.lineage,
      skills: task.scope.skills,
      mcp_servers: task.scope.mcpServers,
      canonical_agent_owner: "typescript",
      python_logical_owner: false,
      runtime_lineage: {
        schema: "zyra.runtime-lineage/v1",
        relation: "agent",
        relation_id: task.identity.attemptId,
        parent_run_id: parent.runId,
        parent_task_id: parent.taskId,
        parent_session_id: parent.sessionId,
        parent_worker_request_id: parent.workerRequestId,
      },
    },
  };
}

function assertE03RunSettlement(
  task: E03TaskState,
  result: RuntimeRunResult,
  startedAt: number,
): void {
  const budget = task.definition.budget;
  const violations: string[] = [];
  if (result.turnCount > budget.maxTurns) violations.push("turns");
  if (result.toolCallCount > budget.maxToolCalls) violations.push("tool_calls");
  const inputTokens = nonNegativeInteger(result.metadata.input_tokens);
  const outputTokens = nonNegativeInteger(result.metadata.output_tokens);
  const resultChars = nonNegativeInteger(result.metadata.tool_result_chars);
  if (inputTokens > budget.maxInputTokens) violations.push("input_tokens");
  if (outputTokens > budget.maxOutputTokens) violations.push("output_tokens");
  if (resultChars > budget.maxResultChars) violations.push("result_chars");
  if (Date.now() - startedAt > budget.maxWallTimeMs)
    violations.push("wall_time");
  const resultTaskId = result.metadata.e03_agent_task_id;
  const resultLeaseId = result.metadata.e03_agent_lease_id;
  if (resultTaskId && resultTaskId !== task.identity.taskId)
    violations.push("task_ownership");
  if (resultLeaseId && resultLeaseId !== task.identity.leaseId)
    violations.push("lease_ownership");
  if (violations.length)
    throw new E03RuntimeError(
      "agent_run_settlement_rejected",
      `child settlement violated ${violations.join(", ")}`,
      {
        taskId: task.identity.taskId,
        leaseId: task.identity.leaseId,
        violations,
      },
    );
}

function e03BudgetPayload(value: E03TaskState["definition"]["budget"]): JsonObject {
  return {
    max_turns: value.maxTurns,
    max_tool_calls: value.maxToolCalls,
    max_input_tokens: value.maxInputTokens,
    max_output_tokens: value.maxOutputTokens,
    max_result_chars: value.maxResultChars,
    max_wall_time_ms: value.maxWallTimeMs,
    max_children: value.maxChildren,
    max_depth: value.maxDepth,
    max_concurrency: value.maxConcurrency,
    deadline_at: value.deadlineAt,
  };
}

function isE03Task(value: AgentTask | E03TaskState): value is E03TaskState {
  return "identity" in value && "checksum" in value;
}

function nonNegativeInteger(value: unknown): number {
  if (typeof value === "number")
    return Number.isSafeInteger(value) && value >= 0 ? value : 0;
  if (typeof value === "string" && /^\d+$/.test(value)) return Number(value);
  return 0;
}

function budgetPayload(value: AgentTask["scope"]["budget"]): JsonObject {
  return {
    maxTurns: value.maxTurns,
    maxToolCalls: value.maxToolCalls,
    maxInputTokens: value.maxInputTokens,
    maxOutputTokens: value.maxOutputTokens,
    maxResultChars: value.maxResultChars,
    maxWallTimeMs: value.maxWallTimeMs,
    maxChildren: value.maxChildren,
    maxDepth: value.maxDepth,
  };
}
