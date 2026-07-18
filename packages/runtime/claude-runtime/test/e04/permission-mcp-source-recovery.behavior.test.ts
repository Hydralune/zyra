import { expect, test } from "bun:test";

import { TypeScriptCapabilityRuntime } from "../../src/capabilities.ts";
import type { JsonObject, RuntimeRunInput } from "../../src/contracts.ts";
import type {
  E02RuntimeIdentity,
  PermissionApprovalResponse,
} from "../../src/e02/contracts.ts";
import {
  PermissionEvaluator,
  PermissionIdentity,
  type PermissionIdentityInput,
} from "../../src/permission/index.ts";
import {
  McpCapabilityCatalog,
  McpClientRuntime,
  McpConnectionRuntime,
  McpInstructionRuntime,
  McpPaginationRuntime,
  McpRateLimitRuntime,
  McpRequestJournal,
  McpRetryRuntime,
  McpSchemaRuntime,
  McpServerPolicy,
  type JsonRpcMessage,
  type McpConnectionPhase,
  type McpServerConfigRecord,
  type McpTransportAdapter,
  type McpTransportEvent,
  type McpTransportHealth,
  type McpTransportRequest,
  type McpTransportResponse,
  type McpTransportSnapshot,
} from "../../../../integrations/claude-mcp/src/index.ts";

const instant = "2026-07-18T13:00:00.000Z";

function runtimeIdentity(id: string, epoch = 1): E02RuntimeIdentity {
  return {
    runtimeId: `e04-permission-runtime-${id}`,
    runId: `e04-permission-run-${id}`,
    taskId: `e04-permission-task-${id}`,
    sessionId: `e04-permission-session-${id}`,
    workerRequestId: `e04-permission-worker-${id}`,
    epoch,
  };
}

function permissionInput(
  id: string,
  overrides: Partial<PermissionIdentityInput> = {},
): PermissionIdentityInput {
  return {
    runId: `e04-permission-run-${id}`,
    taskId: `e04-permission-task-${id}`,
    sessionId: `e04-permission-session-${id}`,
    sessionRevision: 1,
    workerRequestId: `e04-permission-worker-${id}`,
    toolCallId: `e04-permission-call-${id}`,
    toolName: "file_read",
    namespace: "builtin",
    operation: "read",
    workspaceRoot: "G:/agent-zoo/zyra",
    arguments: { path: "package.json" },
    metadata: { e04_slice: "04c" },
    ...overrides,
  };
}

function runtimeInput(): RuntimeRunInput {
  return {
    runId: "e04-default-permission-run",
    taskId: "e04-default-permission-task",
    sessionId: "e04-default-permission-session",
    workerRequestId: "e04-default-permission-worker",
    messages: [{ role: "user", content: "Exercise the permission source runtime." }],
    turns: [],
    tools: [],
    config: {
      maxTurns: null,
      maxToolResultChars: 8_000,
      maxTurnToolResultChars: null,
      maxQueryContextChars: 32_000,
      continueOnError: false,
      maxReadOnlyConcurrency: 4,
      emitToolUseSummaries: true,
      allowEmptyTurns: false,
      modelName: "e04-permission-model",
      runtimeConstraints: { workspaceRoot: "G:/agent-zoo/zyra" },
      permissionPolicy: { mode: "default", interactive: true, headless: false },
      controlCommands: [],
    },
    restoredState: null,
  } as unknown as RuntimeRunInput;
}

function approvalResponse(
  identity: ReturnType<typeof PermissionIdentity.create>,
  requestId: string,
): PermissionApprovalResponse {
  return {
    responseId: `e04-response-${identity.context.toolCallId}`,
    requestId,
    runId: identity.context.runId,
    sessionId: identity.context.sessionId,
    sessionRevision: identity.context.sessionRevision,
    workerRequestId: identity.context.workerRequestId,
    toolCallId: identity.context.toolCallId,
    effect: "allow",
    responder: "e04-permission-suite",
    respondedAt: instant,
    metadata: { e04_slice: "04c" },
  };
}

