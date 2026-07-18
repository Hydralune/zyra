import type {
  JsonObject,
  RuntimeRunInput,
  RuntimeRunResult,
} from "../../src/contracts.ts";
import { AgentDefinitionRegistry } from "../../src/agents/definition-registry.ts";
import type { DefinitionInput } from "../../src/agents/definition-registry.ts";
import { TaskIdentityRuntime } from "../../src/tasks/identity-runtime.ts";
import { DurableTaskRegistry } from "../../src/tasks/registry.ts";
import { TaskStateMachine } from "../../src/tasks/state-machine.ts";
import {
  digest,
  effectRequestDigest,
  emptySnapshot,
  sealSnapshot,
  type AgentTaskPhase,
  type E03AgentDefinition,
  type E03CapabilityScope,
  type E03Clock,
  type E03ContextSnapshot,
  type E03EffectReceipt,
  type E03EffectRequest,
  type E03PhysicalPort,
  type E03RegistrySnapshot,
  type E03TaskState,
} from "../../src/e03/contracts.ts";

export class TestClock implements E03Clock {
  private cursor: number;

  constructor(start = "2026-07-18T00:00:00.000Z") {
    this.cursor = Date.parse(start);
  }

  now(): string {
    const value = new Date(this.cursor).toISOString();
    this.cursor += 1_000;
    return value;
  }

  advance(milliseconds: number): string {
    this.cursor += milliseconds;
    return new Date(this.cursor).toISOString();
  }

  peek(): string {
    return new Date(this.cursor).toISOString();
  }
}

export class TestPhysicalPort implements E03PhysicalPort {
  snapshot: E03RegistrySnapshot | null = null;
  readonly effects: E03EffectRequest[] = [];
  readonly receipts: E03EffectReceipt[] = [];
  rejectNextEffect = false;
  rejectNextCas = false;

  constructor(private readonly clock: E03Clock = new TestClock()) {}

  async restore(
    _runId: string,
    _sessionId: string,
  ): Promise<E03RegistrySnapshot | null> {
    return this.snapshot ? structuredClone(this.snapshot) : null;
  }

  async effect(request: E03EffectRequest): Promise<E03EffectReceipt> {
    this.effects.push(structuredClone(request));
    const payload = {
      receiptId: `receipt-${request.effectId}`,
      effectId: request.effectId,
      idempotencyKey: request.idempotencyKey,
      requestDigest: effectRequestDigest(request),
      requestId: request.requestId,
      taskId: request.taskId,
      leaseId: request.leaseId,
      expectedRevision: request.expectedRevision,
      accepted: !this.rejectNextEffect,
      replayed: false,
      result: { operation: request.operation },
      artifacts: [],
      error: this.rejectNextEffect ? "test-effect-rejected" : "",
      completedAt: this.clock.now(),
    };
    this.rejectNextEffect = false;
    const receipt = { ...payload, digest: digest(payload) };
    this.receipts.push(receipt);
    return structuredClone(receipt);
  }

  async compareAndSwap(
    expectedRevision: number,
    snapshot: E03RegistrySnapshot,
  ): Promise<{
    accepted: boolean;
    revision: number;
    replayed: boolean;
    error: string;
  }> {
    const currentRevision = this.snapshot?.revision ?? 0;
    if (this.rejectNextCas || expectedRevision !== currentRevision) {
      this.rejectNextCas = false;
      return {
        accepted: false,
        revision: currentRevision,
        replayed: false,
        error: "stale_revision",
      };
    }
    this.snapshot = structuredClone(snapshot);
    return {
      accepted: true,
      revision: snapshot.revision,
      replayed: false,
      error: "",
    };
  }
}

