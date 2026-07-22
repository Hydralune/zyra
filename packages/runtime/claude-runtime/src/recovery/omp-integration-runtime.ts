import { createHash } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";


export type ProviderFailureClass = "rate_limit" | "credential_revoked" | "quota" | "transport" | "terminal";
export type CircuitState = "closed" | "open" | "half_open";
export type BackgroundTaskState = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface ProviderCredentialCandidate {
  providerId: string;
  modelId: string;
  credentialId: string;
  routeId: string;
  credentialVersion: number;
  available: boolean;
  cooldownUntil: string;
  scopes: string[];
  metadata?: JsonObject;
}

export interface ProviderFailureObservation {
  runId: string;
  taskId: string;
  requestId: string;
  responseId: string;
  currentProviderId: string;
  currentModelId: string;
  currentCredentialId: string;
  currentRouteId: string;
  failureClass: ProviderFailureClass;
  statusCode: number | null;
  retryAfterMs: number;
  attempt: number;
  requiredScopes: string[];
  candidates: ProviderCredentialCandidate[];
  idempotencyKey: string;
  observedAt?: string;
  metadata?: JsonObject;
}

export interface ProviderRotationReceipt {
  schema: "zyra.omp-provider-rotation-receipt/v1";
  receipt_id: string;
  run_id: string;
  task_id: string;
  request_id: string;
  failure_class: ProviderFailureClass;
  previous_route: JsonObject;
  candidate_routes: JsonObject[];
  selected_candidate: JsonObject | null;
  excluded_route_ids: string[];
  retry_after_ms: number;
  retry_eligible: boolean;
  route_mutation_applied: false;
  canonical_route_owner: "python.ProviderControlPlane";
  replayed: boolean;
  created_at: string;
}

export interface PartialStreamObservation {
  runId: string;
  taskId: string;
  sessionId: string;
  turnId: string;
  requestId: string;
  responseId: string;
  toolCallIds: string[];
  committedToolCallIds: string[];
  emittedContentDigest: string;
  checkpointId: string;
  sideEffectFenceKeys: string[];
  sequence: number;
  idempotencyKey: string;
  metadata?: JsonObject;
}

export interface PartialStreamReceipt {
  schema: "zyra.omp-partial-stream-receipt/v1";
  receipt_id: string;
  run_id: string;
  task_id: string;
  session_id: string;
  turn_id: string;
  request_id: string;
  response_id: string;
  checkpoint_id: string;
  sequence: number;
  emitted_content_digest: string;
  committed_tool_call_ids: string[];
  replayable_tool_call_ids: string[];
  skipped_tool_call_ids: string[];
  side_effect_fence_keys: string[];
  blind_retry_allowed: false;
  exact_resume_required: true;
  replayed: boolean;
  created_at: string;
}

export interface McpTransportObservation {
  runId: string;
  taskId: string;
  sessionId: string;
  requestId: string;
  serverId: string;
  transportId: string;
  failureCode: string;
  authRequired: boolean;
  retryable: boolean;
  attempt: number;
  maximumAttempts: number;
  cooldownMs: number;
  idempotencyKey: string;
  observedAt?: string;
  metadata?: JsonObject;
}

export interface McpBreakerReceipt {
  schema: "zyra.omp-mcp-breaker-receipt/v1";
  receipt_id: string;
  run_id: string;
  task_id: string;
  request_id: string;
  server_id: string;
  transport_id: string;
  previous_state: CircuitState;
  state: CircuitState;
  failure_count: number;
  auth_required: boolean;
  reconnect_eligible: boolean;
  cooldown_until: string;
  action_hint: "authenticate_mcp" | "retry" | "replan";
  applied_action_selected_here: false;
  replayed: boolean;
  created_at: string;
}

export interface WorktreeStateObservation {
  runId: string;
  taskId: string;
  subagentId: string;
  worktreeId: string;
  baseRevision: string;
  headRevision: string;
  targetRevision: string;
  changedPaths: string[];
  dirtyPaths: string[];
  conflictPaths: string[];
  untrackedPaths: string[];
  stashRef: string;
  idempotencyKey: string;
  metadata?: JsonObject;
}