test("e04-permission-allow-ask-deny", async () => {
  const capabilities = await TypeScriptCapabilityRuntime.open(runtimeInput());
  try {
    const base = capabilities.e02.runtime;
    const authorize = (id: string, toolName: string, operation: string, argumentsValue: JsonObject) => capabilities.authorize({
      runId: base.runId,
      taskId: base.taskId,
      sessionId: base.sessionId,
      sessionRevision: 0,
      workerRequestId: base.workerRequestId,
      toolCallId: id,
      toolName,
      namespace: "builtin",
      operation,
      workspaceRoot: "G:/agent-zoo/zyra",
      arguments: argumentsValue,
      issueExecutionPermit: false,
      metadata: { e04_default_entry: "runCapabilityApiPort" },
    });
    const allowed = await authorize("e04-allow", "file_read", "read", { path: "package.json" });
    const asked = await authorize("e04-ask", "file_write", "write", { path: "G:/agent-zoo/zyra/e04.txt", content: "approval" });
    const denied = await authorize("e04-deny", "file_write", "write", { path: "C:/outside/e04.txt", content: "blocked" });

    expect(allowed.enforcement.decision.effect).toBe("allow");
    expect(asked.enforcement.decision.effect).toBe("ask");
    expect(asked.enforcement.decision.continuationRequestId).not.toBeNull();
    expect(denied.enforcement.decision.effect).toBe("deny");
    expect(denied.enforcement.decision.reasonCode).toBe("workspace_boundary_denied");
    expect(allowed.enforcement.decision.metadata.source_custody).toContain("handleCoordinatorPermission");
  } finally {
    await capabilities.close();
  }
});

test("e04-permission-hook-failure", async () => {
  const order: string[] = [];
  const evaluator = new PermissionEvaluator({
    runtime: runtimeIdentity("hook-failure"),
    clock: () => instant,
    classifier: async (identity) => {
      order.push("classifier");
      return {
        effect: "ask",
        confidence: 0.8,
        explanation: "classifier is advisory only",
        model: "e04-test-classifier",
        inputDigest: identity.requestFingerprint,
        outputDigest: "sha256:e04-classifier-output",
      };
    },
  });
  evaluator.hooks.register({
    hookId: "e04-failing-hook",
    pluginId: null,
    enabled: true,
    order: 1,
    toolPattern: "shell",
    namespacePattern: "*",
    serverPattern: "*",
    operationPattern: "*",
    timeoutMs: 1_000,
    canMutateArguments: false,
    failClosed: false,
    metadata: {},
    handler: () => {
      order.push("hook");
      throw new Error("expected non-critical hook failure");
    },
  });
  const decision = await evaluator.evaluateAsync(permissionInput("hook-failure", {
    toolName: "shell",
    operation: "execute",
    arguments: { command: "Get-ChildItem" },
  }));
  expect(order).toEqual(["hook", "classifier"]);
  expect(decision.hooks[0]?.outcome).toBe("error");
  expect(decision.effect).toBe("ask");
  expect(decision.risk.classifierSuggestion).toBe("ask");
});

test("e04-permission-continuation", () => {
  const request = permissionInput("continuation", {
    toolName: "file_write",
    operation: "write",
    arguments: { path: "G:/agent-zoo/zyra/e04-continuation.txt", content: "approved" },
  });
  const identity = PermissionIdentity.create(request);
  const evaluator = new PermissionEvaluator({
    runtime: runtimeIdentity("continuation"),
    clock: () => instant,
  });
  const asked = evaluator.evaluate(request);
  expect(asked.effect).toBe("ask");
  expect(asked.continuationRequestId).not.toBeNull();
  const restored = new PermissionEvaluator({
    runtime: runtimeIdentity("continuation", 2),
    clock: () => instant,
    restored: evaluator.snapshot(),
  });
  const resumed = restored.resumeApproval(approvalResponse(identity, asked.continuationRequestId!));
  expect(resumed.accepted).toBe(true);
  expect(resumed.decision?.effect).toBe("allow");
  expect(resumed.decision?.metadata.exact_approval_binding).toBe(true);
});