export function definition(
  name = "general",
  overrides: Partial<E03AgentDefinition> = {},
): E03AgentDefinition {
  const registry = new AgentDefinitionRegistry();
  const value = registry.register({
    name,
    description: `Definition for ${name}`,
    version: overrides.version ?? "1",
    source: overrides.source ?? "project",
    model: overrides.model ?? "inherit",
    effort: overrides.effort ?? "inherit",
    systemPrompt: overrides.systemPrompt ?? "Operate deterministically.",
    tools: overrides.tools ?? ["read", "write"],
    deniedTools: overrides.deniedTools ?? [],
    skills: overrides.skills ?? ["analysis"],
    mcpServers: overrides.mcpServers ?? ["local"],
    permissionMode: overrides.permissionMode ?? "default",
    isolation: overrides.isolation ?? "workspace",
    background: overrides.background ?? true,
    memoryScope: overrides.memoryScope ?? "task",
    budget: overrides.budget ?? {
      maxTurns: 12,
      maxToolCalls: 48,
      maxInputTokens: 64_000,
      maxOutputTokens: 16_000,
      maxResultChars: 120_000,
      maxWallTimeMs: 900_000,
      maxChildren: 8,
      maxDepth: 4,
      maxConcurrency: 4,
      startedAt: "2026-07-18T00:00:00.000Z",
      deadlineAt: "2026-07-20T00:00:00.000Z",
    },
    metadata: overrides.metadata ?? {},
  } as unknown as DefinitionInput);
  return value;
}

export function scope(
  overrides: Partial<E03CapabilityScope> = {},
): E03CapabilityScope {
  const payload = {
    tools: overrides.tools ?? ["read", "write"],
    deniedTools: overrides.deniedTools ?? [],
    skills: overrides.skills ?? ["analysis"],
    mcpServers: overrides.mcpServers ?? ["local"],
    permissionMode: overrides.permissionMode ?? "default",
    permissionCeilingDigest:
      overrides.permissionCeilingDigest ?? digest("permission-ceiling"),
    workspaceRoots: overrides.workspaceRoots ?? [process.cwd()],
    isolationModes: overrides.isolationModes ?? [
      "workspace" as const,
      "worktree" as const,
      "sandbox" as const,
    ],
    allowBackground: overrides.allowBackground ?? true,
    allowTeamMessaging: overrides.allowTeamMessaging ?? true,
    allowFanout: overrides.allowFanout ?? true,
    allowKill: overrides.allowKill ?? true,
    maxDepth: overrides.maxDepth ?? 4,
    maxChildren: overrides.maxChildren ?? 8,
  };
  return { ...payload, digest: digest(payload) };
}

export function context(
  taskId: string,
  sessionId = `session-${taskId}`,
  capabilityScope = scope(),
  overrides: Partial<E03ContextSnapshot> = {},
): E03ContextSnapshot {
  const payload = {
    snapshotId: overrides.snapshotId ?? `context-${taskId}`,
    parentSnapshotId: overrides.parentSnapshotId ?? null,
    sessionId,
    taskId,
    branchId: overrides.branchId ?? `branch-${taskId}`,
    sequence: overrides.sequence ?? 0,
    messageRefs: overrides.messageRefs ?? [],
    artifactRefs: overrides.artifactRefs ?? [],
    evidenceRefs: overrides.evidenceRefs ?? [],
    memoryRefs: overrides.memoryRefs ?? [],
    compactBoundaryIds: overrides.compactBoundaryIds ?? [],
    permissionDigest: capabilityScope.permissionCeilingDigest,
    toolCatalogDigest: digest(capabilityScope.tools),
    topologyRevision: overrides.topologyRevision ?? 1,
    createdAt: overrides.createdAt ?? "2026-07-18T00:00:00.000Z",
  };
  return { ...payload, checksum: digest(payload) };
}

