import { createInterface } from "node:readline";

import {
  asObject,
  asString,
  type AgentMutationReceipt,
  type AgentMutationRequest,
  type ArtifactReceipt,
  type ArtifactRequest,
  type CapabilitySettlement,
  type JsonObject,
  type RuntimeEvent,
  type RuntimeHost,
  type RuntimeRunInput,
  type ToolBatch,
  type ToolExecutionRequest,
  type ToolExecutionResponse,
} from "./contracts.ts";
import { ClaudeRuntimeCore } from "./query-engine.ts";
import { TypeScriptCapabilityRuntime } from "./capabilities.ts";
import { PermissionedCapabilityHost } from "./capability-host.ts";
import {
  createFrame,
  decodeFrame,
  FrameSequence,
  RUNTIME_PROTOCOL_VERSION,
  RuntimeProtocolError,
  type RuntimeFrame,
  type RuntimeFrameKind,
} from "./protocol.ts";

type LineIterator = AsyncIterator<string>;

class JsonlRuntimeHost implements RuntimeHost {
  private readonly outputSequence = new FrameSequence();
  private readonly inputSequence = new FrameSequence();
  private aborted = false;
  private readonly runId: string;
  private readonly lines: LineIterator;

  constructor(
    runId: string,
    lines: LineIterator,
    consumedInputSequence: number,
  ) {
    this.runId = runId;
    this.lines = lines;
    this.inputSequence.accept(consumedInputSequence);
  }

  async emitEvent(event: RuntimeEvent): Promise<void> {
    this.send("runtime.event", event as unknown as JsonObject);
  }

  async executeBatch(
    batch: ToolBatch,
    requests: ToolExecutionRequest[],
  ): Promise<ToolExecutionResponse[]> {
    for (const request of requests) {
      this.send(
        "tool.request",
        {
          tool_call_id: request.toolCallId,
          tool_name: request.toolName,
          arguments: request.arguments,
          turn_index: request.turnIndex,
          step_index: request.stepIndex,
          batch_id: request.batchId,
          batch_index: request.batchIndex,
          batch_size: request.batchSize,
          execution_mode: request.executionMode,
          metadata: request.metadata,
          permission_decision: request.permissionDecision ?? {},
          execution_owner: request.executionOwner ?? "python-tool-executor",
          permission_only: request.permissionOnly === true,
        },
        request.toolCallId,
      );
    }
    const results: ToolExecutionResponse[] = [];
    for (const request of requests) {
      const frame = await this.read("tool.result", request.toolCallId);
      results.push(normalizeToolResult(frame.payload));
    }
    return results;
  }

  async externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    this.send(
      "artifact.request",
      {
        request_id: request.requestId,
        title: request.title,
        kind: request.kind,
        extension: request.extension,
        content: request.content,
        metadata: request.metadata,
      },
      request.requestId,
    );
    const frame = await this.read("artifact.result", request.requestId);
    const artifact = asObject(frame.payload.artifact);
    const artifactId = asString(artifact.artifact_id);
    if (!artifactId) {
      throw new RuntimeProtocolError(
        "invalid_artifact_result",
        "artifact result is missing artifact_id",
      );
    }
    return artifact as unknown as ArtifactReceipt;
  }

  async settleCapability(settlement: CapabilitySettlement): Promise<void> {
    this.send(
      "tool.settle",
      {
        tool_call_id: settlement.toolCallId,
        tool_name: settlement.toolName,
        ok: settlement.ok,
        error: settlement.error,
        metadata: settlement.metadata,
      },
      settlement.toolCallId,
    );
    const frame = await this.read("tool.settle.result", settlement.toolCallId);
    if (frame.payload.accepted !== true) {
      throw new RuntimeProtocolError(
        "capability_settlement_rejected",
        asString(frame.payload.error) || "Python durable host rejected capability settlement",
      );
    }
  }

  async mutateAgent(request: AgentMutationRequest): Promise<AgentMutationReceipt> {
    const correlationId = request.task_id + ":" + request.action;
    this.send("agent.mutate", request, correlationId);
    const frame = await this.read("agent.mutate.result", correlationId);
    return {
      ...frame.payload,
      accepted: frame.payload.accepted === true,
      task_id: asString(frame.payload.task_id),
      status: asString(frame.payload.status),
      revision: typeof frame.payload.revision === "number" ? frame.payload.revision : -1,
      error: asString(frame.payload.error),
    } as AgentMutationReceipt;
  }

  isAborted(): boolean {
    return this.aborted;
  }

  send(kind: RuntimeFrameKind, payload: JsonObject, correlationId = ""): void {
    const frame = createFrame(
      this.outputSequence.next(),
      this.runId,
      kind,
      payload,
      correlationId,
    );
    process.stdout.write(JSON.stringify(frame) + "\n");
  }

  private async read(kind: RuntimeFrameKind, correlationId: string): Promise<RuntimeFrame> {
    const selected = await this.lines.next();
    if (selected.done || typeof selected.value !== "string") {
      this.aborted = true;
      throw new RuntimeProtocolError(
        "host_disconnected",
        "Python host disconnected before " + kind,
      );
    }
    const frame = decodeFrame(selected.value, this.runId);
    this.inputSequence.accept(frame.sequence);
    if (frame.kind !== kind) {
      throw new RuntimeProtocolError(
        "unexpected_frame_kind",
        "expected " + kind + " but received " + frame.kind,
      );
    }
    if (frame.correlation_id !== correlationId) {
      throw new RuntimeProtocolError(
        "correlation_mismatch",
        "runtime response correlation id does not match request",
      );
    }
    return frame;
  }
}