test("e04-permission-disable", () => {
  const evaluator = new PermissionEvaluator({ runtime: runtimeIdentity("disable"), clock: () => instant });
  expect(evaluator.evaluate(permissionInput("disable-normal")).effect).toBe("allow");
  const previous = process.env.ZYRA_DISABLE_E04_PERMISSION_SOURCE_RUNTIME;
  process.env.ZYRA_DISABLE_E04_PERMISSION_SOURCE_RUNTIME = "1";
  try {
    expect(() => evaluator.evaluate(permissionInput("disable-kill"))).toThrow("migrated TypeScript permission source runtime is disabled");
  } finally {
    if (previous === undefined) delete process.env.ZYRA_DISABLE_E04_PERMISSION_SOURCE_RUNTIME;
    else process.env.ZYRA_DISABLE_E04_PERMISSION_SOURCE_RUNTIME = previous;
  }
});

class FixtureMcpTransport implements McpTransportAdapter {
  readonly serverId: string;
  readonly transportId: string;
  readonly calls: string[] = [];
  startAttempts = 0;
  initializedNotifications = 0;
  private phase: McpConnectionPhase = "idle";
  private readonly failStartAttempts: number;
  private readonly connectionEpoch: number;
  private readonly messageListeners = new Set<(message: JsonRpcMessage) => void | Promise<void>>();
  private readonly eventListeners = new Set<(event: McpTransportEvent) => void | Promise<void>>();

  constructor(serverId: string, failStartAttempts = 0, connectionEpoch = 1) {
    this.serverId = serverId;
    this.transportId = `e04-transport-${serverId}-${connectionEpoch}`;
    this.failStartAttempts = failStartAttempts;
    this.connectionEpoch = connectionEpoch;
  }

  async start(): Promise<McpTransportHealth> {
    this.startAttempts += 1;
    if (this.startAttempts <= this.failStartAttempts) throw new Error("fixture transport start failed");
    this.phase = "ready";
    return this.health();
  }

  async request(request: McpTransportRequest): Promise<McpTransportResponse> {
    this.calls.push(request.method);
    const result: JsonObject = request.method === "initialize"
      ? {
        protocolVersion: "2025-06-18",
        capabilities: {
          tools: { listChanged: true },
          resources: { listChanged: true },
          prompts: { listChanged: true },
        },
        serverInfo: { name: this.serverId, version: "1.0.0" },
        instructions: "E04 MCP fixture",
      }
      : request.method === "tools/list"
        ? { tools: [] }
        : request.method === "resources/list"
          ? { resources: [] }
          : request.method === "resources/templates/list"
            ? { resourceTemplates: [] }
            : request.method === "prompts/list"
              ? { prompts: [] }
              : {};
    return {
      requestId: request.requestId,
      message: {
        jsonrpc: "2.0",
        id: "id" in request.message ? request.message.id : request.requestId,
        result,
      },
      transportId: this.transportId,
      connectionEpoch: this.connectionEpoch,
      elapsedMs: 1,
      replayed: false,
      headers: {},
      metadata: { e04_fixture: true },
    };
  }

  async notify(message: JsonRpcMessage): Promise<void> {
    if ("method" in message && message.method === "notifications/initialized") {
      this.initializedNotifications += 1;
    }
  }

  async close(): Promise<void> {
    this.phase = "closed";
  }