export function task(
  taskId: string,
  options: {
    runId?: string;
    sessionId?: string;
    parentTaskId?: string;
    parentSessionId?: string;
    definition?: E03AgentDefinition;
    scope?: E03CapabilityScope;
    status?: AgentTaskPhase;
    fanoutKey?: string;
  } = {},
): E03TaskState {
  const machine = new TaskStateMachine(new TestClock());
  const identities = new TaskIdentityRuntime();
  const capabilityScope = options.scope ?? scope();
  const agentDefinition =
    options.definition ??
    definition(`agent-${taskId}`, {
      metadata: options.fanoutKey ? { fanout_key: options.fanoutKey } : {},
    });
  const identity = identities.allocate({
    runId: options.runId ?? "run-e03",
    sessionId: options.sessionId ?? `session-${taskId}`,
    taskId,
    parentTaskId: options.parentTaskId ?? "parent-task",
    parentSessionId: options.parentSessionId ?? "parent-session",
    idempotencyKey: `identity-${taskId}`,
  });
  let value = machine.create({
    identity,
    definition: agentDefinition,
    scope: capabilityScope,
    context: context(identity.taskId, identity.sessionId, capabilityScope),
    prompt: `Execute ${taskId}`,
    executionMode: "foreground",
  });
  const target = options.status ?? "created";
  const path: AgentTaskPhase[] = [];
  if (["queued", "running", "waiting", "completed", "failed"].includes(target))
    path.push("queued");
  if (["running", "waiting", "completed", "failed"].includes(target))
    path.push("running");
  if (target === "waiting") path.push("waiting");
  if (target === "completed") path.push("completed");
  if (target === "failed") path.push("failed");
  if (target === "cancelled") path.push("cancelled");
  if (target === "killed") path.push("killed");
  for (const phase of path) {
    const transition = machine.transition(value, phase, {
      requestId: `${taskId}-${phase}`,
      idempotencyKey: `${taskId}-${phase}`,
      writerId: "typescript.E03AgentControlCoordinator",
      expectedRevision: value.revision,
      eventType: `test_${phase}`,
      result: phase === "completed" ? { ok: true } : undefined,
      error:
        phase === "failed" || phase === "cancelled" || phase === "killed"
          ? `test-${phase}`
          : undefined,
    });
    value = transition.state;
  }
  return value;
}

export function registry(
  clock = new TestClock(),
  port = new TestPhysicalPort(clock),
): { registry: DurableTaskRegistry; port: TestPhysicalPort } {
  const value = new DurableTaskRegistry(emptySnapshot(clock), port, clock);
  return { registry: value, port };
}

export async function commitTask(
  durable: DurableTaskRegistry,
  value: E03TaskState,
  key = `commit-${value.identity.taskId}`,
): Promise<E03TaskState> {
  durable.prepare({
    requestId: key,
    idempotencyKey: key,
    writerId: "typescript.E03AgentControlCoordinator",
    taskId: value.identity.taskId,
    expectedRevision: 0,
    proposed: value,
  });
  return (await durable.commit(key, null)).state;
}

export function runInput(taskId = "parent-task"): RuntimeRunInput {
  return {
    runId: "run-e03",
    taskId,
    nodeId: "node-e03",
    workerRequestId: `worker-${taskId}`,
    sessionId: "parent-session",
    messages: [{ role: "user", content: "execute child" }],
    turns: [],
    tools: [],
    config: {
      permissionPolicy: { mode: "default", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot: process.cwd(),
        agentBudget: {
          maxTurns: 12,
          maxToolCalls: 48,
          maxInputTokens: 64_000,
          maxOutputTokens: 16_000,
          maxResultChars: 120_000,
          maxWallTimeMs: 900_000,
          maxChildren: 8,
          maxDepth: 4,
        },
      },
    },
  };
}

export function successfulRunResult(
  output: JsonObject = { value: "ok" },
): RuntimeRunResult {
  return {
    ok: true,
    stoppedReason: null,
    turnCount: 1,
    toolCallCount: 0,
    contextCompactionCount: 0,
    stepSummaries: ["child completed"],
    artifacts: [],
    sessionSnapshot: output,
    metadata: {
      input_tokens: "10",
      output_tokens: "5",
      wall_time_ms: "2",
    },
  };
}

export function snapshotWithTask(value: E03TaskState): E03RegistrySnapshot {
  return sealSnapshot({
    ...emptySnapshot(new TestClock()),
    revision: 1,
    tasks: { [value.identity.taskId]: value },
    writerLeases: {
      [value.identity.leaseId]: "typescript.E03AgentControlCoordinator",
    },
  });
}