export interface WorktreeRecoveryReceipt {
  schema: "zyra.omp-worktree-recovery-receipt/v1";
  receipt_id: string;
  run_id: string;
  task_id: string;
  subagent_id: string;
  worktree_id: string;
  base_revision: string;
  head_revision: string;
  target_revision: string;
  changed_paths: string[];
  dirty_paths: string[];
  conflict_paths: string[];
  untracked_paths: string[];
  stash_ref: string;
  state: "clean" | "dirty_wip" | "conflicted";
  merge_allowed: boolean;
  destructive_cleanup_allowed: false;
  side_effect_key: string;
  replayed: boolean;
  created_at: string;
}

export interface BackgroundTaskObservation {
  runId: string;
  taskId: string;
  jobId: string;
  issueId: string;
  attemptId: string;
  workerId: string;
  workerLeaseId: string;
  checkpointId: string;
  generation: number;
  state: BackgroundTaskState;
  resultRef: string;
  errorCode: string;
  heartbeatAt: string;
  idempotencyKey: string;
  metadata?: JsonObject;
}

export interface BackgroundTaskRecoveryReceipt {
  schema: "zyra.omp-background-task-recovery-receipt/v1";
  receipt_id: string;
  run_id: string;
  task_id: string;
  job_id: string;
  issue_id: string;
  attempt_id: string;
  worker_id: string;
  worker_lease_id: string;
  checkpoint_id: string;
  generation: number;
  state: BackgroundTaskState;
  disposition: "terminal" | "resume_checkpoint" | "reroute_worker" | "wait";
  result_ref: string;
  error_code: string;
  replayed: boolean;
  created_at: string;
}

interface StoredReceipt<T> {
  requestDigest: string;
  receipt: T;
}

interface McpCircuitRecord {
  state: CircuitState;
  failureCount: number;
  cooldownUntil: string;
  lastFailureCode: string;
  lastReceiptId: string;
}

interface BackgroundRecord {
  runId: string;
  taskId: string;
  jobId: string;
  issueId: string;
  generation: number;
  latest: BackgroundTaskRecoveryReceipt;
  history: BackgroundTaskRecoveryReceipt[];
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return "[" + value.map((item) => stableJson(item)).join(",") + "]";
  }
  const record = value as Record<string, unknown>;
  return "{" + Object.keys(record).sort().map((key) => JSON.stringify(key) + ":" + stableJson(record[key])).join(",") + "}";
}

function digest(value: unknown): string {
  return "sha256:" + createHash("sha256").update(stableJson(value)).digest("hex");
}

function identity(name: string, value: string, optional = false): string {
  const candidate = String(value ?? "").trim();
  if (optional && !candidate) {
    return "";
  }
  if (!candidate || !/^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,511}$/.test(candidate)) {
    throw new Error(name + " is missing or invalid");
  }
  return candidate;
}

function boundedInteger(name: string, value: number, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(name + " is outside its supported range");
  }
  return value;
}

function uniqueIdentities(name: string, values: string[]): string[] {
  const result: string[] = [];
  const known = new Set<string>();
  for (const raw of values) {
    const value = identity(name, raw);
    if (!known.has(value)) {
      known.add(value);
      result.push(value);
    }
  }
  return result;
}

function safePaths(name: string, values: string[]): string[] {
  return uniqueIdentities(name, values).map((raw) => {
    const value = raw.replaceAll("\\", "/");
    if (value.startsWith("/") || /^[A-Za-z]:\//.test(value)) {
      throw new Error(name + " cannot be absolute");
    }
    const parts = value.split("/");
    if (parts.some((part) => !part || part === "." || part === "..")) {
      throw new Error(name + " contains an unsafe segment");
    }
    return value;
  }).sort();
}

function future(value: string, now = Date.now()): boolean {
  if (!value) {
    return false;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) && parsed > now;
}

function replay<T extends { replayed: boolean }>(stored: StoredReceipt<T> | undefined, requestDigest: string): T | null {
  if (!stored) {
    return null;
  }
  if (stored.requestDigest !== requestDigest) {
    throw new Error("idempotency key was reused with different content");
  }
  return { ...structuredClone(stored.receipt), replayed: true };
}

