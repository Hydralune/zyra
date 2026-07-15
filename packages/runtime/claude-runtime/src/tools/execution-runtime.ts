import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const TOOL_EXECUTION_SNAPSHOT_VERSION = "zyra.tool-execution/v1";

export type ToolEffect = "read" | "write" | "network" | "process" | "control";
export type ToolRisk = "low" | "medium" | "high" | "unknown";
export type ToolCallState =
  | "registered"
  | "validated"
  | "permission_pending"
  | "queued"
  | "leased"
  | "running"
  | "streaming"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "quarantined";

export interface ToolRuntimeSpec {
  name: string;
  namespace: string;
  version: string;
  description: string;
  inputSchema: JsonObject;
  effects: readonly ToolEffect[];
  risk: ToolRisk;
  readOnly: boolean;
  supportsStreaming: boolean;
  supportsCancellation: boolean;
  idempotent: boolean;
  maximumResultChars: number;
  timeoutMs: number;
  concurrencyKey: string;
  metadata: JsonObject;
  schemaDigest: string;
}

export interface ToolInvocation {
  callId: string;
  sessionId: string;
  runId: string;
  taskId: string;
  turnId: string;
  toolName: string;
  arguments: JsonObject;
  argumentsDigest: string;
  idempotencyKey: string;
  state: ToolCallState;
  permissionDecisionId: string | null;
  permissionGranted: boolean | null;
  leaseId: string | null;
  leaseOwner: string | null;
  leaseExpiresAt: string | null;
  attempt: number;
  streamSequence: number;
  result: JsonValue;
  resultDigest: string | null;
  resultSummary: string | null;
  resultChars: number;
  originalResultChars: number;
  truncated: boolean;
  errorCode: string | null;
  errorMessage: string | null;
  createdAt: string;
  startedAt: string | null;
  completedAt: string | null;
  revision: number;
}

export interface ToolStreamChunk {
  chunkId: string;
  callId: string;
  sequence: number;
  channel: "stdout" | "stderr" | "progress" | "result";
  content: JsonValue;
  contentDigest: string;
  effective: boolean;
  createdAt: string;
}

export interface ToolBatch {
  batchId: string;
  turnId: string;
  callIds: string[];
  concurrencyKeys: string[];
  readOnly: boolean;
  maximumConcurrency: number;
  scheduledAt: string;
}

export interface ToolResultBudget {
  perCallChars: number;
  perTurnChars: number | null;
  perQueryChars: number | null;
  consumedByTurn: Record<string, number>;
  consumedByQuery: number;
  overflowArtifactThreshold: number;
}

export interface ToolLease {
  leaseId: string;
  callId: string;
  owner: string;
  acquiredAt: string;
  expiresAt: string;
  releasedAt: string | null;
  revision: number;
}

export interface ToolExecutionSnapshot {
  version: typeof TOOL_EXECUTION_SNAPSHOT_VERSION;
  revision: number;
  sequence: number;
  restartEpoch: number;
  specs: ToolRuntimeSpec[];
  calls: ToolInvocation[];
  chunks: ToolStreamChunk[];
  batches: ToolBatch[];
  leases: ToolLease[];
  budget: ToolResultBudget;
  completedIdempotency: Record<string, string>;
  checksum: string;
}

export interface ToolValidationResult {
  valid: boolean;
  errors: string[];
  normalizedArguments: JsonObject;
  argumentsDigest: string;
}

export interface ToolPermissionRequest {
  permissionId: string;
  callId: string;
  toolName: string;
  risk: ToolRisk;
  effects: readonly ToolEffect[];
  argumentsDigest: string;
  reason: string;
  createdAt: string;
}

export class ToolExecutionRuntime {
  private readonly specs = new Map<string, ToolRuntimeSpec>();
  private readonly calls = new Map<string, ToolInvocation>();
  private readonly chunks = new Map<string, ToolStreamChunk[]>();
  private readonly batches: ToolBatch[] = [];
  private readonly leases = new Map<string, ToolLease>();
  private readonly completedIdempotency = new Map<string, string>();
  private budget: ToolResultBudget;
  private revision = 0;
  private sequence = 0;
  private restartEpoch = 0;

  constructor(options: {
    perCallChars?: number;
    perTurnChars?: number | null;
    perQueryChars?: number | null;
    overflowArtifactThreshold?: number;
  } = {}) {
    this.budget = {
      perCallChars: positive(options.perCallChars, 8_000),
      perTurnChars: nullablePositive(options.perTurnChars),
      perQueryChars: nullablePositive(options.perQueryChars),
      consumedByTurn: {},
      consumedByQuery: 0,
      overflowArtifactThreshold: positive(options.overflowArtifactThreshold, 32_000),
    };
  }