  health(): McpTransportHealth {
    return {
      transportId: this.transportId,
      serverId: this.serverId,
      phase: this.phase,
      connectionEpoch: this.connectionEpoch,
      connected: this.phase === "ready",
      startedAt: this.startAttempts ? instant : null,
      lastActivityAt: this.startAttempts ? instant : null,
      lastMessageAt: this.calls.length ? instant : null,
      pendingRequests: 0,
      completedRequests: this.calls.length,
      failedRequests: 0,
      reconnectCount: Math.max(0, this.startAttempts - 1),
      bytesSent: 0,
      bytesReceived: 0,
      processId: null,
      endpoint: "in-process:e04",
      lastFailure: null,
    };
  }

  snapshot(): McpTransportSnapshot {
    return {
      version: "zyra.mcp-transport/v1",
      transportId: this.transportId,
      serverId: this.serverId,
      phase: this.phase,
      connectionEpoch: this.connectionEpoch,
      sequence: this.calls.length,
      health: this.health(),
      events: [],
      pending: [],
      completed: [],
      idempotencyReceipts: [],
      metadata: { e04_fixture: true },
    };
  }

  onMessage(listener: (message: JsonRpcMessage) => void | Promise<void>): () => void {
    this.messageListeners.add(listener);
    return () => this.messageListeners.delete(listener);
  }

  onEvent(listener: (event: McpTransportEvent) => void | Promise<void>): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }
}

function mcpPolicy(): McpServerPolicy {
  const policy = new McpServerPolicy();
  policy.replace([], 0, { interactive: "allow", autonomous: "allow" });
  return policy;
}

function mcpConfig(serverId = "e04-server"): McpServerConfigRecord {
  return {
    serverId,
    displayName: "E04 MCP Server",
    enabled: true,
    source: "session",
    sourceRevision: 1,
    transport: { kind: "in_process", factoryId: "e04-fixture", options: {} },
    retry: {
      maximumAttempts: 2,
      initialDelayMs: 1,
      maximumDelayMs: 2,
      multiplier: 1,
      jitterRatio: 0,
      retryStatusCodes: [503],
      retryJsonRpcCodes: [-32001],
    },
    timeouts: {
      connectMs: 1_000,
      initializeMs: 1_000,
      requestMs: 1_000,
      idleMs: 1_000,
      shutdownMs: 1_000,
      authMs: 1_000,
      taskPollMs: 100,
    },
    rateLimit: {
      maximumConcurrent: 2,
      requestsPerMinute: 120,
      burst: 4,
      queueCapacity: 8,
      queueTimeoutMs: 1_000,
    },
    restartPolicy: "on_failure",
    networkPolicy: "offline",
    allowedOperations: ["*"],
    deniedOperations: [],
    allowedRoots: ["G:/agent-zoo/zyra"],
    authProviderId: null,
    protocolVersions: ["2025-06-18"],
    autoInitialize: true,
    autoReconnect: true,
    allowSampling: false,
    allowElicitation: false,
    allowTasks: false,
    metadata: { e04_slice: "04c" },
    configDigest: `sha256:e04-config-${serverId}`,
    createdAt: instant,
    updatedAt: instant,
  };
}

function connections(
  factory: (serverId: string) => FixtureMcpTransport,
  snapshot: ReturnType<McpConnectionRuntime["snapshot"]> | null = null,
): McpConnectionRuntime {
  return new McpConnectionRuntime({
    policy: mcpPolicy(),
    sessionId: "e04-mcp-session",
    workspaceRoot: "G:/agent-zoo/zyra",
    interactive: true,
    sealedAutonomous: false,
    inProcessFactory: (config) => factory(config.serverId),
    now: () => new Date(instant),
    snapshot,
  });
}

test("e04-mcp-lifecycle", async () => {
  const transports: FixtureMcpTransport[] = [];
  const runtime = connections((serverId) => {
    const transport = new FixtureMcpTransport(serverId);
    transports.push(transport);
    return transport;
  });
  const connected = await runtime.connect(mcpConfig());
  expect(connected.record.phase).toBe("ready");
  expect(connected.record.initialized).toBe(true);
  expect(connected.record.metadata.source_custody).toContain("connectToServer");
  expect(transports[0]?.calls[0]).toBe("initialize");
  expect(transports[0]?.initializedNotifications).toBe(1);
  const closed = await runtime.disconnect("e04-server", "e04-test");
  expect(closed.phase).toBe("closed");
});

