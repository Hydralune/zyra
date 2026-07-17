import { asObject, asString, type JsonObject } from "../contracts.ts";
import type { AgentTask } from "./contracts.ts";

export function resumeArguments(task: AgentTask, raw: JsonObject): JsonObject {
  const restored = asObject(raw.restored_state);
  const correlation = asString(raw.resume_correlation_id);
  if (!correlation) {
    throw new Error("agent resume requires resume_correlation_id");
  }
  if (
    restored.agent_task_id &&
    asString(restored.agent_task_id) !== task.taskId
  ) {
    throw new Error("agent resume state belongs to another task");
  }
  return {
    ...raw,
    context_mode: "resume",
    restored_state: {
      ...restored,
      agent_task_id: task.taskId,
      resume_correlation_id: correlation,
      expected_revision: task.revision,
    },
  };
}