  registry_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "list");
    if (action === "register") return specToJson(this.register(specFromJson(asObject(value.spec))));
    if (action === "remove") return { removed: this.remove(asString(value.name)) };
    if (action === "get") return specToJson(this.requireSpec(asString(value.name)));
    return { tools: [...this.specs.values()].map(specToJson), digest: this.registryDigest() };
  }

  execute_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "create") return callToJson(this.createInvocation(invocationInputFromJson(value)));
    if (action === "lease") return leaseToJson(this.acquireLease(asString(value.call_id), asString(value.owner), positive(value.ttl_ms, 30_000)));
    if (action === "start") return callToJson(this.start(asString(value.call_id), asString(value.lease_id)));
    if (action === "settle") {
      return callToJson(this.settle(asString(value.call_id), {
        ok: asBoolean(value.ok),
        result: value.result ?? null,
        summary: asString(value.summary),
        errorCode: asString(value.error_code) || null,
        errorMessage: asString(value.error_message) || null,
      }));
    }
    return callToJson(this.requireCall(asString(value.call_id)));
  }

  stream_module(value: JsonObject): JsonObject {
    const chunk = this.appendChunk(asString(value.call_id), {
      sequence: integer(value.sequence, -1),
      channel: streamChannel(asString(value.channel, "progress")),
      content: value.content ?? null,
      effective: asBoolean(value.effective, false),
      createdAt: asString(value.created_at) || undefined,
    });
    return chunkToJson(chunk);
  }

  budget_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "configure") {
      this.configureBudget({
        perCallChars: value.per_call_chars === undefined ? undefined : positive(value.per_call_chars, this.budget.perCallChars),
        perTurnChars: value.per_turn_chars === undefined ? undefined : nullablePositive(numberOrNull(value.per_turn_chars)),
        perQueryChars: value.per_query_chars === undefined ? undefined : nullablePositive(numberOrNull(value.per_query_chars)),
        overflowArtifactThreshold: value.overflow_artifact_threshold === undefined ? undefined : positive(value.overflow_artifact_threshold, this.budget.overflowArtifactThreshold),
      });
    }
    return budgetToJson(this.budget);
  }

  register(value: Omit<ToolRuntimeSpec, "schemaDigest"> & { schemaDigest?: string }): ToolRuntimeSpec {
    const name = normalizeToolName(value.name);
    if (this.specs.has(name)) throw new Error(`tool already registered: ${name}`);
    const inputSchema = normalizeSchema(value.inputSchema);
    const spec: ToolRuntimeSpec = Object.freeze({
      name,
      namespace: normalizeNamespace(value.namespace),
      version: required(value.version, "tool version"),
      description: value.description.trim().slice(0, 8_000),
      inputSchema,
      effects: Object.freeze([...new Set(value.effects.map(toolEffect))]),
      risk: toolRisk(value.risk),
      readOnly: value.readOnly,
      supportsStreaming: value.supportsStreaming,
      supportsCancellation: value.supportsCancellation,
      idempotent: value.idempotent,
      maximumResultChars: positive(value.maximumResultChars, this.budget.perCallChars),
      timeoutMs: positive(value.timeoutMs, 120_000),
      concurrencyKey: value.concurrencyKey.trim() || `${value.namespace}:${name}`,
      metadata: asObject(value.metadata),
      schemaDigest: value.schemaDigest ?? digest(inputSchema),
    });
    if (spec.readOnly && spec.effects.some((effect) => effect !== "read")) {
      throw new Error(`read-only tool ${name} declares non-read effects`);
    }
    this.specs.set(name, spec);
    this.revision += 1;
    return structuredClone(spec);
  }

  remove(name: string): boolean {
    const normalized = normalizeToolName(name);
    if ([...this.calls.values()].some((call) => call.toolName === normalized && !isTerminal(call.state))) {
      throw new Error(`cannot remove tool with active calls: ${normalized}`);
    }
    const removed = this.specs.delete(normalized);
    if (removed) this.revision += 1;
    return removed;
  }

  validate(toolName: string, argumentsValue: JsonObject): ToolValidationResult {
    const spec = this.requireSpec(toolName);
    const normalizedArguments = sortJson(asObject(argumentsValue)) as JsonObject;
    const errors = validateAgainstSchema(normalizedArguments, spec.inputSchema, "$");
    return {
      valid: errors.length === 0,
      errors,
      normalizedArguments,
      argumentsDigest: digest(normalizedArguments),
    };
  }

  createInvocation(input: {
    callId?: string;
    sessionId: string;
    runId: string;
    taskId: string;
    turnId: string;
    toolName: string;
    arguments: JsonObject;
    idempotencyKey: string;
  }): ToolInvocation {
    const callId = input.callId?.trim() || randomUUID();
    if (this.calls.has(callId)) throw new Error(`tool call already exists: ${callId}`);
    const duplicateId = this.completedIdempotency.get(input.idempotencyKey);
    if (duplicateId) return structuredClone(this.requireCall(duplicateId));
    const spec = this.requireSpec(input.toolName);
    const validation = this.validate(spec.name, input.arguments);
    if (!validation.valid) throw new Error(`tool arguments invalid: ${validation.errors.join("; ")}`);
    const call: ToolInvocation = {
      callId,
      sessionId: required(input.sessionId, "session id"),
      runId: required(input.runId, "run id"),
      taskId: required(input.taskId, "task id"),
      turnId: required(input.turnId, "turn id"),
      toolName: spec.name,
      arguments: validation.normalizedArguments,
      argumentsDigest: validation.argumentsDigest,
      idempotencyKey: required(input.idempotencyKey, "idempotency key"),
      state: "validated",
      permissionDecisionId: null,
      permissionGranted: null,
      leaseId: null,
      leaseOwner: null,
      leaseExpiresAt: null,
      attempt: 0,
      streamSequence: 0,
      result: null,
      resultDigest: null,
      resultSummary: null,
      resultChars: 0,
      originalResultChars: 0,
      truncated: false,
      errorCode: null,
      errorMessage: null,
      createdAt: new Date().toISOString(),
      startedAt: null,
      completedAt: null,
      revision: 1,
    };
    this.calls.set(callId, call);
    this.revision += 1;
    return structuredClone(call);
  }

  permissionRequest(callId: string, reason = "tool_effect_requires_policy"): ToolPermissionRequest {
    const call = this.requireCall(callId);
    const spec = this.requireSpec(call.toolName);
    if (call.state !== "validated") throw new Error(`tool permission cannot be requested from ${call.state}`);
    call.state = "permission_pending";
    call.revision += 1;
    this.revision += 1;
    return {
      permissionId: randomUUID(),
      callId,
      toolName: spec.name,
      risk: spec.risk,
      effects: spec.effects,
      argumentsDigest: call.argumentsDigest,
      reason,
      createdAt: new Date().toISOString(),
    };
  }

  resolvePermission(callId: string, decisionId: string, granted: boolean): ToolInvocation {
    const call = this.requireCall(callId);
    if (call.state !== "permission_pending" && call.state !== "validated") {
      throw new Error(`tool permission cannot resolve from ${call.state}`);
    }
    call.permissionDecisionId = required(decisionId, "permission decision id");
    call.permissionGranted = granted;
    if (granted) call.state = "queued";
    else {
      call.state = "failed";
      call.errorCode = "permission_denied";
      call.errorMessage = "tool execution denied by permission runtime";
      call.completedAt = new Date().toISOString();
      this.completedIdempotency.set(call.idempotencyKey, call.callId);
    }
    call.revision += 1;
    this.revision += 1;
    return structuredClone(call);
  }

  queueWithoutPermission(callId: string): ToolInvocation {
    const call = this.requireCall(callId);
    const spec = this.requireSpec(call.toolName);
    if (spec.risk !== "low" && !spec.readOnly) throw new Error(`tool ${spec.name} requires a permission decision`);
    if (call.state !== "validated") throw new Error(`tool cannot queue from ${call.state}`);
    call.permissionGranted = true;
    call.state = "queued";
    call.revision += 1;
    this.revision += 1;
    return structuredClone(call);
  }

  queueWithDelegatedPermission(callId: string, decisionId: string): ToolInvocation {
    const call = this.requireCall(callId);
    if (call.state !== "validated") throw new Error(`tool cannot delegate permission from ${call.state}`);
    call.permissionDecisionId = required(decisionId, "delegated permission decision id");
    call.permissionGranted = null;
    call.state = "queued";
    call.revision += 1;
    this.revision += 1;
    return structuredClone(call);
  }

  schedule(turnId: string, callIds: readonly string[], maximumConcurrency = 10): ToolBatch[] {
    const calls = callIds.map((id) => this.requireCall(id));
    if (calls.some((call) => call.turnId !== turnId)) throw new Error("tool batch spans multiple turns");
    if (calls.some((call) => call.state !== "queued")) throw new Error("tool batch contains unqueued call");
    const readOnly: ToolInvocation[] = [];
    const serialGroups = new Map<string, ToolInvocation[]>();
    for (const call of calls) {
      const spec = this.requireSpec(call.toolName);
      if (spec.readOnly) readOnly.push(call);
      else {
        const group = serialGroups.get(spec.concurrencyKey) ?? [];
        group.push(call);
        serialGroups.set(spec.concurrencyKey, group);
      }
    }
    const result: ToolBatch[] = [];
    const concurrency = Math.max(1, Math.floor(maximumConcurrency));
    for (let index = 0; index < readOnly.length; index += concurrency) {
      const slice = readOnly.slice(index, index + concurrency);
      result.push(createBatch(turnId, slice, concurrency, true, this.specs));
    }
    const maximumSerialDepth = Math.max(0, ...[...serialGroups.values()].map((items) => items.length));
    for (let depth = 0; depth < maximumSerialDepth; depth += 1) {
      const slice = [...serialGroups.values()].map((items) => items[depth]).filter(Boolean);
      if (slice.length > 0) result.push(createBatch(turnId, slice, Math.min(concurrency, slice.length), false, this.specs));
    }
    this.batches.push(...result);
    this.revision += 1;
    return structuredClone(result);
  }

  acquireLease(callId: string, owner: string, ttlMs = 30_000): ToolLease {
    const call = this.requireCall(callId);
    if (call.state !== "queued" && call.state !== "leased") throw new Error(`tool cannot lease from ${call.state}`);
    if (call.leaseId) {
      const existing = this.leases.get(call.leaseId);
      if (existing && existing.releasedAt === null && Date.parse(existing.expiresAt) > Date.now()) {
        if (existing.owner !== owner) throw new Error(`tool lease already held by ${existing.owner}`);
        return structuredClone(existing);
      }
    }
    const acquiredAt = new Date().toISOString();
    const lease: ToolLease = {
      leaseId: randomUUID(),
      callId,
      owner: required(owner, "lease owner"),
      acquiredAt,
      expiresAt: new Date(Date.parse(acquiredAt) + positive(ttlMs, 30_000)).toISOString(),
      releasedAt: null,
      revision: 1,
    };
    this.leases.set(lease.leaseId, lease);
    call.leaseId = lease.leaseId;
    call.leaseOwner = lease.owner;
    call.leaseExpiresAt = lease.expiresAt;
    call.state = "leased";
    call.revision += 1;
    this.revision += 1;
    return structuredClone(lease);
  }

  renewLease(leaseId: string, owner: string, ttlMs = 30_000): ToolLease {
    const lease = this.requireLease(leaseId);
    if (lease.owner !== owner) throw new Error("tool lease owner mismatch");
    if (lease.releasedAt !== null) throw new Error("tool lease already released");
    if (Date.parse(lease.expiresAt) <= Date.now()) throw new Error("tool lease expired");
    lease.expiresAt = new Date(Date.now() + positive(ttlMs, 30_000)).toISOString();
    lease.revision += 1;
    const call = this.requireCall(lease.callId);
    call.leaseExpiresAt = lease.expiresAt;
    call.revision += 1;
    this.revision += 1;
    return structuredClone(lease);
  }

  start(callId: string, leaseId: string): ToolInvocation {
    const call = this.requireCall(callId);
    const lease = this.requireLease(leaseId);
    if (lease.callId !== callId || call.leaseId !== leaseId) throw new Error("tool lease does not belong to call");
    if (lease.releasedAt !== null || Date.parse(lease.expiresAt) <= Date.now()) throw new Error("tool lease is inactive");
    if (call.state !== "leased") throw new Error(`tool cannot start from ${call.state}`);
    call.state = "running";
    call.attempt += 1;
    call.startedAt = new Date().toISOString();
    call.revision += 1;
    this.revision += 1;
    return structuredClone(call);
  }

  appendChunk(callId: string, input: {
    sequence: number;
    channel: ToolStreamChunk["channel"];
    content: JsonValue;
    effective: boolean;
    createdAt?: string;
  }): ToolStreamChunk {
    const call = this.requireCall(callId);
    const spec = this.requireSpec(call.toolName);
    if (!spec.supportsStreaming) throw new Error(`tool does not support streaming: ${spec.name}`);
    if (call.state !== "running" && call.state !== "streaming") throw new Error(`tool cannot stream from ${call.state}`);
    const expected = call.streamSequence + 1;
    const sequence = input.sequence < 0 ? expected : input.sequence;
    if (sequence !== expected) throw new Error(`tool stream sequence gap: expected ${expected}, received ${sequence}`);
    const chunk: ToolStreamChunk = {
      chunkId: randomUUID(),
      callId,
      sequence,
      channel: input.channel,
      content: structuredClone(input.content),
      contentDigest: digest(input.content),
      effective: input.effective,
      createdAt: normalizeTimestamp(input.createdAt),
    };
    const chunks = this.chunks.get(callId) ?? [];
    chunks.push(chunk);
    this.chunks.set(callId, chunks);
    call.streamSequence = sequence;
    call.state = "streaming";
    call.revision += 1;
    this.sequence += 1;
    this.revision += 1;
    return structuredClone(chunk);
  }

  settle(callId: string, input: {
    ok: boolean;
    result: JsonValue;
    summary: string;
    errorCode: string | null;
    errorMessage: string | null;
  }): ToolInvocation {
    const call = this.requireCall(callId);
    if (isTerminal(call.state)) {
      const incomingDigest = digest(input.result);
      if (call.resultDigest !== incomingDigest) throw new Error(`conflicting late tool result: ${callId}`);
      return structuredClone(call);
    }
    if (call.state !== "running" && call.state !== "streaming") throw new Error(`tool cannot settle from ${call.state}`);
    const budgeted = this.applyResultBudget(call, input.result);
    call.result = budgeted.result;
    call.resultDigest = digest(input.result);
    call.resultSummary = input.summary.trim().slice(0, 4_096);
    call.resultChars = budgeted.resultChars;
    call.originalResultChars = budgeted.originalChars;
    call.truncated = budgeted.truncated;
    call.errorCode = input.ok ? null : (input.errorCode ?? "tool_failed");
    call.errorMessage = input.ok ? null : (input.errorMessage ?? "tool execution failed").trim().slice(0, 8_192);
    call.state = input.ok ? "succeeded" : "failed";
    call.completedAt = new Date().toISOString();
    call.revision += 1;
    this.completedIdempotency.set(call.idempotencyKey, call.callId);
    if (call.leaseId) this.releaseLease(call.leaseId, call.leaseOwner ?? "unknown");
    this.revision += 1;
    return structuredClone(call);
  }

  cancel(callId: string, reason: string): ToolInvocation {
    const call = this.requireCall(callId);
    const spec = this.requireSpec(call.toolName);
    if (isTerminal(call.state)) return structuredClone(call);
    if (call.state === "running" || call.state === "streaming") {
      if (!spec.supportsCancellation) throw new Error(`tool does not support cancellation: ${spec.name}`);
    }
    call.state = "cancelled";
    call.errorCode = "cancelled";
    call.errorMessage = reason.trim().slice(0, 4_096);
    call.completedAt = new Date().toISOString();
    call.revision += 1;
    this.completedIdempotency.set(call.idempotencyKey, call.callId);
    if (call.leaseId) this.releaseLease(call.leaseId, call.leaseOwner ?? "unknown");
    this.revision += 1;
    return structuredClone(call);
  }

  rejectLateResult(callId: string, result: JsonValue): void {
    const call = this.requireCall(callId);
    if (!isTerminal(call.state)) throw new Error("late-result fence called for nonterminal tool");
    if (call.resultDigest === digest(result)) return;
    call.state = "quarantined";
    call.errorCode = "late_result_conflict";
    call.errorMessage = "late tool result conflicted with committed result";
    call.revision += 1;
    this.revision += 1;
    throw new Error(`late tool result quarantined: ${callId}`);
  }

  configureBudget(value: Partial<Pick<ToolResultBudget, "perCallChars" | "perTurnChars" | "perQueryChars" | "overflowArtifactThreshold">>): void {
    this.budget = {
      ...this.budget,
      perCallChars: value.perCallChars ?? this.budget.perCallChars,
      perTurnChars: value.perTurnChars === undefined ? this.budget.perTurnChars : value.perTurnChars,
      perQueryChars: value.perQueryChars === undefined ? this.budget.perQueryChars : value.perQueryChars,
      overflowArtifactThreshold: value.overflowArtifactThreshold ?? this.budget.overflowArtifactThreshold,
    };
    this.revision += 1;
  }

  registryDigest(): string {
    return digest([...this.specs.values()].sort((left, right) => left.name.localeCompare(right.name)).map(specToJson));
  }

  snapshot(): ToolExecutionSnapshot {
    const unsigned: Omit<ToolExecutionSnapshot, "checksum"> = {
      version: TOOL_EXECUTION_SNAPSHOT_VERSION,
      revision: this.revision,
      sequence: this.sequence,
      restartEpoch: this.restartEpoch,
      specs: structuredClone([...this.specs.values()]),
      calls: structuredClone([...this.calls.values()]),
      chunks: structuredClone([...this.chunks.values()].flat()),
      batches: structuredClone(this.batches),
      leases: structuredClone([...this.leases.values()]),
      budget: structuredClone(this.budget),
      completedIdempotency: Object.fromEntries(this.completedIdempotency),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: ToolExecutionSnapshot): void {
    if (snapshot.version !== TOOL_EXECUTION_SNAPSHOT_VERSION) throw new Error("unsupported tool execution snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("tool execution snapshot checksum mismatch");
    validateSnapshot(snapshot);
    this.specs.clear();
    for (const spec of snapshot.specs) this.specs.set(spec.name, Object.freeze(structuredClone(spec)));
    this.calls.clear();
    for (const call of snapshot.calls) this.calls.set(call.callId, structuredClone(call));
    this.chunks.clear();
    for (const chunk of snapshot.chunks) {
      const list = this.chunks.get(chunk.callId) ?? [];
      list.push(structuredClone(chunk));
      this.chunks.set(chunk.callId, list);
    }
    this.batches.splice(0, this.batches.length, ...structuredClone(snapshot.batches));
    this.leases.clear();
    for (const lease of snapshot.leases) this.leases.set(lease.leaseId, structuredClone(lease));
    this.budget = structuredClone(snapshot.budget);
    this.completedIdempotency.clear();
    for (const [key, value] of Object.entries(snapshot.completedIdempotency)) this.completedIdempotency.set(key, value);
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    this.restartEpoch = snapshot.restartEpoch + 1;
    this.recoverExpiredLeases();
  }

  private applyResultBudget(call: ToolInvocation, result: JsonValue): {
    result: JsonValue;
    resultChars: number;
    originalChars: number;
    truncated: boolean;
  } {
    const spec = this.requireSpec(call.toolName);
    const serialized = canonicalJson(result);
    const originalChars = serialized.length;
    const turnRemaining = this.budget.perTurnChars === null
      ? Number.MAX_SAFE_INTEGER
      : Math.max(0, this.budget.perTurnChars - (this.budget.consumedByTurn[call.turnId] ?? 0));
    const queryRemaining = this.budget.perQueryChars === null
      ? Number.MAX_SAFE_INTEGER
      : Math.max(0, this.budget.perQueryChars - this.budget.consumedByQuery);
    const limit = Math.max(0, Math.min(this.budget.perCallChars, spec.maximumResultChars, turnRemaining, queryRemaining));
    let budgeted: JsonValue = result;
    let truncated = false;
    if (originalChars > limit) {
      truncated = true;
      const prefixLength = Math.max(0, limit - 256);
      budgeted = {
        truncated: true,
        original_chars: originalChars,
        retained_chars: prefixLength,
        content_prefix: serialized.slice(0, prefixLength),
        overflow_digest: digest(result),
        artifact_recommended: originalChars >= this.budget.overflowArtifactThreshold,
      };
    }
    const resultChars = canonicalJson(budgeted).length;
    this.budget.consumedByTurn[call.turnId] = (this.budget.consumedByTurn[call.turnId] ?? 0) + resultChars;
    this.budget.consumedByQuery += resultChars;
    return { result: budgeted, resultChars, originalChars, truncated };
  }

  private releaseLease(leaseId: string, owner: string): void {
    const lease = this.requireLease(leaseId);
    if (lease.owner !== owner) throw new Error("tool lease release owner mismatch");
    if (lease.releasedAt === null) {
      lease.releasedAt = new Date().toISOString();
      lease.revision += 1;
    }
  }

  private recoverExpiredLeases(): void {
    const now = Date.now();
    for (const lease of this.leases.values()) {
      if (lease.releasedAt !== null || Date.parse(lease.expiresAt) > now) continue;
      lease.releasedAt = new Date(now).toISOString();
      lease.revision += 1;
      const call = this.calls.get(lease.callId);
      if (!call || isTerminal(call.state)) continue;
      call.state = "queued";
      call.leaseId = null;
      call.leaseOwner = null;
      call.leaseExpiresAt = null;
      call.revision += 1;
    }
  }

  private requireSpec(name: string): ToolRuntimeSpec {
    const spec = this.specs.get(normalizeToolName(name));
    if (!spec) throw new Error(`tool is not registered: ${name}`);
    return spec;
  }

  private requireCall(callId: string): ToolInvocation {
    const call = this.calls.get(callId);
    if (!call) throw new Error(`tool call not found: ${callId}`);
    return call;
  }

  private requireLease(leaseId: string): ToolLease {
    const lease = this.leases.get(leaseId);
    if (!lease) throw new Error(`tool lease not found: ${leaseId}`);
    return lease;
  }
}

function createBatch(
  turnId: string,
  calls: readonly ToolInvocation[],
  maximumConcurrency: number,
  readOnly: boolean,
  specs: ReadonlyMap<string, ToolRuntimeSpec>,
): ToolBatch {
  return {
    batchId: randomUUID(),
    turnId,
    callIds: calls.map((call) => call.callId),
    concurrencyKeys: [...new Set(calls.map((call) => specs.get(call.toolName)?.concurrencyKey ?? call.toolName))],
    readOnly,
    maximumConcurrency,
    scheduledAt: new Date().toISOString(),
  };
}

function validateAgainstSchema(value: JsonValue, schema: JsonObject, path: string): string[] {
  const errors: string[] = [];
  const type = asString(schema.type);
  if (type === "object") {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [`${path} must be an object`];
    const record = value as JsonObject;
    const properties = asObject(schema.properties);
    const requiredNames = Array.isArray(schema.required) ? schema.required.map(String) : [];
    for (const name of requiredNames) if (record[name] === undefined) errors.push(`${path}.${name} is required`);
    if (schema.additionalProperties === false) {
      for (const name of Object.keys(record)) if (properties[name] === undefined) errors.push(`${path}.${name} is not allowed`);
    }
    for (const [name, propertySchema] of Object.entries(properties)) {
      if (record[name] === undefined) continue;
      errors.push(...validateAgainstSchema(record[name], asObject(propertySchema), `${path}.${name}`));
    }
  } else if (type === "array") {
    if (!Array.isArray(value)) return [`${path} must be an array`];
    const minimum = integer(schema.minItems, 0);
    const maximum = schema.maxItems === undefined ? Number.MAX_SAFE_INTEGER : integer(schema.maxItems, Number.MAX_SAFE_INTEGER);
    if (value.length < minimum) errors.push(`${path} requires at least ${minimum} items`);
    if (value.length > maximum) errors.push(`${path} allows at most ${maximum} items`);
    const itemSchema = asObject(schema.items);
    for (let index = 0; index < value.length; index += 1) errors.push(...validateAgainstSchema(value[index], itemSchema, `${path}[${index}]`));
  } else if (type === "string") {
    if (typeof value !== "string") return [`${path} must be a string`];
    const minimum = integer(schema.minLength, 0);
    const maximum = schema.maxLength === undefined ? Number.MAX_SAFE_INTEGER : integer(schema.maxLength, Number.MAX_SAFE_INTEGER);
    if (value.length < minimum) errors.push(`${path} must have at least ${minimum} characters`);
    if (value.length > maximum) errors.push(`${path} must have at most ${maximum} characters`);
    if (typeof schema.pattern === "string") {
      try {
        if (!new RegExp(schema.pattern).test(value)) errors.push(`${path} does not match pattern`);
      } catch {
        errors.push(`${path} schema pattern is invalid`);
      }
    }
  } else if (type === "number" || type === "integer") {
    if (typeof value !== "number" || !Number.isFinite(value)) return [`${path} must be a number`];
    if (type === "integer" && !Number.isInteger(value)) errors.push(`${path} must be an integer`);
    if (typeof schema.minimum === "number" && value < schema.minimum) errors.push(`${path} must be >= ${schema.minimum}`);
    if (typeof schema.maximum === "number" && value > schema.maximum) errors.push(`${path} must be <= ${schema.maximum}`);
  } else if (type === "boolean" && typeof value !== "boolean") errors.push(`${path} must be a boolean`);
  if (Array.isArray(schema.enum) && !schema.enum.some((item) => canonicalJson(item) === canonicalJson(value))) {
    errors.push(`${path} is not in the allowed enum`);
  }
  return errors;
}

function normalizeSchema(value: JsonObject): JsonObject {
  const schema = structuredClone(value);
  if (schema.type === undefined) schema.type = "object";
  if (schema.type !== "object") throw new Error("tool input schema root must be object");
  if (schema.properties === undefined) schema.properties = {};
  if (schema.additionalProperties === undefined) schema.additionalProperties = false;
  return sortJson(schema) as JsonObject;
}

function validateSnapshot(snapshot: ToolExecutionSnapshot): void {
  const specs = new Set(snapshot.specs.map((spec) => spec.name));
  const calls = new Set(snapshot.calls.map((call) => call.callId));
  const leases = new Set(snapshot.leases.map((lease) => lease.leaseId));
  for (const call of snapshot.calls) {
    if (!specs.has(call.toolName)) throw new Error(`snapshot call references missing tool: ${call.toolName}`);
    if (call.leaseId && !leases.has(call.leaseId)) throw new Error(`snapshot call references missing lease: ${call.leaseId}`);
  }
  for (const chunk of snapshot.chunks) if (!calls.has(chunk.callId)) throw new Error(`snapshot chunk references missing call: ${chunk.callId}`);
  for (const batch of snapshot.batches) for (const id of batch.callIds) if (!calls.has(id)) throw new Error(`snapshot batch references missing call: ${id}`);
}

function specFromJson(value: JsonObject): Omit<ToolRuntimeSpec, "schemaDigest"> & { schemaDigest?: string } {
  return {
    name: asString(value.name),
    namespace: asString(value.namespace, "runtime"),
    version: asString(value.version, "1"),
    description: asString(value.description),
    inputSchema: asObject(value.input_schema),
    effects: Array.isArray(value.effects) ? value.effects.map((item) => toolEffect(String(item))) : ["read"],
    risk: toolRisk(asString(value.risk, "unknown")),
    readOnly: asBoolean(value.read_only, false),
    supportsStreaming: asBoolean(value.supports_streaming, false),
    supportsCancellation: asBoolean(value.supports_cancellation, true),
    idempotent: asBoolean(value.idempotent, false),
    maximumResultChars: positive(value.maximum_result_chars, 8_000),
    timeoutMs: positive(value.timeout_ms, 120_000),
    concurrencyKey: asString(value.concurrency_key),
    metadata: asObject(value.metadata),
    schemaDigest: asString(value.schema_digest) || undefined,
  };
}

function invocationInputFromJson(value: JsonObject): Parameters<ToolExecutionRuntime["createInvocation"]>[0] {
  return {
    callId: asString(value.call_id) || undefined,
    sessionId: asString(value.session_id),
    runId: asString(value.run_id),
    taskId: asString(value.task_id),
    turnId: asString(value.turn_id),
    toolName: asString(value.tool_name),
    arguments: asObject(value.arguments),
    idempotencyKey: asString(value.idempotency_key, randomUUID()),
  };
}

function specToJson(value: ToolRuntimeSpec): JsonObject {
  return {
    name: value.name,
    namespace: value.namespace,
    version: value.version,
    description: value.description,
    input_schema: value.inputSchema,
    effects: [...value.effects],
    risk: value.risk,
    read_only: value.readOnly,
    supports_streaming: value.supportsStreaming,
    supports_cancellation: value.supportsCancellation,
    idempotent: value.idempotent,
    maximum_result_chars: value.maximumResultChars,
    timeout_ms: value.timeoutMs,
    concurrency_key: value.concurrencyKey,
    metadata: value.metadata,
    schema_digest: value.schemaDigest,
  };
}

function callToJson(value: ToolInvocation): JsonObject {
  return {
    call_id: value.callId,
    session_id: value.sessionId,
    run_id: value.runId,
    task_id: value.taskId,
    turn_id: value.turnId,
    tool_name: value.toolName,
    arguments: value.arguments,
    arguments_digest: value.argumentsDigest,
    idempotency_key: value.idempotencyKey,
    state: value.state,
    permission_decision_id: value.permissionDecisionId,
    permission_granted: value.permissionGranted,
    lease_id: value.leaseId,
    lease_owner: value.leaseOwner,
    lease_expires_at: value.leaseExpiresAt,
    attempt: value.attempt,
    stream_sequence: value.streamSequence,
    result: value.result,
    result_digest: value.resultDigest,
    result_summary: value.resultSummary,
    result_chars: value.resultChars,
    original_result_chars: value.originalResultChars,
    truncated: value.truncated,
    error_code: value.errorCode,
    error_message: value.errorMessage,
    created_at: value.createdAt,
    started_at: value.startedAt,
    completed_at: value.completedAt,
    revision: value.revision,
  };
}

function chunkToJson(value: ToolStreamChunk): JsonObject {
  return {
    chunk_id: value.chunkId,
    call_id: value.callId,
    sequence: value.sequence,
    channel: value.channel,
    content: value.content,
    content_digest: value.contentDigest,
    effective: value.effective,
    created_at: value.createdAt,
  };
}

function leaseToJson(value: ToolLease): JsonObject {
  return {
    lease_id: value.leaseId,
    call_id: value.callId,
    owner: value.owner,
    acquired_at: value.acquiredAt,
    expires_at: value.expiresAt,
    released_at: value.releasedAt,
    revision: value.revision,
  };
}

function budgetToJson(value: ToolResultBudget): JsonObject {
  return {
    per_call_chars: value.perCallChars,
    per_turn_chars: value.perTurnChars,
    per_query_chars: value.perQueryChars,
    consumed_by_turn: value.consumedByTurn,
    consumed_by_query: value.consumedByQuery,
    overflow_artifact_threshold: value.overflowArtifactThreshold,
  };
}

function normalizeToolName(value: string): string {
  const name = value.trim();
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(name)) throw new Error(`invalid tool name: ${value}`);
  return name;
}

