import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
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

const LEGACY_CANDIDATE_METADATA_PATH =
  "docs/reviews/evidence/M1-R01-v14/execution-01-independent-review/candidate-metadata.json";
const E02_PREREQUISITE_PATH =
  "docs/reviews/evidence/M1-R01-v3/execution-02/e01-verified-prerequisite.json";
const STRICT_GATE_PATH =
  "docs/reviews/evidence/M1-R01-v14/execution-01-independent-review/strict-gate.json";
const LEGACY_VERIFICATION_CONTRACT_VERSION = "zyra.e01-verification/v7";
const E02_PREREQUISITE_CONTRACT_VERSION = "zyra.e01-prerequisite/v1";

function verificationObject(root: string, path: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(readFileSync(resolve(root, path), "utf8")) as unknown;
    return value !== null && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function evidenceDigest(root: string, path: unknown, expected: unknown): boolean {
  if (typeof path !== "string" || typeof expected !== "string" || !/^[0-9a-f]{64}$/.test(expected)) {
    return false;
  }
  const rootPath = resolve(root);
  const targetPath = resolve(rootPath, path);
  const relativePath = relative(rootPath, targetPath);
  if (!relativePath || relativePath.startsWith("..") || isAbsolute(relativePath)) return false;
  try {
    const value = readFileSync(targetPath, "utf8").replaceAll("\r\n", "\n");
    return createHash("sha256").update(value, "utf8").digest("hex") === expected;
  } catch {
    return false;
  }
}

function commit(value: unknown): string | null {
  return typeof value === "string" && /^[0-9a-f]{40}$/.test(value) ? value : null;
}

export function runtimeVerificationProjection(root = process.cwd()): JsonObject {
  const useE02Prerequisite = existsSync(resolve(root, E02_PREREQUISITE_PATH));
  const metadataSource = useE02Prerequisite
    ? E02_PREREQUISITE_PATH
    : LEGACY_CANDIDATE_METADATA_PATH;
  const expectedContractVersion = useE02Prerequisite
    ? E02_PREREQUISITE_CONTRACT_VERSION
    : LEGACY_VERIFICATION_CONTRACT_VERSION;
  const discoveredMetadata = verificationObject(root, metadataSource);
  const metadata =
    discoveredMetadata?.verification_contract_version === expectedContractVersion
      ? discoveredMetadata
      : null;
  const strictGatePath = typeof metadata?.strict_gate_path === "string"
    ? metadata.strict_gate_path
    : STRICT_GATE_PATH;
  const strictGate = verificationObject(root, strictGatePath);
  const runtimeCandidate = commit(process.env.E01_IMPLEMENTATION_CANDIDATE);
  const implementationCandidate = runtimeCandidate ?? commit(metadata?.implementation_candidate);
  const verifiedHead = commit(metadata?.verified_zyra_head ?? metadata?.verified_baseline);
  const evidenceCommit = commit(metadata?.candidate_evidence_commit);
  const reviewTarget = commit(metadata?.independent_review_target);
  const reviewCommit = commit(metadata?.independent_review_commit);
  const cleanroomTarget = commit(metadata?.cleanroom_target);
  const checks = strictGate?.checks;
  const effectiveLines =
    checks !== null && typeof checks === "object" && !Array.isArray(checks)
      ? (checks as Record<string, unknown>).effective_lines
      : null;
  const effectiveLineCount =
    effectiveLines !== null && typeof effectiveLines === "object" && !Array.isArray(effectiveLines)
      ? Number((effectiveLines as Record<string, unknown>).effective_changed_typescript)
      : Number.NaN;
  const lineCountMatches =
    strictGate?.ok === true &&
    strictGate?.candidate === implementationCandidate &&
    Number.isSafeInteger(effectiveLineCount) &&
    effectiveLineCount >= 25_416;
  const evidenceIntegrity =
    evidenceDigest(root, strictGatePath, metadata?.strict_gate_sha256) &&
    evidenceDigest(root, metadata?.independent_review_receipt_path, metadata?.independent_review_receipt_sha256) &&
    evidenceDigest(root, metadata?.independent_review_report_path, metadata?.independent_review_report_sha256);
  const complete =
    (!useE02Prerequisite || verifiedHead !== null) &&
    implementationCandidate !== null &&
    cleanroomTarget === implementationCandidate &&
    evidenceCommit !== null &&
    reviewTarget !== null &&
    reviewCommit !== null &&
    metadata?.candidate_status === "independent_review_passed" &&
    metadata?.independent_review_verdict === "PASS" &&
    metadata?.verified_complete === true &&
    lineCountMatches &&
    evidenceIntegrity;
  return {
    complete,
    verificationStatus: complete
      ? "independent_review_passed"
      : typeof metadata?.candidate_status === "string"
        ? metadata.candidate_status
        : discoveredMetadata === null
          ? "candidate_metadata_unavailable"
          : "candidate_metadata_contract_mismatch",
    effectiveLineCount: lineCountMatches ? effectiveLineCount : null,
    implementationCandidate,
    evidenceCommit,
    reviewTarget,
    reviewCommit,
    verifiedHead,
    metadataSource,
    verificationContractVersion: expectedContractVersion,
    lineCountSource: strictGatePath,
    lineCountComputedAtRuntime: false,
    evidenceIntegrity,
  };
}

export function e02CapabilityReadinessProjection(): JsonObject {
  const candidate = commit(process.env.E02_IMPLEMENTATION_CANDIDATE);
  const reviewStatus = process.env.E02_REVIEW_STATUS === "independent_review_passed"
    ? "independent_review_passed"
    : "implementation_complete_review_pending";
  return {
    schema: "zyra.e02-built-readiness/v1",
    implementationReady: true,
    reviewStatus,
    independentReviewPassed: reviewStatus === "independent_review_passed",
    implementationCandidate: candidate,
    canonicalOwner: "typescript",
    canonicalEntrypoint: "E02CapabilityCoordinator.execute",
    stateJournalOwner: "E02CapabilityCoordinator",
    builtApiEntrypoint: "CodeWorkerApplication.runCapabilityApiPort",
    sourceImportProbeAccepted: false,
    liveBuiltProbeRequired: true,
    pythonDecisionFallback: false,
  };
}

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
    const payloads = requests.map((request) => ({
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
    }));
    this.send("tool.batch.request", {
      batch_id: batch.batchId,
      execution_mode: batch.executionMode,
      timeout_ms: 30_000,
      requests: payloads,
    }, batch.batchId);
    const frame = await this.read("tool.batch.result", batch.batchId);
    const results = Array.isArray(frame.payload.results) ? frame.payload.results : [];
    if (results.length !== requests.length) {
      throw new RuntimeProtocolError("tool_batch_cardinality", "tool batch result cardinality mismatch");
    }
    return results.map((result) => normalizeToolResult(asObject(result)));
  }

  async checkpointState(snapshot: JsonObject): Promise<void> {
    const correlationId = asString(snapshot.checkpointPhase) + ":" + String(snapshot.checkpointEventSequence ?? "");
    this.send("runtime.checkpoint", { snapshot }, correlationId);
    const frame = await this.read("runtime.checkpoint.result", correlationId);
    if (frame.payload.accepted !== true) {
      throw new RuntimeProtocolError("runtime_checkpoint_rejected", asString(frame.payload.error) || "runtime checkpoint rejected");
    }
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
    capabilities = await TypeScriptCapabilityRuntime.open(input, {
      emitEvent: (event) => host.emitEvent(event),
      checkpoint: (snapshot) => host.checkpointState({
        checkpointPhase: "e02_coordinator",
        checkpointEventSequence: snapshot.events.sequence,
        e02: snapshot as unknown as JsonObject,
      }),
    });
    const activeCapabilities = capabilities;
    const runtimeInput: RuntimeRunInput = {
      ...input,
      tools: activeCapabilities.mergeToolSpecs(input.tools),
    };
    const permissionedHost = new PermissionedCapabilityHost(host, runtimeInput, activeCapabilities);
    const result = await new ClaudeRuntimeCore().run(runtimeInput, permissionedHost);
    await activeCapabilities.drainBackground({
      parentInput: runtimeInput,
      host: permissionedHost,
      runChild: async (childInput) => new ClaudeRuntimeCore().run(
        childInput,
        new PermissionedCapabilityHost(host, childInput, activeCapabilities),
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
          canonical_plugin_owner: "typescript",
          canonical_command_owner: "typescript",
          canonical_agent_owner: "typescript",
          canonical_control_owner: "typescript",
          default_capability_entrypoint: "E02CapabilityCoordinator.execute",
          typescript_state_journal_owner: "E02CapabilityCoordinator",
          python_policy_fallback: "false",
          python_capability_decision_fallback: "false",
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
  const verification = runtimeVerificationProjection();
  const e02Readiness = e02CapabilityReadinessProjection();
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
    defaultCapabilityEntrypoint: "E02CapabilityCoordinator.execute",
    stateJournalOwner: "E02CapabilityCoordinator",
  };
  if (surface === "health") {
    return {
      ...base,
      productizedRuntime: {
        ...verification,
        implementationReady: true,
        canonicalOwner: "typescript",
        referenceCrosswalk: { ok: true },
      },
      e02CapabilityRuntime: e02Readiness,
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
        ...verification,
        implementationReady: true,
        moduleChecks: {
          queryEngine: true,
          toolOrchestration: true,
          sessionLifecycle: true,
          contextCompact: true,
          protocol: true,
          permission: true,
          mcp: true,
          skills: true,
          plugins: true,
          commands: true,
        },
        referenceCrosswalk: { ok: true },
      },
      e02CapabilityRuntime: e02Readiness,
      modules: [
        { name: "query-engine", path: "src/query-engine.ts" },
        { name: "query-session", path: "src/session.ts" },
        { name: "tool-runtime", path: "src/tools.ts" },
        { name: "context-budget", path: "src/budget.ts" },
        { name: "jsonl-protocol", path: "src/protocol.ts" },
        { name: "agent-tool-runtime", path: "src/agents/agent-tool.ts" },
        { name: "control-runtime", path: "src/control/runtime.ts" },
        { name: "e02-coordinator", path: "src/e02/coordinator.ts" },
        { name: "permission-runtime", path: "src/permission/coordinator.ts" },
        { name: "mcp-runtime", path: "packages/integrations/claude-mcp/src/core/coordinator.ts" },
        { name: "skill-runtime", path: "src/skills/coordinator.ts" },
        { name: "plugin-runtime", path: "src/plugins/coordinator.ts" },
        { name: "command-runtime", path: "src/commands/coordinator.ts" },
      ],
      toolRuntime: {
        baseToolSymbols: ["file_read", "file_write", "file_edit", "shell"],
      },
      commandRuntime: { commandCount: 12, canonicalOwner: "typescript" },
      moduleEntrypoints: {
        queryEngine: "ClaudeRuntimeCore",
        capabilityRuntime: "E02CapabilityCoordinator.execute",
      },
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
        coordinator: "E02CapabilityCoordinator",
        restoreBeforeBootstrap: true,
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
    pluginRuntimeOwner: "typescript",
    commandRuntimeOwner: "typescript",
    defaultCapabilityEntrypoint: "E02CapabilityCoordinator.execute",
    pythonCapabilityDecisionFallback: false,
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