export class ProviderCredentialRotationRuntime {
  private readonly receipts = new Map<string, StoredReceipt<ProviderRotationReceipt>>();

  observe(input: ProviderFailureObservation): ProviderRotationReceipt {
    const normalized = this.normalize(input);
    const requestDigest = digest(normalized);
    const key = digest({ runId: normalized.runId, taskId: normalized.taskId, requestId: normalized.requestId, idempotencyKey: normalized.idempotencyKey });
    const prior = replay(this.receipts.get(key), requestDigest);
    if (prior) {
      return prior;
    }
    const currentRoute = normalized.currentRouteId;
    const required = new Set(normalized.requiredScopes);
    const candidates = normalized.candidates
      .filter((candidate) => candidate.available)
      .filter((candidate) => !future(candidate.cooldownUntil))
      .filter((candidate) => [...required].every((scope) => candidate.scopes.includes(scope)))
      .filter((candidate) => candidate.routeId !== currentRoute)
      .filter((candidate) => candidate.credentialId !== normalized.currentCredentialId || candidate.providerId !== normalized.currentProviderId)
      .sort((left, right) => (
        Number(right.credentialVersion) - Number(left.credentialVersion)
        || left.providerId.localeCompare(right.providerId)
        || left.modelId.localeCompare(right.modelId)
        || left.credentialId.localeCompare(right.credentialId)
      ));
    const selected = candidates.at(0) ?? null;
    const retryEligible = normalized.failureClass === "rate_limit"
      || normalized.failureClass === "transport";
    const receipt: ProviderRotationReceipt = {
      schema: "zyra.omp-provider-rotation-receipt/v1",
      receipt_id: "ompprovider_" + digest({ key, requestDigest, selected }).slice(7, 47),
      run_id: normalized.runId,
      task_id: normalized.taskId,
      request_id: normalized.requestId,
      failure_class: normalized.failureClass,
      previous_route: {
        provider_id: normalized.currentProviderId,
        model_id: normalized.currentModelId,
        credential_id: normalized.currentCredentialId,
        route_id: normalized.currentRouteId,
      },
      candidate_routes: candidates.map((candidate) => this.candidateJson(candidate)),
      selected_candidate: selected ? this.candidateJson(selected) : null,
      excluded_route_ids: [normalized.currentRouteId, ...normalized.candidates.filter((item) => !item.available || future(item.cooldownUntil)).map((item) => item.routeId)].filter(Boolean),
      retry_after_ms: normalized.retryAfterMs,
      retry_eligible: retryEligible,
      route_mutation_applied: false,
      canonical_route_owner: "python.ProviderControlPlane",
      replayed: false,
      created_at: new Date().toISOString(),
    };
    this.receipts.set(key, { requestDigest, receipt });
    return structuredClone(receipt);
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-provider-rotation-runtime/v1",
      receipt_count: this.receipts.size,
      receipts: [...this.receipts.values()].map((item) => item.receipt) as unknown as JsonValue,
      canonical_route_owner: "python.ProviderControlPlane",
      route_mutation_applied_here: false,
    };
  }

  private normalize(input: ProviderFailureObservation): ProviderFailureObservation {
    if (!["rate_limit", "credential_revoked", "quota", "transport", "terminal"].includes(input.failureClass)) {
      throw new Error("provider failure class is invalid");
    }
    return {
      ...structuredClone(input),
      runId: identity("run id", input.runId),
      taskId: identity("task id", input.taskId),
      requestId: identity("request id", input.requestId),
      responseId: identity("response id", input.responseId, true),
      currentProviderId: identity("provider id", input.currentProviderId, true),
      currentModelId: identity("model id", input.currentModelId, true),
      currentCredentialId: identity("credential id", input.currentCredentialId, true),
      currentRouteId: identity("route id", input.currentRouteId, true),
      retryAfterMs: boundedInteger("retry after", input.retryAfterMs, 0, 3_600_000),
      attempt: boundedInteger("attempt", input.attempt, 0, 10_000),
      requiredScopes: uniqueIdentities("required scope", input.requiredScopes),
      candidates: input.candidates.map((candidate) => ({
        ...structuredClone(candidate),
        providerId: identity("candidate provider id", candidate.providerId),
        modelId: identity("candidate model id", candidate.modelId),
        credentialId: identity("candidate credential id", candidate.credentialId),
        routeId: identity("candidate route id", candidate.routeId),
        credentialVersion: boundedInteger("credential version", candidate.credentialVersion),
        scopes: uniqueIdentities("candidate scope", candidate.scopes),
      })),
      idempotencyKey: identity("idempotency key", input.idempotencyKey),
    };
  }

  private candidateJson(candidate: ProviderCredentialCandidate): JsonObject {
    return {
      provider_id: candidate.providerId,
      model_id: candidate.modelId,
      credential_id: candidate.credentialId,
      route_id: candidate.routeId,
      credential_version: candidate.credentialVersion,
      available: candidate.available,
      cooldown_until: candidate.cooldownUntil,
      scopes: [...candidate.scopes],
      credential_material_present: false,
    };
  }
}

