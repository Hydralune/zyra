import { asObject, asString, type JsonObject, type RuntimeRunInput, type RuntimeRunResult } from "../contracts.ts";
import type { AgentExecutionContext, AgentTask } from "./contracts.ts";

export function childRunInput(
  task: AgentTask,
  parent: RuntimeRunInput,
  argumentsValue: JsonObject,
): RuntimeRunInput {
  const turns = Array.isArray(argumentsValue.turns) ? argumentsValue.turns : [];
  const messages = Array.isArray(argumentsValue.messages)
    ? argumentsValue.messages.map((item) => asObject(item))
    : [{
      role: "user",
      content: asString(argumentsValue.prompt),
      metadata: { agent_task_id: task.taskId, parent_task_id: task.parentTaskId },
    }];
  const childTools = parent.tools.filter((tool) => task.scope.childTools.includes(tool.name));
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
      modelName: task.definition.model === "inherit"
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
    },
  };
}

export async function runAgent(
  task: AgentTask,
  argumentsValue: JsonObject,
  context: AgentExecutionContext,
): Promise<RuntimeRunResult> {
  return context.runChild(childRunInput(task, context.parentInput, argumentsValue));
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
