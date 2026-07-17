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
import { ClaudeRuntimeCore } from "./query-engine.ts";
import {
  ToolExecutionSettlementRuntime,
  type ExecutionSettlementSnapshot,
} from "./tools/execution-settlement-runtime.ts";

export class PermissionedCapabilityHost implements RuntimeHost {
  private readonly delegate: RuntimeHost;
  private readonly input: RuntimeRunInput;
  private readonly capabilities: TypeScriptCapabilityRuntime;
  private readonly settlement: ToolExecutionSettlementRuntime;

  constructor(
    delegate: RuntimeHost,
    input: RuntimeRunInput,
    capabilities: TypeScriptCapabilityRuntime,
  ) {
    this.delegate = delegate;
    this.input = input;
    this.capabilities = capabilities;
    this.settlement = new ToolExecutionSettlementRuntime({
      runtimeId: `execution-settlement:${input.sessionId}:${input.workerRequestId}`,
      sessionId: input.sessionId,
      runId: input.runId,
      workerRequestId: input.workerRequestId,
    });
    const restoredSettlement = asObject(restoredCapabilityState(input.restoredState).settlement);
    if (Object.keys(restoredSettlement).length > 0) {
      this.settlement.restore(restoredSettlement as unknown as ExecutionSettlementSnapshot, true);
    }
  }

  emitEvent(event: RuntimeEvent): Promise<void> {
    return this.delegate.emitEvent(event);
  }

  checkpointState(snapshot: JsonObject): Promise<void> {
    return this.delegate.checkpointState?.({
      ...snapshot,
      typescriptCapabilities: this.snapshot(),
    }) ?? Promise.resolve();
  }

  async executeBatch(
    batch: ToolBatch,
    requests: ToolExecutionRequest[],
  ): Promise<ToolExecutionResponse[]> {
    const enriched = [];
    for (const request of requests) {
      const tool = this.input.tools.find((item) => item.name === request.toolName);
      const identity = inferToolIdentity(
        request.toolName,
        tool,
      );
      const local = this.capabilities.owns(request.toolName);
      const permissionRuntime = this.capabilities.e02.runtime;
      const authorization = await this.capabilities.authorize({
        runId: permissionRuntime.runId,
        taskId: permissionRuntime.taskId,
        sessionId: permissionRuntime.sessionId,
        sessionRevision: 0,
        workerRequestId: permissionRuntime.workerRequestId,
        toolCallId: request.toolCallId,
        toolName: request.toolName,
        namespace: identity.namespace,
        serverId: identity.serverId,
        operation: inferOperation(request.toolName, tool),
        workspaceRoot: workspaceRoot(this.input),
        arguments: request.arguments,
        issueExecutionPermit: local,
        metadata: {
          ...request.metadata,
          version: identity.version,
          schema_digest: identity.schemaDigest,
          worker_request_id: this.input.workerRequestId,
          session_revision: sessionRevision(this.input),
          delegated_run_id: this.input.runId,
          delegated_task_id: this.input.taskId,
          delegated_session_id: this.input.sessionId,
          delegated_worker_request_id: this.input.workerRequestId,
          authority_inheritance: this.input.sessionId === permissionRuntime.sessionId
            ? "direct"
            : "parent-e02-permission-ceiling",
        },
      });
      enriched.push({
        ...request,
        arguments: authorization.finalArguments,
        permissionDecision: authorization.enforcement.decision as unknown as JsonObject,
        executionOwner: local ? this.capabilities.owner(request.toolName) : "python-tool-executor",
        permissionOnly: local,
        e02PermitId: authorization.permitId,
        e02SessionRevision: 0,
        metadata: {
          ...request.metadata,
          canonical_permission_owner: "typescript",
          canonical_capability_owner: local
            ? this.capabilities.owner(request.toolName)
            : "python-tool-executor",
          e02_permit_id: authorization.permitId ?? "",
          e02_permission_decision_id: authorization.enforcement.decision.decisionId,
          e02_final_arguments_digest: authorization.enforcement.decision.finalArgumentsDigest,
          e02_replan_required: String(authorization.enforcement.replanRequired),
        },
      });
    }
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
    const committed = await this.delegate.executeBatch(batch, enriched);
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
          {
            toolCallId: request.toolCallId,
            permitId: request.e02PermitId,
            sessionRevision: request.e02SessionRevision,
            namespace: inferToolIdentity(request.toolName, this.input.tools.find((item) => item.name === request.toolName)).namespace,
            serverId: inferToolIdentity(request.toolName, this.input.tools.find((item) => item.name === request.toolName)).serverId,
            operation: inferOperation(request.toolName, this.input.tools.find((item) => item.name === request.toolName)),
            metadata: request.metadata,
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
    const capabilities = this.capabilities.snapshot();
    return {
      permission: capabilities.permission as unknown as JsonObject,
      settlement: this.settlement.snapshot() as unknown as JsonObject,
      capabilities: capabilities as unknown as JsonObject,
    };
  }
}

function restoredCapabilityState(value: JsonObject | null | undefined): JsonObject {
  const root = asObject(value);
  const candidates = [
    asObject(root.typescriptCapabilities),
    asObject(asObject(root.typescript_runtime_snapshot).typescriptCapabilities),
    asObject(asObject(root.typescript_runtime).typescriptCapabilities),
    asObject(asObject(root.query_engine).typescriptCapabilities),
    asObject(asObject(asObject(root.metadata).typescript_runtime_snapshot).typescriptCapabilities),
  ];
  return candidates.find((candidate) => Object.keys(candidate).length > 0) ?? {};
}

function permissionCommitAccepted(response: ToolExecutionResponse): boolean {
  return response.ok
    && response.metadata.permission_effect === "allow"
    && response.metadata.permission_commit_only === "true";
}

function sessionRevision(input: RuntimeRunInput): number {
  const metadataRevision = asObject(input.metadata).session_revision;
  if (typeof metadataRevision === "number" && Number.isSafeInteger(metadataRevision) && metadataRevision >= 0) {
    return metadataRevision;
  }
  const restored = restoredCapabilityState(input.restoredState);
  const runtime = asObject(asObject(restored.capabilities).runtime);
  const restoredRevision = runtime.sessionRevision;
  return typeof restoredRevision === "number" && Number.isSafeInteger(restoredRevision) && restoredRevision >= 0
    ? restoredRevision
    : 0;
}

function workspaceRoot(input: RuntimeRunInput): string {
  const constraints = asObject(input.config.runtimeConstraints);
  return asString(constraints.workspace_root)
    || asString(constraints.workspaceRoot)
    || asString(asObject(input.metadata).workspace_root)
    || process.cwd();
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