export class PartialStreamContinuationRuntime {
  private readonly receipts = new Map<string, StoredReceipt<PartialStreamReceipt>>();
  private readonly processedResponses = new Map<string, string>();
  private readonly committedTools = new Map<string, string>();

  observe(input: PartialStreamObservation): PartialStreamReceipt {
    const normalized = this.normalize(input);
    const requestDigest = digest(normalized);
    const key = digest({ taskId: normalized.taskId, responseId: normalized.responseId, idempotencyKey: normalized.idempotencyKey });
    const prior = replay(this.receipts.get(key), requestDigest);
    if (prior) {
      return prior;
    }
    const responseKey = digest({ taskId: normalized.taskId, responseId: normalized.responseId });
    const existingResponse = this.processedResponses.get(responseKey);
    if (existingResponse) {
      throw new Error("partial stream response was already processed by " + existingResponse);
    }
    const committed = new Set(normalized.committedToolCallIds);
    const replayable: string[] = [];
    const skipped: string[] = [];
    for (const toolCallId of normalized.toolCallIds) {
      const toolKey = digest({ taskId: normalized.taskId, toolCallId });
      if (committed.has(toolCallId) || this.committedTools.has(toolKey)) {
        skipped.push(toolCallId);
      } else {
        replayable.push(toolCallId);
      }
    }
    const receipt: PartialStreamReceipt = {
      schema: "zyra.omp-partial-stream-receipt/v1",
      receipt_id: "ompstream_" + digest({ key, requestDigest, replayable, skipped }).slice(7, 47),
      run_id: normalized.runId,
      task_id: normalized.taskId,
      session_id: normalized.sessionId,
      turn_id: normalized.turnId,
      request_id: normalized.requestId,
      response_id: normalized.responseId,
      checkpoint_id: normalized.checkpointId,
      sequence: normalized.sequence,
      emitted_content_digest: normalized.emittedContentDigest,
      committed_tool_call_ids: [...normalized.committedToolCallIds],
      replayable_tool_call_ids: replayable,
      skipped_tool_call_ids: skipped,
      side_effect_fence_keys: [...normalized.sideEffectFenceKeys],
      blind_retry_allowed: false,
      exact_resume_required: true,
      replayed: false,
      created_at: new Date().toISOString(),
    };
    this.receipts.set(key, { requestDigest, receipt });
    this.processedResponses.set(responseKey, receipt.receipt_id);
    for (const toolCallId of normalized.committedToolCallIds) {
      this.committedTools.set(digest({ taskId: normalized.taskId, toolCallId }), receipt.receipt_id);
    }
    return structuredClone(receipt);
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-partial-stream-runtime/v1",
      receipt_count: this.receipts.size,
      processed_response_count: this.processedResponses.size,
      committed_tool_count: this.committedTools.size,
      blind_retry_allowed: false,
      canonical_fence_owner: "python.RecoveryPlanStore",
    };
  }

  private normalize(input: PartialStreamObservation): PartialStreamObservation {
    const toolCalls = uniqueIdentities("tool call id", input.toolCallIds);
    const committed = uniqueIdentities("committed tool call id", input.committedToolCallIds);
    if (committed.some((item) => !toolCalls.includes(item))) {
      throw new Error("committed tool call is absent from the partial stream tool set");
    }
    return {
      ...structuredClone(input),
      runId: identity("run id", input.runId),
      taskId: identity("task id", input.taskId),
      sessionId: identity("session id", input.sessionId),
      turnId: identity("turn id", input.turnId),
      requestId: identity("request id", input.requestId),
      responseId: identity("response id", input.responseId),
      checkpointId: identity("checkpoint id", input.checkpointId),
      emittedContentDigest: identity("content digest", input.emittedContentDigest),
      toolCallIds: toolCalls,
      committedToolCallIds: committed,
      sideEffectFenceKeys: uniqueIdentities("side effect fence key", input.sideEffectFenceKeys),
      sequence: boundedInteger("stream sequence", input.sequence),
      idempotencyKey: identity("idempotency key", input.idempotencyKey),
    };
  }
}

