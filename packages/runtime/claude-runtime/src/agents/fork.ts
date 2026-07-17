import {
  asObject,
  asString,
  type JsonObject,
  type RuntimeRunInput,
} from "../contracts.ts";
import type { AgentScope } from "./contracts.ts";
import { createContextSnapshot } from "./memory.ts";

export function forkAgentContext(
  input: RuntimeRunInput,
  taskId: string,
  scope: AgentScope,
  argumentsValue: JsonObject,
) {
  const parentContext = asObject(argumentsValue.context);
  const modeValue = asString(argumentsValue.context_mode, "isolated");
  const mode = ["isolated", "fork", "resume"].includes(modeValue)
    ? (modeValue as "isolated" | "fork" | "resume")
    : "isolated";
  return createContextSnapshot({
    snapshotId: "agentctx_" + taskId,
    parentSessionId: input.sessionId,
    parentTaskId: input.taskId,
    parentWorkerRequestId: input.workerRequestId,
    mode,
    messageRefs: strings(parentContext.message_refs),
    artifactRefs: strings(parentContext.artifact_refs),
    evidenceRefs: strings(parentContext.evidence_refs),
    ancestry: scope.lineage,
    depth: scope.depth,
    metadata: {
      parent_context_digest: asString(parentContext.digest),
      compact_boundary_id: asString(parentContext.compact_boundary_id),
      context_epoch:
        typeof parentContext.context_epoch === "number"
          ? parentContext.context_epoch
          : 0,
      fork_owner: "typescript-agent-runtime",
    },
  });
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item)).filter(Boolean)
    : [];
}