function normalizeNamespace(value: string): string {
  const namespace = value.trim() || "runtime";
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(namespace)) throw new Error(`invalid tool namespace: ${value}`);
  return namespace;
}

function toolEffect(value: string): ToolEffect {
  if (value === "write" || value === "network" || value === "process" || value === "control") return value;
  return "read";
}

function toolRisk(value: string): ToolRisk {
  if (value === "low" || value === "medium" || value === "high") return value;
  return "unknown";
}

function streamChannel(value: string): ToolStreamChunk["channel"] {
  if (value === "stdout" || value === "stderr" || value === "result") return value;
  return "progress";
}

function isTerminal(value: ToolCallState): boolean {
  return value === "succeeded" || value === "failed" || value === "cancelled" || value === "quarantined";
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.floor(value) : fallback;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function nullablePositive(value: number | null | undefined): number | null {
  if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) return null;
  return Math.floor(value);
}

function normalizeTimestamp(value?: string): string {
  if (!value) return new Date().toISOString();
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid timestamp: ${value}`);
  return new Date(timestamp).toISOString();
}

function sortJson(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(sortJson);
  if (value && typeof value === "object") {
    const result: JsonObject = {};
    for (const key of Object.keys(value).sort()) result[key] = sortJson(value[key]);
    return result;
  }
  return value;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