export class McpReconnectBreakerRuntime {
  private readonly circuits = new Map<string, McpCircuitRecord>();
  private readonly receipts = new Map<string, StoredReceipt<McpBreakerReceipt>>();

  observe(input: McpTransportObservation): McpBreakerReceipt {
    const normalized = this.normalize(input);
    const requestDigest = digest(normalized);
    const key = digest({ runId: normalized.runId, taskId: normalized.taskId, requestId: normalized.requestId, idempotencyKey: normalized.idempotencyKey });
    const prior = replay(this.receipts.get(key), requestDigest);
    if (prior) {
      return prior;
    }
    const circuitKey = digest({ taskId: normalized.taskId, serverId: normalized.serverId, transportId: normalized.transportId });
    const previous = this.circuits.get(circuitKey) ?? {
      state: "closed" as CircuitState,
      failureCount: 0,
      cooldownUntil: "",
      lastFailureCode: "",
      lastReceiptId: "",
    };
    const failureCount = previous.failureCount + 1;
    const exhausted = normalized.attempt >= normalized.maximumAttempts || failureCount >= normalized.maximumAttempts;
    const state: CircuitState = exhausted ? "open" : previous.state === "open" && !future(previous.cooldownUntil) ? "half_open" : previous.state;
    const cooldownUntil = state === "open"
      ? new Date(Date.now() + normalized.cooldownMs).toISOString()
      : previous.cooldownUntil;
    const reconnectEligible = normalized.retryable && !normalized.authRequired && state !== "open";
    const action = normalized.authRequired ? "authenticate_mcp" : reconnectEligible ? "retry" : "replan";
    const receipt: McpBreakerReceipt = {
      schema: "zyra.omp-mcp-breaker-receipt/v1",
      receipt_id: "ompmcp_" + digest({ key, requestDigest, failureCount, state }).slice(7, 47),
      run_id: normalized.runId,
      task_id: normalized.taskId,
      request_id: normalized.requestId,
      server_id: normalized.serverId,
      transport_id: normalized.transportId,
      previous_state: previous.state,
      state,
      failure_count: failureCount,
      auth_required: normalized.authRequired,
      reconnect_eligible: reconnectEligible,
      cooldown_until: cooldownUntil,
      action_hint: action,
      applied_action_selected_here: false,
      replayed: false,
      created_at: new Date().toISOString(),
    };
    this.circuits.set(circuitKey, {
      state,
      failureCount,
      cooldownUntil,
      lastFailureCode: normalized.failureCode,
      lastReceiptId: receipt.receipt_id,
    });
    this.receipts.set(key, { requestDigest, receipt });
    return structuredClone(receipt);
  }

