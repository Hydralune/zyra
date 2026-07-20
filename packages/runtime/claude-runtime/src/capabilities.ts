import type {
  JsonObject,
  RuntimeRunInput,
  ToolSpecContract,
} from "./contracts.ts";
import type {
  AgentCapabilityResult,
  AgentExecutionContext,
} from "./agents/index.ts";
import {
  cloneJson,
  digest,
  E02CapabilityCoordinator,
  type E02AuthorizationInput,
  type E02AuthorizationResult,
  type E02CapabilityCoordinatorSnapshot,
  type E02CoordinatorPorts,
  type E02ExecutionContext,
  type E02ExecutionReceipt,
  type PermissionApprovalResponse,
  type PermissionContinuationRecord,
  type PermissionDecisionRecord,
} from "./e02/index.ts";
import type { McpCoordinatorExecution } from "../../../integrations/claude-mcp/src/index.ts";
import type { SkillCoordinatorExecution } from "./skills/index.ts";
import type { PluginCoordinatorExecution } from "./plugins/index.ts";
import type { CommandCoordinatorExecution } from "./commands/index.ts";

export type CapabilityExecutionResult =
  | McpCoordinatorExecution
  | SkillCoordinatorExecution
  | PluginCoordinatorExecution
  | CommandCoordinatorExecution
  | AgentCapabilityResult;

export class TypeScriptCapabilityRuntime {
  readonly e02: E02CapabilityCoordinator;
  private readonly resumedAuthorizations = new Map<string, E02AuthorizationResult>();

  private constructor(e02: E02CapabilityCoordinator) {
    this.e02 = e02;
  }

  static async open(
    input: RuntimeRunInput,
    ports: E02CoordinatorPorts = {},
  ): Promise<TypeScriptCapabilityRuntime> {
    return new TypeScriptCapabilityRuntime(
      await E02CapabilityCoordinator.open(input, ports),
    );
  }

  mergeToolSpecs(existing: ToolSpecContract[]): ToolSpecContract[] {
    return this.e02.mergeToolSpecs(existing);
  }

  toolSpecs(): ToolSpecContract[] {
    return this.e02.toolSpecs();
  }

  owns(toolName: string): boolean {
    return this.e02.owns(toolName);
  }

  owner(toolName: string): string {
    return this.e02.owner(toolName);
  }

  authorize(input: E02AuthorizationInput): Promise<E02AuthorizationResult> {
    const resumed = this.resumedAuthorizations.get(input.toolCallId);
    if (resumed) {
      assertResumedAuthorizationBinding(input, resumed);
      this.resumedAuthorizations.delete(input.toolCallId);
      return Promise.resolve(cloneJson(resumed));
    }
    return this.e02.authorize(input);
  }

  resumePermission(response: PermissionApprovalResponse): E02AuthorizationResult {
    let authorization: E02AuthorizationResult;
    try {
      authorization = this.e02.resumePermission(response);
    } catch (error) {
      const snapshot = this.e02.snapshot();
      const replay = restoredApprovalReplay(snapshot, response);
      if (!replay) {
        const replayState = approvalReplayState(snapshot, response);
        throw new Error(
          `${error instanceof Error ? error.message : String(error)}; exact replay rejected (${replayState})`,
        );
      }
      authorization = replay;
    }
    this.resumedAuthorizations.set(response.toolCallId, cloneJson(authorization));
    return cloneJson(authorization);
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    agentContext?: AgentExecutionContext,
    executionContext: Partial<E02ExecutionContext> = {},
  ): Promise<CapabilityExecutionResult> {
    const receipt = await this.executeWithReceipt(
      toolName,
      argumentsValue,
      agentContext,
      executionContext,
    );
    return receipt.result as CapabilityExecutionResult;
  }

  executeWithReceipt(
    toolName: string,
    argumentsValue: JsonObject,
    agentContext?: AgentExecutionContext,
    executionContext: Partial<E02ExecutionContext> = {},
  ): Promise<E02ExecutionReceipt> {
    const toolCallId = executionContext.toolCallId
      || `direct:${toolName}:${this.e02.runtime.workerRequestId}`;
    return this.e02.execute(toolName, argumentsValue, {
      ...executionContext,
      toolCallId,
      agentContext,
    });
  }

  async drainBackground(context: AgentExecutionContext): Promise<void> {
    await this.e02.drainBackground(context);
  }

  snapshot(): E02CapabilityCoordinatorSnapshot {
    return this.e02.snapshot();
  }

  health(): ReturnType<E02CapabilityCoordinator["health"]> {
    return this.e02.health();
  }

  async close(): Promise<void> {
    await this.e02.close();
  }
}

