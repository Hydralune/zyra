import { createHash } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";
import { OmpContinuityReceiptRuntime } from "./continuity-runtime.ts";
import {
  OmpRecoveryIntegrationRuntime,
  type McpTransportObservation,
  type PartialStreamObservation,
  type ProviderCredentialCandidate,
  type ProviderFailureObservation,
} from "./omp-integration-runtime.ts";

export type OmpRecoverySignal =
  | "api_retry_exhausted"
  | "stream_stall"
  | "provider_unavailable"
  | "provider_rate_limit"
  | "provider_quota"
  | "worker_unavailable"
  | "tool_timeout"
  | "mcp_disconnected"
  | "process_exited";

export type OmpRecoveryAction =
  | "retry"
  | "switch_provider"
  | "switch_backend"
  | "reroute"
  | "authenticate_mcp"
  | "abort";

export interface OmpRecoveryRefs {
  runId: string;
  taskId: string;
  sessionId: string;
  requestId: string;
  responseId: string;
  attemptId: string;
  workerId: string;
  backendId: string;
  providerId: string;
  modelId: string;
  mcpServerId: string;
  toolCallId: string;
}

export interface OmpFaultInput {
  kind: string;
  retryable: boolean;
  terminal: boolean;
  statusCode: number | null;
  observedCode: string;
  errorType: string;
  refs: OmpRecoveryRefs;
  details: JsonObject;
  observedAt: string;
}

export interface OmpRecoveryCandidate {
  action: OmpRecoveryAction;
  eligible: boolean;
  score: number;
  delayMs: number;
  ruleId: string;
  blockers: string[];
  requirements: string[];
  metadata: JsonObject;
}

export interface OmpRecoveryReceipt {
  schema: "zyra.omp-recovery-receipt/v1";
  receipt_id: string;
  signal_kind: OmpRecoverySignal;
  action_candidates: OmpRecoveryCandidate[];
  retry: {
    eligible: boolean;
    attempt: number;
    max_attempts: number;
    delay_ms: number;
    retry_after_ms: number;
    backoff_policy: string;
  };
  replay_fence: {
    request_key: string;
    response_key: string;
    side_effect_key: string;
    retry_safe: boolean;
    partial_output: boolean;
    observable_side_effect: boolean;
  };
  routing: {
    current_provider_id: string;
    excluded_provider_ids: string[];
    requested_layer: string;
    lease_owner: string;
  };
  refs: JsonObject;
  details: JsonObject;
  provenance: JsonObject;
  created_at: string;
}

export interface RetryBudget {
  maxAttempts: number;
  baseDelayMs: number;
  maximumDelayMs: number;
  multiplier: number;
  jitterRatio: number;
}

export interface RetryHistoryEntry {
  requestKey: string;
  signal: OmpRecoverySignal;
  attempt: number;
  result: "scheduled" | "succeeded" | "failed" | "aborted";
  delayMs: number;
  providerId: string;
  responseId: string;
  createdAt: string;
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

function boundedInteger(value: unknown, fallback: number, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return fallback;
  }
  return Math.max(minimum, Math.min(maximum, Math.trunc(value)));
}

function booleanValue(value: JsonValue | undefined, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function stringValue(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}

function objectValue(value: JsonValue | undefined): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? structuredClone(value as JsonObject)
    : {};
}

