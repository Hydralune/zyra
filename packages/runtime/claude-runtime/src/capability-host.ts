import type {
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

export class PermissionedCapabilityHost implements RuntimeHost {
  private readonly delegate: RuntimeHost;
  private readonly input: RuntimeRunInput;
  private readonly capabilities: TypeScriptCapabilityRuntime;
  private readonly permission: TypeScriptPermissionEvaluator;

  constructor(
    delegate: RuntimeHost,
    input: RuntimeRunInput,
    capabilities: TypeScriptCapabilityRuntime,
  ) {
    this.delegate = delegate;
    this.input = input;
    this.capabilities = capabilities;
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
      const identity = inferToolIdentity(
        request.toolName,
        this.input.tools.find((tool) => tool.name === request.toolName),
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
        operation: inferOperation(request.toolName),
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
    const committed = await this.delegate.executeBatch(batch, enriched);
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
        const result = await this.capabilities.execute(request.toolName, request.arguments);
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
    return output;
  }

  externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    return this.delegate.externalize(request);
  }

  isAborted(): boolean {
    return this.delegate.isAborted();
  }

  snapshot(): JsonObject {
    return {
      permission: this.permission.snapshot(),
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
  return {
    namespace: asString(provenance.namespace) || "builtin",
    serverId: asString(provenance.server_id),
    version,
    schemaDigest,
  };
}

function inferOperation(toolName: string): string {
  if ([
    "file_read",
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