function assertResumedAuthorizationBinding(
  input: E02AuthorizationInput,
  authorization: E02AuthorizationResult,
): void {
  const decision = authorization.enforcement.decision;
  const binding = decision.requestBinding;
  const mismatch = (
    String(binding.run_id ?? "") !== input.runId
    || String(binding.task_id ?? "") !== input.taskId
    || String(binding.session_id ?? "") !== input.sessionId
    || Number(binding.session_revision ?? 0) !== Number(input.sessionRevision ?? 0)
    || String(binding.worker_request_id ?? "") !== String(input.workerRequestId ?? input.toolCallId)
    || String(binding.tool_call_id ?? "") !== input.toolCallId
    || String(binding.tool_name ?? "") !== input.toolName
    || String(binding.namespace ?? "builtin") !== String(input.namespace ?? "builtin")
    || String(binding.server_id ?? "") !== String(input.serverId ?? "")
    || decision.originalArgumentsDigest !== digest(input.arguments)
  );
  if (mismatch) {
    throw new Error(
      `resumed permission approval ${decision.continuationRequestId ?? decision.decisionId} does not match the physical tool request`,
    );
  }
}

function approvalReplayState(
  snapshot: E02CapabilityCoordinatorSnapshot,
  response: PermissionApprovalResponse,
): string {
  const envelope = snapshot.permission.approvals.envelopes.find(
    (item) => item.requestId === response.requestId,
  );
  const evaluator = snapshot.permission.evaluator;
  const continuationSnapshot = evaluator.continuations as unknown as {
    records?: PermissionContinuationRecord[];
  };
  const continuation = continuationSnapshot.records?.find(
    (item) => item.requestId === response.requestId,
  );
  const permits = snapshot.executionLedger.permits.filter(
    (item) => item.toolCallId === response.toolCallId,
  );
  return [
    `envelope=${envelope?.status ?? "missing"}`,
    `envelope_response=${String(envelope?.metadata.response_id === response.responseId)}`,
    `continuation=${continuation?.status ?? "missing"}`,
    `continuation_response=${String(continuation?.responseId === response.responseId)}`,
    `tool_call=${String(continuation?.toolCallId === response.toolCallId)}`,
    `permits=${permits.map((item) => item.status).join(",") || "missing"}`,
    `decisions=${Array.isArray(evaluator.decisions) ? evaluator.decisions.length : 0}`,
  ].join(";");
}

function restoredApprovalReplay(
  snapshot: E02CapabilityCoordinatorSnapshot,
  response: PermissionApprovalResponse,
): E02AuthorizationResult | null {
  const envelope = snapshot.permission.approvals.envelopes.find(
    (item) => item.requestId === response.requestId,
  );
  const evaluator = snapshot.permission.evaluator;
  const continuationSnapshot = evaluator.continuations as unknown as {
    records?: PermissionContinuationRecord[];
  };
  const continuations = Array.isArray(evaluator.continuations)
    ? evaluator.continuations as PermissionContinuationRecord[]
    : Array.isArray(continuationSnapshot?.records)
      ? continuationSnapshot.records
      : [];
  const decisions = Array.isArray(evaluator.decisions)
    ? evaluator.decisions as PermissionDecisionRecord[]
    : [];
  const continuation = continuations.find(
    (item) => item.requestId === response.requestId,
  );
  if (
    !envelope
    || envelope.status !== "responded"
    || envelope.metadata.response_id !== response.responseId
    || envelope.metadata.response_effect !== response.effect
    || envelope.metadata.response_accepted !== true
    || !continuation
    || continuation.responseId !== response.responseId
    || continuation.toolCallId !== response.toolCallId
    || !new Set([
      response.effect === "allow" ? "approved" : "denied",
      "consumed",
    ]).has(continuation.status)
  ) {
    return null;
  }
  const permit = response.effect === "allow"
    ? snapshot.executionLedger.permits.find(
      (item) => item.status === "issued"
        && item.toolCallId === response.toolCallId
        && item.metadata.continuation_request_id === response.requestId
        && item.metadata.resumed_approval_response_id === response.responseId,
    ) ?? null
    : null;
  if (response.effect === "allow" && !permit) return null;
  const decision = decisions.find(
    (item) => item.decisionId === permit?.decisionId,
  ) ?? decisions.find(
    (item) => item.continuationRequestId === response.requestId
      && item.effect === response.effect
      && item.metadata.approval_response_id === response.responseId,
  );
  if (!decision) return null;
  const enforcement = {
    decision: cloneJson(decision),
    allowed: decision.effect === "allow",
    blocked: decision.effect !== "allow",
    pendingApproval: false,
    replanRequired: decision.replanRequired,
    recoveryInput: cloneJson(decision.recoveryInput),
    finalArguments: cloneJson(decision.finalArguments),
    approvalEnvelopeId: envelope.envelopeId,
    stateDigest: digest({
      decision_id: decision.decisionId,
      response_id: response.responseId,
      replay: "restart_safe_exact_approval",
    }),
  };
  return {
    enforcement,
    permit,
    permitId: permit?.permitId ?? null,
    finalArguments: cloneJson(decision.finalArguments),
    stateDigest: digest({ enforcement: enforcement.stateDigest, permit: permit?.bindingDigest ?? null }),
  };
}