function stringArray(value: JsonValue | undefined): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export class DeterministicRetryBudgetRuntime {
  private readonly historyByRequest = new Map<string, RetryHistoryEntry[]>();
  readonly policy: RetryBudget;

  constructor(policy: Partial<RetryBudget> = {}) {
    this.policy = {
      maxAttempts: boundedInteger(policy.maxAttempts, 3, 0, 100),
      baseDelayMs: boundedInteger(policy.baseDelayMs, 250, 0, 3_600_000),
      maximumDelayMs: boundedInteger(policy.maximumDelayMs, 30_000, 0, 3_600_000),
      multiplier: typeof policy.multiplier === "number" && Number.isFinite(policy.multiplier)
        ? Math.max(1, Math.min(10, policy.multiplier))
        : 2,
      jitterRatio: typeof policy.jitterRatio === "number" && Number.isFinite(policy.jitterRatio)
        ? Math.max(0, Math.min(0.5, policy.jitterRatio))
        : 0.1,
    };
  }

  attempt(requestKey: string): number {
    return (this.historyByRequest.get(requestKey) ?? []).filter((item) => item.result !== "aborted").length;
  }

  eligible(requestKey: string): boolean {
    return this.attempt(requestKey) < this.policy.maxAttempts;
  }

  delay(requestKey: string, retryAfterMs = 0): number {
    const attempt = this.attempt(requestKey);
    const exponential = this.policy.baseDelayMs * Math.pow(this.policy.multiplier, attempt);
    const seed = parseInt(digest({ requestKey, attempt }).slice(-8), 16) / 0xffffffff;
    const jitter = exponential * this.policy.jitterRatio * ((seed * 2) - 1);
    const computed = Math.max(0, Math.round(exponential + jitter));
    return Math.min(this.policy.maximumDelayMs, Math.max(computed, retryAfterMs));
  }

  schedule(
    requestKey: string,
    signal: OmpRecoverySignal,
    providerId: string,
    retryAfterMs = 0,
  ): RetryHistoryEntry {
    if (!this.eligible(requestKey)) {
      throw new Error("retry budget exhausted for request " + requestKey);
    }
    const attempt = this.attempt(requestKey) + 1;
    const entry: RetryHistoryEntry = {
      requestKey,
      signal,
      attempt,
      result: "scheduled",
      delayMs: this.delay(requestKey, retryAfterMs),
      providerId,
      responseId: "",
      createdAt: new Date().toISOString(),
    };
    this.historyByRequest.set(requestKey, [...(this.historyByRequest.get(requestKey) ?? []), entry]);
    return structuredClone(entry);
  }

  settle(requestKey: string, attempt: number, result: "succeeded" | "failed" | "aborted", responseId = ""): RetryHistoryEntry {
    const history = this.historyByRequest.get(requestKey) ?? [];
    const index = history.findIndex((item) => item.attempt === attempt);
    if (index < 0) {
      throw new Error("unknown retry attempt");
    }
    const current = history[index];
    if (current.result !== "scheduled") {
      if (current.result === result && current.responseId === responseId) {
        return structuredClone(current);
      }
      throw new Error("retry attempt is already terminal");
    }
    const settled = { ...current, result, responseId };
    history[index] = settled;
    this.historyByRequest.set(requestKey, history);
    return structuredClone(settled);
  }

  history(requestKey: string): RetryHistoryEntry[] {
    return structuredClone(this.historyByRequest.get(requestKey) ?? []);
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-retry-budget/v1",
      policy: {
        max_attempts: this.policy.maxAttempts,
        base_delay_ms: this.policy.baseDelayMs,
        maximum_delay_ms: this.policy.maximumDelayMs,
        multiplier: this.policy.multiplier,
        jitter_ratio: this.policy.jitterRatio,
      },
      request_count: this.historyByRequest.size,
      histories: Object.fromEntries(
        [...this.historyByRequest.entries()].map(([key, value]) => [key, value]),
      ) as unknown as JsonObject,
    };
  }
}

export class ResponseReplayFence {
  private readonly processedResponses = new Map<string, string>();
  private readonly committedSideEffects = new Map<string, string>();
  private readonly pendingRequests = new Set<string>();

  requestKey(refs: OmpRecoveryRefs): string {
    return digest({
      runId: refs.runId,
      taskId: refs.taskId,
      sessionId: refs.sessionId,
      requestId: refs.requestId,
      attemptId: refs.attemptId,
      toolCallId: refs.toolCallId,
    });
  }

  responseKey(refs: OmpRecoveryRefs): string {
    return refs.responseId
      ? digest({ taskId: refs.taskId, responseId: refs.responseId })
      : "";
  }

  sideEffectKey(refs: OmpRecoveryRefs, details: JsonObject): string {
    const explicit = stringValue(details.side_effect_key);
    if (explicit) {
      return explicit;
    }
    if (!refs.toolCallId) {
      return "";
    }
    return digest({ taskId: refs.taskId, toolCallId: refs.toolCallId, attemptId: refs.attemptId });
  }