  success(taskId: string, serverId: string, transportId: string): void {
    const key = digest({ taskId: identity("task id", taskId), serverId: identity("server id", serverId), transportId: identity("transport id", transportId) });
    this.circuits.set(key, { state: "closed", failureCount: 0, cooldownUntil: "", lastFailureCode: "", lastReceiptId: "" });
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-mcp-breaker-runtime/v1",
      circuits: Object.fromEntries(this.circuits) as unknown as JsonObject,
      receipt_count: this.receipts.size,
      canonical_mcp_owner: "python.McpControlRuntime",
      applied_action_selected_here: false,
    };
  }

  private normalize(input: McpTransportObservation): McpTransportObservation {
    return {
      ...structuredClone(input),
      runId: identity("run id", input.runId),
      taskId: identity("task id", input.taskId),
      sessionId: identity("session id", input.sessionId),
      requestId: identity("request id", input.requestId),
      serverId: identity("server id", input.serverId),
      transportId: identity("transport id", input.transportId),
      failureCode: identity("failure code", input.failureCode),
      attempt: boundedInteger("attempt", input.attempt, 0, 10_000),
      maximumAttempts: boundedInteger("maximum attempts", input.maximumAttempts, 1, 10_000),
      cooldownMs: boundedInteger("cooldown", input.cooldownMs, 0, 86_400_000),
      idempotencyKey: identity("idempotency key", input.idempotencyKey),
    };
  }
}

export class WorktreeRecoveryRuntime {
  private readonly receipts = new Map<string, StoredReceipt<WorktreeRecoveryReceipt>>();

  observe(input: WorktreeStateObservation): WorktreeRecoveryReceipt {
    const normalized = this.normalize(input);
    const requestDigest = digest(normalized);
    const key = digest({ runId: normalized.runId, taskId: normalized.taskId, worktreeId: normalized.worktreeId, idempotencyKey: normalized.idempotencyKey });
    const prior = replay(this.receipts.get(key), requestDigest);
    if (prior) {
      return prior;
    }
    const state = normalized.conflictPaths.length ? "conflicted" : normalized.dirtyPaths.length || normalized.untrackedPaths.length ? "dirty_wip" : "clean";
    const sideEffectKey = digest({ taskId: normalized.taskId, worktreeId: normalized.worktreeId, targetRevision: normalized.targetRevision });
    const receipt: WorktreeRecoveryReceipt = {
      schema: "zyra.omp-worktree-recovery-receipt/v1",
      receipt_id: "ompworktree_" + digest({ key, requestDigest, state }).slice(7, 47),
      run_id: normalized.runId,
      task_id: normalized.taskId,
      subagent_id: normalized.subagentId,
      worktree_id: normalized.worktreeId,
      base_revision: normalized.baseRevision,
      head_revision: normalized.headRevision,
      target_revision: normalized.targetRevision,
      changed_paths: normalized.changedPaths,
      dirty_paths: normalized.dirtyPaths,
      conflict_paths: normalized.conflictPaths,
      untracked_paths: normalized.untrackedPaths,
      stash_ref: normalized.stashRef,
      state,
      merge_allowed: state === "clean",
      destructive_cleanup_allowed: false,
      side_effect_key: sideEffectKey,
      replayed: false,
      created_at: new Date().toISOString(),
    };
    this.receipts.set(key, { requestDigest, receipt });
    return structuredClone(receipt);
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-worktree-recovery-runtime/v1",
      receipt_count: this.receipts.size,
      receipts: [...this.receipts.values()].map((item) => item.receipt) as unknown as JsonValue,
      canonical_workspace_owner: "python.WorkspaceManager",
      destructive_cleanup_allowed: false,
    };
  }

  private normalize(input: WorktreeStateObservation): WorktreeStateObservation {
    return {
      ...structuredClone(input),
      runId: identity("run id", input.runId),
      taskId: identity("task id", input.taskId),
      subagentId: identity("subagent id", input.subagentId),
      worktreeId: identity("worktree id", input.worktreeId),
      baseRevision: identity("base revision", input.baseRevision),
      headRevision: identity("head revision", input.headRevision),
      targetRevision: identity("target revision", input.targetRevision),
      changedPaths: safePaths("changed path", input.changedPaths),
      dirtyPaths: safePaths("dirty path", input.dirtyPaths),
      conflictPaths: safePaths("conflict path", input.conflictPaths),
      untrackedPaths: safePaths("untracked path", input.untrackedPaths),
      stashRef: identity("stash ref", input.stashRef, true),
      idempotencyKey: identity("idempotency key", input.idempotencyKey),
    };
  }
}

export class DurableBackgroundTaskReceiptRuntime {
  private readonly records = new Map<string, BackgroundRecord>();
  private readonly receipts = new Map<string, StoredReceipt<BackgroundTaskRecoveryReceipt>>();