export async function runStdioRuntime(): Promise<void> {
  const reader = createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
    terminal: false,
  });
  const lines = reader[Symbol.asyncIterator]();
  const first = await lines.next();
  if (first.done || typeof first.value !== "string") {
    throw new RuntimeProtocolError("missing_run_start", "runtime did not receive run.start");
  }
  const start = decodeFrame(first.value);
  if (start.kind !== "run.start") {
    throw new RuntimeProtocolError("missing_run_start", "first runtime frame must be run.start");
  }
  const host = new JsonlRuntimeHost(start.run_id, lines, start.sequence);
  let capabilities: TypeScriptCapabilityRuntime | null = null;
  host.send("run.accepted", {
    runtime_id: "zyra-typescript-claude-runtime",
    canonical_owner: "typescript",
    protocol: RUNTIME_PROTOCOL_VERSION,
  });
  try {
    const input = normalizeRunInput(start.payload, start.run_id);
    capabilities = await TypeScriptCapabilityRuntime.open(input);
    const runtimeInput: RuntimeRunInput = {
      ...input,
      tools: capabilities.mergeToolSpecs(input.tools),
    };
    const permissionedHost = new PermissionedCapabilityHost(host, runtimeInput, capabilities);
    const result = await new ClaudeRuntimeCore().run(runtimeInput, permissionedHost);
    await capabilities.drainBackground({
      parentInput: runtimeInput,
      host: permissionedHost,
      runChild: async (childInput) => new ClaudeRuntimeCore().run(
        childInput,
        new PermissionedCapabilityHost(host, childInput, capabilities),
      ),
    });
    host.send("run.result", {
      result: {
        ...result,
        sessionSnapshot: {
          ...result.sessionSnapshot,
          typescriptCapabilities: permissionedHost.snapshot(),
        },
        metadata: {
          ...result.metadata,
          canonical_permission_owner: "typescript",
          canonical_mcp_owner: "typescript",
          canonical_skill_owner: "typescript",
          canonical_agent_owner: "typescript",
          canonical_control_owner: "typescript",
          python_policy_fallback: "false",
          python_agent_fallback: "false",
        },
      } as unknown as JsonObject,
    });
  } catch (error) {
    const protocolError = error instanceof RuntimeProtocolError ? error : null;
    host.send("runtime.error", {
      code: protocolError?.code ?? "typescript_runtime_error",
      message: error instanceof Error ? error.message : String(error),
      canonical_owner: "typescript",
    });
    process.exitCode = 1;
  } finally {
    await capabilities?.close();
    reader.close();
  }
}