  beginRequest(key: string): boolean {
    if (!key || this.pendingRequests.has(key)) {
      return false;
    }
    this.pendingRequests.add(key);
    return true;
  }

  finishRequest(key: string): void {
    this.pendingRequests.delete(key);
  }

  markResponse(key: string, receiptRef: string): boolean {
    if (!key) {
      throw new Error("response replay key is required");
    }
    const existing = this.processedResponses.get(key);
    if (existing !== undefined) {
      if (existing !== receiptRef) {
        throw new Error("response replay key changed receipt identity");
      }
      return false;
    }
    this.processedResponses.set(key, receiptRef);
    return true;
  }

  markSideEffect(key: string, receiptRef: string): boolean {
    if (!key) {
      throw new Error("side effect replay key is required");
    }
    const existing = this.committedSideEffects.get(key);
    if (existing !== undefined) {
      if (existing !== receiptRef) {
        throw new Error("side effect key changed receipt identity");
      }
      return false;
    }
    this.committedSideEffects.set(key, receiptRef);
    return true;
  }

  retrySafety(refs: OmpRecoveryRefs, details: JsonObject): {
    requestKey: string;
    responseKey: string;
    sideEffectKey: string;
    retrySafe: boolean;
    partialOutput: boolean;
    observableSideEffect: boolean;
  } {
    const requestKey = this.requestKey(refs);
    const responseKey = this.responseKey(refs);
    const sideEffectKey = this.sideEffectKey(refs, details);
    const partialOutput = booleanValue(details.partial_output);
    const observableSideEffect = booleanValue(details.observable_side_effect);
    const responseProcessed = responseKey ? this.processedResponses.has(responseKey) : false;
    const sideEffectCommitted = sideEffectKey ? this.committedSideEffects.has(sideEffectKey) : false;
    return {
      requestKey,
      responseKey,
      sideEffectKey,
      retrySafe: !partialOutput && !observableSideEffect && !responseProcessed && !sideEffectCommitted,
      partialOutput,
      observableSideEffect,
    };
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-response-replay-fence/v1",
      processed_responses: Object.fromEntries(this.processedResponses) as JsonObject,
      committed_side_effects: Object.fromEntries(this.committedSideEffects) as JsonObject,
      pending_requests: [...this.pendingRequests],
    };
  }
}

export class OmpRecoveryReceiptRuntime {
  readonly retry: DeterministicRetryBudgetRuntime;
  readonly replay: ResponseReplayFence;
  readonly continuity: OmpContinuityReceiptRuntime;
  readonly integration: OmpRecoveryIntegrationRuntime;

  constructor(options: { retry?: Partial<RetryBudget> } = {}) {
    this.retry = new DeterministicRetryBudgetRuntime(options.retry);
    this.replay = new ResponseReplayFence();
    this.continuity = new OmpContinuityReceiptRuntime();
    this.integration = new OmpRecoveryIntegrationRuntime();
  }

  receipt(input: OmpFaultInput): OmpRecoveryReceipt {
    const signal = this.normalizeSignal(input);
    const safety = this.replay.retrySafety(input.refs, input.details);
    const attempt = this.retry.attempt(safety.requestKey);
    const retryAfter = boundedInteger(input.details.retry_after_ms, 0, 0, 3_600_000);
    const retryEligible = input.retryable
      && !input.terminal
      && safety.retrySafe
      && this.retry.eligible(safety.requestKey);
    const candidates = this.candidates(signal, input, retryEligible, safety.retrySafe);
    const requestedLayer = this.requestedLayer(signal);
    const integrationEvidence = this.integrationEvidence(input, signal, safety);
    const receiptId = "omprecovery_" + digest({ input, signal, candidates, attempt }).slice(7, 47);
    return {
      schema: "zyra.omp-recovery-receipt/v1",
      receipt_id: receiptId,
      signal_kind: signal,
      action_candidates: candidates,
      retry: {
        eligible: retryEligible,
        attempt,
        max_attempts: this.retry.policy.maxAttempts,
        delay_ms: retryEligible ? this.retry.delay(safety.requestKey, retryAfter) : 0,
        retry_after_ms: retryAfter,
        backoff_policy: "bounded-exponential-deterministic-jitter",
      },
      replay_fence: {
        request_key: safety.requestKey,
        response_key: safety.responseKey,
        side_effect_key: safety.sideEffectKey,
        retry_safe: safety.retrySafe,
        partial_output: safety.partialOutput,
        observable_side_effect: safety.observableSideEffect,
      },
      routing: {
        current_provider_id: input.refs.providerId,
        excluded_provider_ids: input.refs.providerId ? [input.refs.providerId] : [],
        requested_layer: requestedLayer,
        lease_owner: this.leaseOwner(requestedLayer),
      },
      refs: this.refsJson(input.refs),
      details: {
        ...structuredClone(input.details),
        omp_integration: integrationEvidence,
      },
      provenance: {
        source_repo: "oh-my-pi",
        source_revision: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        selected_mechanisms: [
          "bounded auth/provider retry classification",
          "provider fallback receipt",
          "session response replay fence",
          "task/worktree successor routing receipt",
        ],
        canonical_policy_owner: "python.RecoveryDecisionRuntime",
        applied_action_selected_here: false,
      },
      created_at: new Date().toISOString(),
    };
  }