  observe(input: BackgroundTaskObservation): BackgroundTaskRecoveryReceipt {
    const normalized = this.normalize(input);
    const requestDigest = digest(normalized);
    const key = digest({ runId: normalized.runId, taskId: normalized.taskId, jobId: normalized.jobId, idempotencyKey: normalized.idempotencyKey });
    const prior = replay(this.receipts.get(key), requestDigest);
    if (prior) {
      return prior;
    }
    const recordKey = digest({ runId: normalized.runId, taskId: normalized.taskId, jobId: normalized.jobId });
    const previous = this.records.get(recordKey);
    if (previous && normalized.generation < previous.generation) {
      throw new Error("background task generation regressed");
    }
    if (previous && normalized.generation === previous.generation && previous.latest.state !== normalized.state) {
      const priorTerminal = ["succeeded", "failed", "cancelled"].includes(previous.latest.state);
      if (priorTerminal) {
        throw new Error("terminal background task generation cannot change state");
      }
    }
    const disposition = this.disposition(normalized);
    const receipt: BackgroundTaskRecoveryReceipt = {
      schema: "zyra.omp-background-task-recovery-receipt/v1",
      receipt_id: "ompbackground_" + digest({ key, requestDigest, disposition }).slice(7, 47),
      run_id: normalized.runId,
      task_id: normalized.taskId,
      job_id: normalized.jobId,
      issue_id: normalized.issueId,
      attempt_id: normalized.attemptId,
      worker_id: normalized.workerId,
      worker_lease_id: normalized.workerLeaseId,
      checkpoint_id: normalized.checkpointId,
      generation: normalized.generation,
      state: normalized.state,
      disposition,
      result_ref: normalized.resultRef,
      error_code: normalized.errorCode,
      replayed: false,
      created_at: new Date().toISOString(),
    };
    const history = [...(previous?.history ?? []), receipt];
    this.records.set(recordKey, {
      runId: normalized.runId,
      taskId: normalized.taskId,
      jobId: normalized.jobId,
      issueId: normalized.issueId,
      generation: normalized.generation,
      latest: receipt,
      history,
    });
    this.receipts.set(key, { requestDigest, receipt });
    return structuredClone(receipt);
  }

  restartWorkset(runId: string, taskId: string): JsonObject {
    const selected = [...this.records.values()]
      .filter((record) => record.runId === runId && record.taskId === taskId)
      .map((record) => record.latest)
      .filter((receipt) => !["succeeded", "cancelled"].includes(receipt.state))
      .sort((left, right) => left.job_id.localeCompare(right.job_id));
    return {
      schema: "zyra.omp-background-restart-workset/v1",
      run_id: identity("run id", runId),
      task_id: identity("task id", taskId),
      tasks: selected as unknown as JsonValue,
      canonical_restart_owner: "python.RecoveryRestartRuntime",
      mutable_task_state_copied: false,
    };
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-background-task-runtime/v1",
      record_count: this.records.size,
      records: Object.fromEntries([...this.records].map(([key, value]) => [key, {
        run_id: value.runId,
        task_id: value.taskId,
        job_id: value.jobId,
        issue_id: value.issueId,
        generation: value.generation,
        latest: value.latest,
        history_count: value.history.length,
      }])) as unknown as JsonObject,
      canonical_task_owner: "python.TaskState",
      canonical_restart_owner: "python.RecoveryRestartRuntime",
    };
  }

  private disposition(input: BackgroundTaskObservation): BackgroundTaskRecoveryReceipt["disposition"] {
    if (["succeeded", "cancelled"].includes(input.state)) {
      return "terminal";
    }
    if (input.state === "running" && input.checkpointId) {
      return "resume_checkpoint";
    }
    if (input.state === "failed" || (input.state === "running" && !input.workerLeaseId)) {
      return "reroute_worker";
    }
    return "wait";
  }

  private normalize(input: BackgroundTaskObservation): BackgroundTaskObservation {
    if (!["queued", "running", "succeeded", "failed", "cancelled"].includes(input.state)) {
      throw new Error("background task state is invalid");
    }
    if (input.state === "succeeded" && !input.resultRef) {
      throw new Error("successful background task requires a result ref");
    }
    if (input.state === "failed" && !input.errorCode) {
      throw new Error("failed background task requires an error code");
    }
    return {
      ...structuredClone(input),
      runId: identity("run id", input.runId),
      taskId: identity("task id", input.taskId),
      jobId: identity("job id", input.jobId),
      issueId: identity("issue id", input.issueId),
      attemptId: identity("attempt id", input.attemptId),
      workerId: identity("worker id", input.workerId, true),
      workerLeaseId: identity("worker lease id", input.workerLeaseId, true),
      checkpointId: identity("checkpoint id", input.checkpointId, true),
      generation: boundedInteger("generation", input.generation),
      resultRef: identity("result ref", input.resultRef, true),
      errorCode: identity("error code", input.errorCode, true),
      idempotencyKey: identity("idempotency key", input.idempotencyKey),
    };
  }
}