test("e04-mcp-connect-failure", async () => {
  let transportEpoch = 0;
  const runtime = connections((serverId) => {
    transportEpoch += 1;
    return new FixtureMcpTransport(serverId, transportEpoch === 1 ? 1 : 0, transportEpoch);
  });
  await expect(runtime.connect(mcpConfig("e04-failure"))).rejects.toThrow("connection failed");
  const recovered = await runtime.connect(mcpConfig("e04-failure"));
  expect(recovered.record.phase).toBe("ready");
  expect(recovered.record.epoch).toBe(2);
  expect(recovered.transport.health().connectionEpoch).toBe(2);
  expect(runtime.snapshot().transitions.some((transition) => transition.toPhase === "failed")).toBe(true);
});

test("e04-mcp-resume", async () => {
  const original = connections((serverId) => new FixtureMcpTransport(serverId));
  await original.connect(mcpConfig("e04-resume"));
  const snapshot = original.snapshot();
  const restored = connections((serverId) => new FixtureMcpTransport(serverId), snapshot);
  const connected = await restored.connect(mcpConfig("e04-resume"));
  expect(connected.record.phase).toBe("ready");
  expect(restored.snapshot().transitions.length).toBeGreaterThan(snapshot.transitions.length);
});

test("MCP client reconnects a cleared live client before catalog access", async () => {
  let transportEpoch = 0;
  const runtime = connections((serverId) => {
    transportEpoch += 1;
    return new FixtureMcpTransport(serverId, 0, transportEpoch);
  });
  const catalog = new McpCapabilityCatalog({ now: () => new Date(instant) });
  const client = new McpClientRuntime({
    policy: mcpPolicy(),
    connections: runtime,
    catalog,
    journal: new McpRequestJournal({ now: () => new Date(instant) }),
    rateLimits: new McpRateLimitRuntime({ now: () => new Date(instant) }),
    retry: new McpRetryRuntime({ now: () => new Date(instant) }),
    schemas: new McpSchemaRuntime(),
    pagination: new McpPaginationRuntime(),
    instructions: new McpInstructionRuntime({ now: () => new Date(instant) }),
    serverRequests: { handle: async () => undefined } as never,
    tasks: {} as never,
    now: () => new Date(instant),
  });
  await client.connect(mcpConfig("e04-client"));
  await runtime.disconnect("e04-client", "clear-live-client");
  const refreshed = await client.refreshCatalog("e04-client");
  expect(refreshed.serverId).toBe("e04-client");
  const reconnected = runtime.requireConnected("e04-client");
  expect(reconnected.record.phase).toBe("ready");
  expect(reconnected.record.epoch).toBe(2);
  expect(reconnected.transport.health().connectionEpoch).toBe(2);
});

test("e04-mcp-disable", async () => {
  const normal = connections((serverId) => new FixtureMcpTransport(serverId));
  expect((await normal.connect(mcpConfig("e04-disable-normal"))).record.phase).toBe("ready");
  const previous = process.env.ZYRA_DISABLE_E04_MCP_SOURCE_RUNTIME;
  process.env.ZYRA_DISABLE_E04_MCP_SOURCE_RUNTIME = "1";
  try {
    const disabled = connections((serverId) => new FixtureMcpTransport(serverId));
    await expect(disabled.connect(mcpConfig("e04-disable-kill"))).rejects.toThrow("migrated TypeScript MCP source runtime is disabled");
  } finally {
    if (previous === undefined) delete process.env.ZYRA_DISABLE_E04_MCP_SOURCE_RUNTIME;
    else process.env.ZYRA_DISABLE_E04_MCP_SOURCE_RUNTIME = previous;
  }
});
