import { createHash } from "node:crypto";

import type {
  AgentMutationReceipt,
  AgentMutationRequest,
  ArtifactReceipt,
  ArtifactRequest,
  JsonObject,
  RuntimeEvent,
  RuntimeHost,
  RuntimeRunInput,
  ToolBatch,
  ToolExecutionRequest,
  ToolExecutionResponse,
} from "./contracts.ts";
import { asObject, asString } from "./contracts.ts";
import { TypeScriptCapabilityRuntime } from "./capabilities.ts";
import {
  TypeScriptPermissionEvaluator,
  type PermissionToolContext,
} from "./permission/index.ts";
import { ClaudeRuntimeCore } from "./query-engine.ts";
import {
  PermissionEnforcementRuntime,
  type PermissionDecisionView,
} from "./tools/permission-enforcement-runtime.ts";
import { ToolExecutionSettlementRuntime } from "./tools/execution-settlement-runtime.ts";

export class PermissionedCapabilityHost implements RuntimeHost {
  private readonly delegate: RuntimeHost;
  private readonly input: RuntimeRunInput;
  private readonly capabilities: TypeScriptCapabilityRuntime;
  private readonly permission: TypeScriptPermissionEvaluator;
  private readonly enforcement: PermissionEnforcementRuntime;
  private readonly settlement: ToolExecutionSettlementRuntime;

  constructor(
    delegate: RuntimeHost,
    input: RuntimeRunInput,
    capabilities: TypeScriptCapabilityRuntime,
  ) {
    this.delegate = delegate;
    this.input = input;
    this.capabilities = capabilities;
    this.enforcement = new PermissionEnforcementRuntime(`permission:${input.sessionId}:${input.workerRequestId}`);
    this.settlement = new ToolExecutionSettlementRuntime({
      runtimeId: `execution-settlement:${input.sessionId}:${input.workerRequestId}`,
      sessionId: input.sessionId,
      runId: input.runId,
      workerRequestId: input.workerRequestId,
    });
    const constraints = input.config.runtimeConstraints as JsonObject | undefined;
    this.permission = new TypeScriptPermissionEvaluator(
      input.config.permissionPolicy,
      {
        sessionId: input.sessionId,
        workspaceRoot: typeof constraints?.workspaceRoot === "string"
          ? constraints.workspaceRoot
          : typeof constraints?.workspace_root === "string"
            ? constraints.workspace_root
            : "",
      },
    );
  }

  emitEvent(event: RuntimeEvent): Promise<void> {
    return this.delegate.emitEvent(event);
  }