  schedule(receipt: OmpRecoveryReceipt): RetryHistoryEntry {
    if (!receipt.retry.eligible) {
      throw new Error("receipt does not permit retry");
    }
    return this.retry.schedule(
      receipt.replay_fence.request_key,
      receipt.signal_kind,
      receipt.routing.current_provider_id,
      receipt.retry.retry_after_ms,
    );
  }

  settle(
    receipt: OmpRecoveryReceipt,
    attempt: number,
    result: "succeeded" | "failed" | "aborted",
    responseId = "",
  ): RetryHistoryEntry {
    const settled = this.retry.settle(receipt.replay_fence.request_key, attempt, result, responseId);
    if (result === "succeeded" && responseId) {
      this.replay.markResponse(digest({
        taskId: stringValue(receipt.refs.task_id),
        responseId,
      }), receipt.receipt_id);
    }
    return settled;
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-recovery-runtime/v1",
      retry: this.retry.snapshot(),
      replay: this.replay.snapshot(),
      continuity: this.continuity.snapshot(),
      integration: this.integration.snapshot(),
      applied_action_owner: "python.RecoveryDecisionRuntime",
      supplementary_only: true,
    };
  }

  private normalizeSignal(input: OmpFaultInput): OmpRecoverySignal {
    if (input.kind === "model_rate_limit") {
      return "provider_rate_limit";
    }
    if (input.kind === "model_quota_exhausted") {
      return "provider_quota";
    }
    if (input.kind === "model_failure") {
      return input.observedCode.includes("stall") ? "stream_stall" : "provider_unavailable";
    }
    if (input.kind === "worker_unavailable") {
      return "worker_unavailable";
    }
    if (input.kind === "tool_timeout") {
      return "tool_timeout";
    }
    if (input.kind === "mcp_disconnected") {
      return "mcp_disconnected";
    }
    if (input.kind === "process_exited") {
      return "process_exited";
    }
    return "api_retry_exhausted";
  }

  private candidates(
    signal: OmpRecoverySignal,
    input: OmpFaultInput,
    retryEligible: boolean,
    retrySafe: boolean,
  ): OmpRecoveryCandidate[] {
    const candidates: OmpRecoveryCandidate[] = [];
    if (["provider_rate_limit", "stream_stall", "tool_timeout"].includes(signal)) {
      candidates.push(this.candidate("retry", retryEligible, 0.88, "omp.retry.bounded.v1", input, retrySafe));
    }
    if (["provider_rate_limit", "provider_quota", "provider_unavailable", "stream_stall", "api_retry_exhausted"].includes(signal)) {
      candidates.push(this.candidate("switch_provider", Boolean(input.refs.providerId), 0.8, "omp.provider.fallback.v1", input, retrySafe));
    }
    if (["worker_unavailable", "process_exited"].includes(signal)) {
      candidates.push(this.candidate("reroute", Boolean(input.refs.workerId), 0.84, "omp.task.successor-route.v1", input, retrySafe));
      candidates.push(this.candidate("switch_backend", Boolean(input.refs.backendId), 0.72, "omp.backend.successor-route.v1", input, retrySafe));
    }
    if (signal === "mcp_disconnected") {
      candidates.push(this.candidate("authenticate_mcp", Boolean(input.refs.mcpServerId), 0.86, "omp.mcp.reconnect.v1", input, retrySafe));
    }
    candidates.push(this.candidate("abort", true, 0.05, "omp.fail-closed.v1", input, retrySafe));
    return candidates.sort((left, right) => (Number(right.eligible) - Number(left.eligible)) || (right.score - left.score) || left.action.localeCompare(right.action));
  }

  private candidate(
    action: OmpRecoveryAction,
    eligible: boolean,
    score: number,
    ruleId: string,
    input: OmpFaultInput,
    retrySafe: boolean,
  ): OmpRecoveryCandidate {
    const blockers: string[] = [];
    if (!eligible) {
      blockers.push("required structured route or retry evidence is unavailable");
    }
    if (action === "retry" && !retrySafe) {
      blockers.push("partial output or observable side effect requires checkpoint resume");
    }
    return {
      action,
      eligible,
      score,
      delayMs: action === "retry" && eligible
        ? this.retry.delay(this.replay.requestKey(input.refs), boundedInteger(input.details.retry_after_ms, 0))
        : 0,
      ruleId,
      blockers,
      requirements: this.requirements(action),
      metadata: {
        supplementary_only: true,
        selected_action: false,
        status_code: input.statusCode,
        observed_code: input.observedCode,
      },
    };
  }

  private requirements(action: OmpRecoveryAction): string[] {
    const values: Record<OmpRecoveryAction, string[]> = {
      retry: ["retry_budget", "replay_safe_request"],
      switch_provider: ["provider_control_plane_lease"],
      switch_backend: ["backend_registry_lease"],
      reroute: ["worker_pool_successor_lease"],
      authenticate_mcp: ["mcp_auth_control_path"],
      abort: ["canonical_failure_event"],
    };
    return values[action];
  }

  private requestedLayer(signal: OmpRecoverySignal): string {
    if (["worker_unavailable", "process_exited"].includes(signal)) {
      return "worker";
    }
    if (signal === "mcp_disconnected") {
      return "transport";
    }
    return "provider";
  }

  private leaseOwner(layer: string): string {
    const owners: Record<string, string> = {
      worker: "WorkerPoolFoundationRuntime",
      backend: "BackendRegistry",
      provider: "ProviderControlPlane",
      transport: "McpControlRuntime",
    };
    return owners[layer] ?? "RecoveryDecisionRuntime";
  }

  private refsJson(refs: OmpRecoveryRefs): JsonObject {
    return {
      run_id: refs.runId,
      task_id: refs.taskId,
      session_id: refs.sessionId,
      request_id: refs.requestId,
      response_id: refs.responseId,
      attempt_id: refs.attemptId,
      worker_id: refs.workerId,
      backend_id: refs.backendId,
      provider_id: refs.providerId,
      model_id: refs.modelId,
      mcp_server_id: refs.mcpServerId,
      tool_call_id: refs.toolCallId,
    };
  }

  private integrationEvidence(
    input: OmpFaultInput,
    signal: OmpRecoverySignal,
    safety: {
      requestKey: string;
      responseKey: string;
      sideEffectKey: string;
      retrySafe: boolean;
      partialOutput: boolean;
      observableSideEffect: boolean;
    },
  ): JsonObject {
    const supplement: {
      provider?: ProviderFailureObservation;
      stream?: PartialStreamObservation;
      mcp?: McpTransportObservation;
    } = {};
    if (["provider_rate_limit", "provider_quota", "provider_unavailable", "stream_stall", "api_retry_exhausted"].includes(signal)) {
      const rawCandidates = Array.isArray(input.details.candidate_routes)
        ? input.details.candidate_routes
        : [];
      const candidates: ProviderCredentialCandidate[] = rawCandidates
        .filter((item): item is JsonObject => Boolean(item) && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          providerId: stringValue(item.provider_id),
          modelId: stringValue(item.model_id),
          credentialId: stringValue(item.credential_id),
          routeId: stringValue(item.route_id),
          credentialVersion: boundedInteger(item.credential_version, 0),
          available: booleanValue(item.available, true),
          cooldownUntil: stringValue(item.cooldown_until),
          scopes: stringArray(item.scopes),
        }))
        .filter((item) => item.providerId && item.modelId && item.credentialId && item.routeId);
      supplement.provider = {
        runId: input.refs.runId,
        taskId: input.refs.taskId,
        requestId: input.refs.requestId || safety.requestKey,
        responseId: input.refs.responseId,
        currentProviderId: input.refs.providerId,
        currentModelId: input.refs.modelId,
        currentCredentialId: stringValue(input.details.credential_id),
        currentRouteId: stringValue(input.details.route_id),
        failureClass: signal === "provider_rate_limit"
          ? "rate_limit"
          : signal === "provider_quota"
            ? "quota"
            : signal === "provider_unavailable"
              ? "transport"
              : "transport",
        statusCode: input.statusCode,
        retryAfterMs: boundedInteger(input.details.retry_after_ms, 0, 0, 3_600_000),
        attempt: boundedInteger(input.details.attempt_count, 0, 0, 10_000),
        requiredScopes: stringArray(input.details.required_scopes),
        candidates,
        idempotencyKey: safety.requestKey,
        observedAt: input.observedAt,
      };
    }
    const checkpointId = stringValue(input.details.checkpoint_id);
    if ((safety.partialOutput || signal === "stream_stall") && checkpointId && input.refs.responseId) {
      supplement.stream = {
        runId: input.refs.runId,
        taskId: input.refs.taskId,
        sessionId: input.refs.sessionId,
        turnId: stringValue(input.details.turn_id) || input.refs.requestId,
        requestId: input.refs.requestId,
        responseId: input.refs.responseId,
        toolCallIds: stringArray(input.details.tool_call_ids),
        committedToolCallIds: stringArray(input.details.committed_tool_call_ids),
        emittedContentDigest: stringValue(input.details.emitted_content_digest) || safety.responseKey,
        checkpointId,
        sideEffectFenceKeys: stringArray(input.details.side_effect_fence_keys),
        sequence: boundedInteger(input.details.stream_sequence, 0),
        idempotencyKey: safety.responseKey || safety.requestKey,
      };
    }
    if (signal === "mcp_disconnected" && input.refs.mcpServerId) {
      supplement.mcp = {
        runId: input.refs.runId,
        taskId: input.refs.taskId,
        sessionId: input.refs.sessionId,
        requestId: input.refs.requestId || safety.requestKey,
        serverId: input.refs.mcpServerId,
        transportId: stringValue(input.details.transport_id) || "transport:unknown",
        failureCode: input.observedCode || "mcp.disconnected",
        authRequired: booleanValue(input.details.auth_required),
        retryable: input.retryable,
        attempt: boundedInteger(input.details.attempt_count, 0),
        maximumAttempts: boundedInteger(input.details.maximum_attempts, 3, 1, 10_000),
        cooldownMs: boundedInteger(input.details.cooldown_ms, 30_000, 0, 86_400_000),
        idempotencyKey: safety.requestKey,
        observedAt: input.observedAt,
      };
    }
    return this.integration.evidence(supplement);
  }
}

export function ompRecoveryRuntimeContract(): JsonObject {
  return {
    schema: "zyra.omp-recovery-runtime-contract/v1",
    source: {
      repository: "oh-my-pi",
      revision: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
      role: "supplementary",
    },
    migrated_mechanisms: [
      "auth/provider retry classification",
      "bounded exponential backoff",
      "provider fallback receipts",
      "response and side-effect replay fences",
      "append-only session resume and fork receipts",
      "task terminal generation receipts",
      "worktree merge conflict and side-effect receipts",
      "task worker successor route receipts",
      "provider credential rotation integration receipts",
      "partial stream exact-resume integration receipts",
      "MCP reconnect breaker integration receipts",
      "dirty worktree and durable background task recovery receipts",
    ],
    rejected_mechanisms: ["second recovery planner", "second checkpoint owner", "model-selected applied route"],
    receipt_consumer: "python.RecoverySignalClassifier.from_omp_receipt",
  };
}