export class OmpRecoveryIntegrationRuntime {
  readonly providers = new ProviderCredentialRotationRuntime();
  readonly streams = new PartialStreamContinuationRuntime();
  readonly mcp = new McpReconnectBreakerRuntime();
  readonly worktrees = new WorktreeRecoveryRuntime();
  readonly background = new DurableBackgroundTaskReceiptRuntime();

  evidence(input: {
    provider?: ProviderFailureObservation;
    stream?: PartialStreamObservation;
    mcp?: McpTransportObservation;
    worktree?: WorktreeStateObservation;
    background?: BackgroundTaskObservation;
  }): JsonObject {
    const receipts: JsonObject = {};
    if (input.provider) {
      receipts.provider = this.providers.observe(input.provider) as unknown as JsonValue;
    }
    if (input.stream) {
      receipts.stream = this.streams.observe(input.stream) as unknown as JsonValue;
    }
    if (input.mcp) {
      receipts.mcp = this.mcp.observe(input.mcp) as unknown as JsonValue;
    }
    if (input.worktree) {
      receipts.worktree = this.worktrees.observe(input.worktree) as unknown as JsonValue;
    }
    if (input.background) {
      receipts.background = this.background.observe(input.background) as unknown as JsonValue;
    }
    return {
      schema: "zyra.omp-recovery-integration-evidence/v1",
      receipts,
      supplementary_only: true,
      canonical_policy_owner: "python.RecoveryDecisionRuntime",
      canonical_checkpoint_owner: "python.RecoveryPlanStore",
      canonical_route_owner: "python.LayeredRouteRuntime+domain owners",
      applied_action_selected_here: false,
      provenance: {
        repository: "oh-my-pi",
        revision: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        migration_mode: "cropped_migration",
        selected_mechanisms: [
          "credential rotation candidates",
          "partial stream replay fencing",
          "MCP reconnect breaker",
          "dirty worktree and merge conflict receipts",
          "durable background task restart receipts",
        ],
      },
    };
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-recovery-integration-runtime/v1",
      providers: this.providers.snapshot(),
      streams: this.streams.snapshot(),
      mcp: this.mcp.snapshot(),
      worktrees: this.worktrees.snapshot(),
      background: this.background.snapshot(),
      supplementary_only: true,
      process_local_receipts_only: true,
      applied_action_selected_here: false,
    };
  }
}

export function ompIntegrationRuntimeContract(): JsonObject {
  return {
    schema: "zyra.omp-recovery-integration-contract/v1",
    source_repository: "oh-my-pi",
    source_revision: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
    role: "supplementary_implementation",
    migration_mode: "cropped_migration",
    mechanisms: [
      "provider retry and credential rotation receipts",
      "partial stream no-duplicate-side-effect receipts",
      "MCP reconnect and breaker receipts",
      "worktree conflict and dirty WIP receipts",
      "background and durable issue worker restart receipts",
    ],
    canonical_policy_owner: "python.RecoveryDecisionRuntime",
    canonical_checkpoint_owner: "python.RecoveryPlanStore",
    canonical_route_owner: "python domain route owners",
    consumer: "RecoverySignalClassifier.from_omp_receipt",
    langgraph_runtime_dependency: false,
    applied_action_selected_here: false,
    mutable_canonical_state_copied: false,
  };
}