  async executeBatch(
    batch: ToolBatch,
    requests: ToolExecutionRequest[],
  ): Promise<ToolExecutionResponse[]> {
    const enriched = requests.map((request) => {
      const tool = this.input.tools.find((item) => item.name === request.toolName);
      const identity = inferToolIdentity(
        request.toolName,
        tool,
      );
      const context: PermissionToolContext = {
        runId: this.input.runId,
        taskId: this.input.taskId,
        sessionId: this.input.sessionId,
        toolCallId: request.toolCallId,
        toolName: request.toolName,
        namespace: identity.namespace,
        serverId: identity.serverId,
        version: identity.version,
        schemaDigest: identity.schemaDigest,
        operation: inferOperation(request.toolName, tool),
        arguments: request.arguments,
        metadata: request.metadata,
      };
      const local = this.capabilities.owns(request.toolName);
      return {
        ...request,
        permissionDecision: this.permission.evaluate(context),
        executionOwner: local ? this.capabilities.owner(request.toolName) : "python-tool-executor",
        permissionOnly: local,
        metadata: {
          ...request.metadata,
          canonical_permission_owner: "typescript",
          canonical_capability_owner: local
            ? this.capabilities.owner(request.toolName)
            : "python-tool-executor",
        },
      };
    });
    this.settlement.planBatch({
      batchId: batch.batchId,
      executionMode: batch.executionMode,
      calls: enriched.map((request, position) => ({
        callId: request.toolCallId,
        toolName: request.toolName,
        arguments: request.arguments,
        localCapability: request.permissionOnly,
        executionOwner: request.executionOwner,
        position,
        metadata: request.metadata,
      })),
    });
    for (const request of enriched) {
      const value = request.permissionDecision as unknown as Record<string, unknown>;
      const effect = value.effect === "allow" || value.effect === "ask" ? value.effect : "deny";
      this.settlement.recordPermission(
        request.toolCallId,
        effect,
        typeof value.reason === "string" ? value.reason : `typescript_permission_${String(value.effect)}`,
        typeof value.ruleId === "string"
          ? value.ruleId
          : typeof value.rule_id === "string"
            ? value.rule_id
            : null,
      );
    }
    this.settlement.beginDelegation(
      batch.batchId,
      enriched
        .filter((request) => (request.permissionDecision as unknown as { effect?: string }).effect === "allow")
        .map((request) => request.toolCallId),
    );
    const committed = await this.enforcement.enforce(enriched, {
      identifyRequest: (request) => ({
        requestId: request.toolCallId,
        toolName: request.toolName,
        inputDigest: createHash("sha256").update(JSON.stringify(request.arguments)).digest("hex"),
      }),
      decide: (request) => request.permissionDecision,
      viewDecision: (decision) => {
        const value = decision as unknown as Record<string, unknown>;
        return {
          effect: value.effect === "allow" || value.effect === "ask" ? value.effect : "deny",
          reason: typeof value.reason === "string" ? value.reason : `typescript_permission_${String(value.effect)}`,
          ruleId: typeof value.ruleId === "string"
            ? value.ruleId
            : typeof value.rule_id === "string"
              ? value.rule_id
              : null,
        } satisfies PermissionDecisionView;
      },
      delegate: (allowed) => this.delegate.executeBatch(batch, [...allowed]),
      identifyReceipt: (receipt) => ({
        requestId: receipt.tool_call_id,
        success: receipt.ok,
        errorCode: receipt.error ?? null,
      }),
      blockedReceipt: (request, decision) => ({
        tool_call_id: request.toolCallId,
        ok: false,
        summary: decision.effect === "ask"
          ? `Tool execution requires approval: ${decision.reason}`
          : `Tool execution denied: ${decision.reason}`,
        output: {
          permission_effect: decision.effect,
          permission_reason: decision.reason,
          permission_rule_id: decision.ruleId ?? "",
        },
        artifacts: [],
        error: decision.effect === "ask" ? "permission_approval_required" : "permission_denied",
        completed_at: new Date().toISOString(),
        metadata: {
          ...request.metadata,
          permission_effect: decision.effect,
          permission_reason: decision.reason,
          permission_rule_id: decision.ruleId ?? "",
          permission_abort_loop: decision.effect === "ask" ? "true" : "false",
          canonical_permission_owner: "typescript",
          permission_delegated: "false",
        },
      }),
      failedReceipt: (request, _identity, code, message) => ({
        tool_call_id: request.toolCallId,
        ok: false,
        summary: `Permission enforcement failed closed: ${message}`,
        output: { permission_error: code },
        artifacts: [],
        error: code,
        completed_at: new Date().toISOString(),
        metadata: {
          ...request.metadata,
          permission_effect: "deny",
          permission_reason: message,
          canonical_permission_owner: "typescript",
          permission_delegated: "false",
        },
      }),
      serializeDecision: (decision) => decision as unknown as JsonObject,
      serializeReceipt: (receipt) => receipt as unknown as JsonObject,
    });
    for (let index = 0; index < committed.length; index += 1) {
      const request = enriched[index];
      const receipt = committed[index];
      if ((request.permissionDecision as unknown as { effect?: string }).effect !== "allow") continue;
      this.settlement.appendProgress(
        request.toolCallId,
        0,
        receipt.ok ? "progress" : "diagnostic",
        receipt.summary,
        32_768,
      );
      this.settlement.recordGatewayReceipt({
        callId: request.toolCallId,
        ok: receipt.ok,
        summary: receipt.summary,
        output: receipt.output,
        error: receipt.error ?? null,
        receiptMetadata: receipt.metadata,
        intermediate: request.permissionOnly && permissionCommitAccepted(receipt),
      });
    }
    const output: ToolExecutionResponse[] = [];
    for (let index = 0; index < enriched.length; index += 1) {
      const request = enriched[index];
      const receipt = committed[index];
      if (!request.permissionOnly || !permissionCommitAccepted(receipt)) {
        output.push(receipt);
        continue;
      }
      let settlementAttempted = false;
      try {
        this.settlement.beginLocalCapability(request.toolCallId);
        const result = await this.capabilities.execute(
          request.toolName,
          request.arguments,
          {
            parentInput: this.input,
            host: this,
            runChild: async (childInput) => new ClaudeRuntimeCore().run(
              childInput,
              new PermissionedCapabilityHost(this.delegate, childInput, this.capabilities),
            ),
          },
        );
        settlementAttempted = true;
        await this.delegate.settleCapability?.({
          toolCallId: request.toolCallId,
          toolName: request.toolName,
          ok: true,
          error: "",
          metadata: {
            canonical_capability_owner: request.executionOwner ?? "typescript",
          },
        });
        this.settlement.recordLocalSettlement({
          callId: request.toolCallId,
          ok: true,
          summary: result.summary,
          output: result.output,
          error: null,
          metadata: result.metadata,
        });
        output.push({
          tool_call_id: request.toolCallId,
          ok: true,
          summary: result.summary,
          output: result.output,
          artifacts: [],
          error: null,
          completed_at: new Date().toISOString(),
          metadata: {
            ...receipt.metadata,
            ...result.metadata,
            permission_effect: "allow",
            canonical_permission_owner: "typescript",
            python_capability_fallback: "false",
          },
        });
      } catch (error) {
        if (settlementAttempted) {
          throw error;
        }
        const errorMessage = error instanceof Error ? error.message : String(error);
        settlementAttempted = true;
        await this.delegate.settleCapability?.({
          toolCallId: request.toolCallId,
          toolName: request.toolName,
          ok: false,
          error: errorMessage,
          metadata: {
            canonical_capability_owner: request.executionOwner ?? "typescript",
          },
        });
        this.settlement.recordLocalSettlement({
          callId: request.toolCallId,
          ok: false,
          summary: errorMessage,
          output: {},
          error: "typescript_capability_error",
          metadata: {
            canonical_capability_owner: request.executionOwner ?? "typescript",
          },
        });
        output.push({
          tool_call_id: request.toolCallId,
          ok: false,
          summary: errorMessage,
          output: {},
          artifacts: [],
          error: "typescript_capability_error",
          completed_at: new Date().toISOString(),
          metadata: {
            ...receipt.metadata,
            canonical_permission_owner: "typescript",
            canonical_capability_owner: request.executionOwner ?? "typescript",
            python_capability_fallback: "false",
          },
        });
      }
    }
    this.settlement.completeBatch(batch.batchId);
    return output;
  }

  externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    return this.delegate.externalize(request);
  }

  mutateAgent(request: AgentMutationRequest): Promise<AgentMutationReceipt> {
    if (!this.delegate.mutateAgent) {
      throw new Error("Python durable/physical agent port is disconnected");
    }
    return this.delegate.mutateAgent(request);
  }

  isAborted(): boolean {
    return this.delegate.isAborted();
  }

  snapshot(): JsonObject {
    return {
      permission: this.permission.snapshot(),
      enforcement: this.enforcement.snapshot() as unknown as JsonObject,
      settlement: this.settlement.snapshot() as unknown as JsonObject,
      capabilities: this.capabilities.snapshot(),
    };
  }
}

function permissionCommitAccepted(response: ToolExecutionResponse): boolean {
  return response.ok
    && response.metadata.permission_effect === "allow"
    && response.metadata.permission_commit_only === "true";
}

function inferToolIdentity(
  toolName: string,
  spec: RuntimeRunInput["tools"][number] | undefined,
): { namespace: string; serverId: string; version: string; schemaDigest: string } {
  const provenance = asObject(spec?.execution_provenance);
  const version = asString(provenance.version);
  const schemaDigest = asString(provenance.schema_digest);
  if (toolName.startsWith("mcp__")) {
    const [, serverId = ""] = toolName.split("__", 3);
    return {
      namespace: asString(provenance.namespace) || "mcp",
      serverId: asString(provenance.server_id) || serverId,
      version,
      schemaDigest,
    };
  }
  if (toolName.startsWith("mcp_")) {
    return {
      namespace: asString(provenance.namespace) || "mcp",
      serverId: asString(provenance.server_id),
      version,
      schemaDigest,
    };
  }
  if ([
    "list_skills",
    "skill",
    "read_skill_resource",
    "list_commands",
    "command",
    "list_plugins",
    "plugin_command",
  ].includes(toolName)) {
    return {
      namespace: asString(provenance.namespace) || "skill",
      serverId: asString(provenance.server_id),
      version,
      schemaDigest,
    };
  }
  if ([
    "Agent",
    "Task",
    "agent_status",
    "agent_cancel",
    "agent_resume",
    "agent_message",
  ].includes(toolName)) {
    return {
      namespace: asString(provenance.namespace) || "agent",
      serverId: asString(provenance.server_id),
      version,
      schemaDigest,
    };
  }
  return {
    namespace: asString(provenance.namespace) || "builtin",
    serverId: asString(provenance.server_id),
    version,
    schemaDigest,
  };
}

function inferOperation(
  toolName: string,
  tool?: RuntimeRunInput["tools"][number],
): string {
  const metadata = asObject(tool?.metadata);
  if (metadata.read_only === true || asString(metadata.read_only).toLowerCase() === "true") {
    return "read";
  }
  if ([
    "file_read",
    "agent_status",
    "list_skills",
    "read_skill_resource",
    "list_commands",
    "list_plugins",
    "mcp_list_resources",
    "mcp_read_resource",
    "mcp_list_prompts",
    "mcp_get_prompt",
  ].includes(toolName)) {
    return "read";
  }
  if (toolName === "file_write" || toolName === "file_edit") {
    return "write";
  }
  return "execute";
}