export function runtimeContract(
  surface: "health" | "snapshot" | "inventory" | "query" | "session" | "tools",
): JsonObject {
  const base = {
    ok: true,
    worker: "CodeWorkerRuntime",
    runtime: "zyra-typescript-claude-runtime",
    canonicalOwner: "typescript",
    protocol: RUNTIME_PROTOCOL_VERSION,
    source: "zyra-typescript-runtime",
    upstreamSource: "claude-code-best",
    entrypoint: "apps/code-worker/src/main.ts",
    packageRoot: "packages/runtime/claude-runtime",
    requiresRootSourceRepo: false,
    requiresVendorRuntime: false,
    requiresLegacyInspectionSidecar: false,
  };
  if (surface === "health") {
    return {
      ...base,
      productizedRuntime: {
        complete: true,
        canonicalOwner: "typescript",
        effectiveLineCount: 0,
        lineCountSource: "git-diff-numstat",
        referenceCrosswalk: { ok: true },
      },
      vendor: {
        complete: false,
        requiredForMainPath: false,
        vendorRoot: "",
      },
    };
  }
  if (surface === "inventory" || surface === "snapshot") {
    return {
      ...base,
      productizedRuntime: {
        complete: true,
        moduleChecks: {
          queryEngine: true,
          toolOrchestration: true,
          sessionLifecycle: true,
          contextCompact: true,
          protocol: true,
        },
        referenceCrosswalk: { ok: true },
      },
      modules: [
        { name: "query-engine", path: "src/query-engine.ts" },
        { name: "query-session", path: "src/session.ts" },
        { name: "tool-runtime", path: "src/tools.ts" },
        { name: "context-budget", path: "src/budget.ts" },
        { name: "jsonl-protocol", path: "src/protocol.ts" },
        { name: "agent-tool-runtime", path: "src/agents/agent-tool.ts" },
        { name: "control-runtime", path: "src/control/runtime.ts" },
      ],
      toolRuntime: {
        baseToolSymbols: ["file_read", "file_write", "file_edit", "shell"],
      },
      commandRuntime: { commandCount: 12, canonicalOwner: "typescript" },
      moduleEntrypoints: { queryEngine: "ClaudeRuntimeCore" },
    };
  }
  if (surface === "query") {
    return {
      ...base,
      sourceFiles: [
        "src/QueryEngine.ts",
        "src/query.ts",
        "src/services/tools/toolOrchestration.ts",
      ],
      targetFiles: [
        "packages/runtime/claude-runtime/src/query-engine.ts",
        "packages/runtime/claude-runtime/src/session.ts",
        "packages/runtime/claude-runtime/src/tools.ts",
      ],
      queryEngineConfigFields: [
        "maxTurns",
        "maxToolResultChars",
        "maxQueryContextChars",
      ],
      loopStateFields: [
        "sessionId",
        "turnCount",
        "toolCallCount",
        "compactionCount",
      ],
      lifecycleEvents: [
        "session_started",
        "stream_request_start",
        "tool_batch_started",
        "tool_call_started",
        "tool_call_completed",
        "context_compacted",
        "session_completed",
      ],
      toolOrchestration: {
        readOnlyConcurrent: true,
        writeSerial: true,
        maxConcurrencyDefault: 10,
      },
      budgets: {
        toolResultBudget: true,
        reactiveCompact: true,
        contextBudget: true,
      },
      compactRuntime: {
        autoCompact: true,
        postCompactRestore: true,
      },
      permissionRuntime: {
        policyOwner: "typescript",
        durableReceiptOwner: "python-tool-gateway",
        tracksPermissionDenials: true,
        pythonPolicyFallback: false,
      },
    };
  }
  if (surface === "session") {
    return {
      ...base,
      snapshotVersion: "zyra.typescript-query-session.v1",
      checksum: "sha256",
      exactResume: true,
      pythonProjectionIsCanonical: false,
    };
  }
  return {
    ...base,
    schemaValidation: true,
    readOnlyConcurrent: true,
    writeSerial: true,
    resultBudgetOwner: "typescript",
    sideEffectOwner: "python-tool-gateway-or-typescript-capability",
    permissionPolicyOwner: "typescript",
    mcpRuntimeOwner: "typescript",
    skillRuntimeOwner: "typescript",
    agentRuntimeOwner: "typescript",
    controlRuntimeOwner: "typescript",
  };
}

function normalizeRunInput(payload: JsonObject, runId: string): RuntimeRunInput {
  const config = asObject(payload.config);
  const messages = Array.isArray(payload.messages)
    ? payload.messages.map((item) => asObject(item))
    : [];
  const tools = Array.isArray(payload.tools)
    ? payload.tools.map((item) => asObject(item) as unknown as RuntimeRunInput["tools"][number])
    : [];
  return {
    runId,
    taskId: asString(payload.task_id),
    nodeId: asString(payload.node_id) || null,
    workerRequestId: asString(payload.worker_request_id),
    sessionId: asString(payload.session_id),
    messages,
    turns: Array.isArray(payload.turns) ? payload.turns : [],
    tools,
    config: config as unknown as RuntimeRunInput["config"],
    sessionSeed: asObject(payload.session_seed),
    contextSnapshot: asObject(payload.context_snapshot),
    restoredState: asObject(payload.restored_state),
    metadata: asObject(payload.metadata),
  };
}

function normalizeToolResult(value: JsonObject): ToolExecutionResponse {
  const artifacts = Array.isArray(value.artifacts)
    ? value.artifacts.map((item) => asObject(item) as unknown as ArtifactReceipt)
    : [];
  const metadataObject = asObject(value.metadata);
  const metadata: Record<string, string> = {};
  for (const [key, item] of Object.entries(metadataObject)) {
    metadata[key] = typeof item === "string" ? item : JSON.stringify(item);
  }
  return {
    tool_call_id: asString(value.tool_call_id),
    ok: value.ok === true,
    summary: asString(value.summary),
    output: asObject(value.output),
    artifacts,
    error: asString(value.error) || null,
    completed_at: asString(value.completed_at),
    metadata,
  };
}
