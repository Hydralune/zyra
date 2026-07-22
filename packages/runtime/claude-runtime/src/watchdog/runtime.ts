import { createHash, randomUUID } from "node:crypto";

import {
  asObject,
  asString,
  type JsonObject,
  type JsonValue,
  type RuntimeEvent,
  type RuntimeRunInput,
  type ToolBatch,
  type ToolExecutionRequest,
  type ToolExecutionResponse,
} from "../contracts.ts";
import {
  CrossRuntimeFaultSupervisor,
  crossRuntimeFaultSupervisorContract,
} from "./integration-supervisor.ts";

export type WatchdogMaturity =
  | "active_real"
  | "experimental"
  | "source_inactive"
  | "injection_only";

export type WatchdogLifecycle =
  | "registered"
  | "attached"
  | "running"
  | "stopped"
  | "disabled"
  | "failed";

export type ObservationCategory =
  | "tool"
  | "worker"
  | "browser"
  | "permission"
  | "provider"
  | "schema"
  | "workspace"
  | "mcp"
  | "process"
  | "requirement_change";

export type RuntimeFaultKind =
  | "tool_timeout"
  | "worker_unavailable"
  | "browser_crash"
  | "browser_disconnect"
  | "permission_denied"
  | "model_failure"
  | "model_rate_limit"
  | "model_quota_exhausted"
  | "schema_failure"
  | "workspace_corrupt"
  | "mcp_disconnected"
  | "process_exited";

export interface WatchdogRefs {
  runId: string;
  taskId: string;
  observationId: string;
  sessionId: string;
  nodeId: string;
  attemptId: string;
  toolCallId: string;
  toolName: string;
  workerId: string;
  backendId: string;
  providerId: string;
  workspaceId: string;
  browserSessionId: string;
  mcpServerId: string;
  subagentTaskId: string;
  sourceStateRevision: number;
}

export interface WatchdogDescriptor {
  observerId: string;
  displayName: string;
  maturity: WatchdogMaturity;
  lifecycleOwner: "typescript.RuntimeWatchdogObserver";
  attachOwner: string;
  observationPoint: string;
  sourceRepo: "oh-my-pi" | "browser-use" | "zyra";
  sourceRevision: string;
  categories: ObservationCategory[];
  emittedKinds: RuntimeFaultKind[];
  enabledByDefault: boolean;
  metadata: JsonObject;
}

export interface WatchdogObservation {
  observationId: string;
  observerId: string;
  category: ObservationCategory;
  code: string;
  status: string;
  summary: string;
  errorType: string;
  retryable: boolean | null;
  terminal: boolean | null;
  elapsedMs: number | null;
  deadlineMs: number | null;
  statusCode: number | null;
  refs: WatchdogRefs;
  details: JsonObject;
  observedAt: string;
}

export interface WatchdogClassification {
  kind: RuntimeFaultKind;
  severity: "warning" | "error" | "critical";
  disposition: "degrade" | "block" | "recover";
  retryable: boolean;
  terminal: boolean;
  ruleId: string;
}

export interface ObserverState {
  descriptor: WatchdogDescriptor;
  lifecycle: WatchdogLifecycle;
  revision: number;
  observationCount: number;
  emittedCount: number;
  duplicateCount: number;
  lastObservationId: string;
  lastError: string;
}

type EmitFaultEvent = (event: RuntimeEvent) => Promise<void>;

const OMP_REVISION = "c6b83c-source-audit";
const ZYRA_REVISION = "M1-S07B-01";

function runtimeId(prefix: string): string {
  return prefix + "_" + randomUUID().replaceAll("-", "");
}

function nowIso(): string {
  return new Date().toISOString();
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

function descriptor(
  observerId: string,
  displayName: string,
  observationPoint: string,
  categories: ObservationCategory[],
  emittedKinds: RuntimeFaultKind[],
  metadata: JsonObject,
): WatchdogDescriptor {
  return {
    observerId,
    displayName,
    maturity: "active_real",
    lifecycleOwner: "typescript.RuntimeWatchdogObserver",
    attachOwner: "typescript.JsonlRuntimeHost",
    observationPoint,
    sourceRepo: "oh-my-pi",
    sourceRevision: OMP_REVISION,
    categories,
    emittedKinds,
    enabledByDefault: true,
    metadata,
  };
}

const DESCRIPTORS: WatchdogDescriptor[] = [
  descriptor(
    "ts-tool-deadline",
    "TypeScript tool batch deadline observer",
    "stdio.tool.batch.result",
    ["tool"],
    ["tool_timeout"],
    {
      source_mechanism: "oh-my-pi emission guard and MCP timeout race",
      terminal_result_guard: true,
    },
  ),
  descriptor(
    "ts-permission-receipt",
    "TypeScript permission settlement observer",
    "permission.tool.settlement",
    ["permission"],
    ["permission_denied"],
    {
      source_mechanism: "oh-my-pi structured response error contract",
      prose_identity_inference: false,
    },
  ),
  descriptor(
    "ts-provider-terminal",
    "TypeScript provider terminal failure observer",
    "query.runtime.terminal-error",
    ["provider"],
    ["model_failure", "model_rate_limit", "model_quota_exhausted"],
    {
      source_mechanism: "oh-my-pi provider error and retryability taxonomy",
      structured_status_only: true,
    },
  ),
  descriptor(
    "ts-process-transport",
    "TypeScript process and MCP transport observer",
    "stdio.transport.lifecycle",
    ["process", "mcp", "worker"],
    ["process_exited", "mcp_disconnected", "worker_unavailable"],
    {
      source_mechanism: "oh-my-pi stdio close and MCP reconnect breaker",
      reconnect_owner: "M1-S07C",
    },
  ),
];

function emptyState(value: WatchdogDescriptor): ObserverState {
  return {
    descriptor: value,
    lifecycle: "registered",
    revision: 1,
    observationCount: 0,
    emittedCount: 0,
    duplicateCount: 0,
    lastObservationId: "",
    lastError: "",
  };
}

function structuredError(error: unknown): { name: string; code: string; statusCode: number | null; retryable: boolean | null } {
  if (error === null || typeof error !== "object") {
    return { name: "Error", code: "provider_error", statusCode: null, retryable: null };
  }
  const value = error as Record<string, unknown>;
  const name = typeof value.name === "string" ? value.name : "Error";
  const rawCode = typeof value.code === "string" ? value.code.toLowerCase() : "";
  const statusCode = typeof value.status === "number"
    ? value.status
    : (typeof value.statusCode === "number" ? value.statusCode : null);
  const retryable = typeof value.retryable === "boolean" ? value.retryable : null;
  let code = rawCode || "provider_error";
  if (statusCode === 429 && ["insufficient_quota", "quota_exhausted", "usage_limit_reached"].includes(code)) {
    code = "quota_exhausted";
  } else if (statusCode === 429) {
    code = "rate_limited";
  } else if ([408, 500, 502, 503, 504, 529].includes(statusCode ?? -1)) {
    code = "provider_error";
  }
  return { name, code, statusCode, retryable };
}

function classify(observation: WatchdogObservation): WatchdogClassification | null {
  if (observation.category === "requirement_change") {
    return null;
  }
  if (observation.category === "tool" && ["tool_timeout", "deadline_expired", "request_timeout"].includes(observation.code)) {
    if (!observation.refs.toolCallId) {
      throw new Error("tool timeout observation lacks explicit toolCallId");
    }
    return {
      kind: "tool_timeout",
      severity: "error",
      disposition: "recover",
      retryable: observation.retryable ?? true,
      terminal: observation.terminal ?? true,
      ruleId: "tool.deadline.expired.v1",
    };
  }
  if (observation.category === "permission" && observation.code === "permission_denied") {
    return {
      kind: "permission_denied",
      severity: "error",
      disposition: "block",
      retryable: false,
      terminal: true,
      ruleId: "permission.denied.v1",
    };
  }
  if (observation.category === "provider") {
    if (!observation.refs.providerId) {
      throw new Error("provider observation lacks explicit providerId");
    }
    if (["quota_exhausted", "usage_limit_reached", "insufficient_quota"].includes(observation.code)) {
      return { kind: "model_quota_exhausted", severity: "error", disposition: "recover", retryable: true, terminal: true, ruleId: "provider.quota.v1" };
    }
    if (["rate_limit", "rate_limited", "capacity_exhausted"].includes(observation.code) || observation.statusCode === 429) {
      return { kind: "model_rate_limit", severity: "warning", disposition: "degrade", retryable: true, terminal: false, ruleId: "provider.rate-limit.v1" };
    }
    return { kind: "model_failure", severity: "error", disposition: "recover", retryable: observation.retryable ?? true, terminal: observation.terminal ?? true, ruleId: "provider.failure.v1" };
  }
  if (observation.category === "mcp" && ["transport_closed", "request_timeout", "reconnect_breaker_open", "server_exited"].includes(observation.code)) {
    if (!observation.refs.mcpServerId) {
      throw new Error("MCP observation lacks explicit mcpServerId");
    }
    return { kind: "mcp_disconnected", severity: "error", disposition: "recover", retryable: true, terminal: true, ruleId: "mcp.transport-disconnected.v1" };
  }
  if (observation.category === "worker" && ["worker_lost", "heartbeat_lost", "lease_lost"].includes(observation.code)) {
    if (!observation.refs.workerId) {
      throw new Error("worker observation lacks explicit workerId");
    }
    return { kind: "worker_unavailable", severity: "critical", disposition: "recover", retryable: true, terminal: true, ruleId: "worker.lease-or-heartbeat-lost.v1" };
  }
  if (observation.category === "process" && ["process_exited", "pipe_closed"].includes(observation.code)) {
    return { kind: "process_exited", severity: "error", disposition: "recover", retryable: true, terminal: true, ruleId: "process.unexpected-exit.v1" };
  }
  return null;
}

export class RuntimeWatchdogObserver {
  private readonly states = new Map<string, ObserverState>();
  private readonly fingerprints = new Map<string, number>();
  private readonly terminalKeys = new Set<string>();
  private sequence = 0;
  private context: RuntimeRunInput | null = null;
  private readonly emit: EmitFaultEvent;
  readonly supervision: CrossRuntimeFaultSupervisor;

  constructor(emit: EmitFaultEvent) {
    this.emit = emit;
    this.supervision = new CrossRuntimeFaultSupervisor(
      async (observerId, observation) => await this.observeSupplementary(observerId, observation),
    );
    for (const item of DESCRIPTORS) {
      this.states.set(item.observerId, emptyState(item));
    }
  }

  configure(input: RuntimeRunInput): void {
    if (this.context !== null && this.context.runId !== input.runId) {
      throw new Error("watchdog cannot be rebound to another run");
    }
    this.context = input;
    this.supervision.configure(input);
    for (const state of this.states.values()) {
      if (!state.descriptor.enabledByDefault || state.descriptor.maturity !== "active_real") {
        continue;
      }
      if (state.lifecycle === "registered") {
        state.lifecycle = "attached";
        state.revision += 1;
      }
      state.lifecycle = "running";
      state.revision += 1;
    }
  }

  stop(): void {
    this.supervision.dispose();
    for (const state of this.states.values()) {
      if (state.lifecycle === "running") {
        state.lifecycle = "stopped";
        state.revision += 1;
      }
    }
  }

  disable(observerId: string): void {
    const state = this.requireState(observerId);
    state.lifecycle = "disabled";
    state.revision += 1;
  }

  async observeSupplementary(
    observerId: string,
    observation: WatchdogObservation,
  ): Promise<void> {
    if (observation.observerId !== observerId) {
      throw new Error("supplementary observation observerId mismatch");
    }
    await this.accept(observerId, observation);
  }

  async observeToolBatch(
    batch: ToolBatch,
    requests: ToolExecutionRequest[],
    responses: ToolExecutionResponse[],
    elapsedMs: number,
    deadlineMs: number,
  ): Promise<void> {
    for (let index = 0; index < requests.length; index += 1) {
      const request = requests[index];
      const response = responses[index];
      const responseMetadata = response?.metadata ?? {};
      const permissionDenied = response?.ok === false && (
        responseMetadata.permission_decision === "deny"
        || responseMetadata.permission_status === "denied"
        || responseMetadata.reason_code === "permission_denied"
      );
      if (permissionDenied) {
        await this.accept("ts-permission-receipt", {
          observationId: runtimeId("ts_permission_observation"),
          observerId: "ts-permission-receipt",
          category: "permission",
          code: "permission_denied",
          status: "denied",
          summary: "Canonical TypeScript permission settlement denied the tool call.",
          errorType: "PermissionDenied",
          retryable: false,
          terminal: true,
          elapsedMs,
          deadlineMs,
          statusCode: null,
          refs: this.refs({
            observationId: "",
            toolCallId: request.toolCallId,
            toolName: request.toolName,
          }),
          details: {
            batch_id: batch.batchId,
            batch_index: request.batchIndex,
            permission_decision: responseMetadata.permission_decision ?? "deny",
            reason_code: responseMetadata.reason_code ?? "permission_denied",
          },
          observedAt: nowIso(),
        });
        continue;
      }
      const timedOut = elapsedMs >= deadlineMs || (
        response?.ok === false
        && ["tool_timeout", "timeout", "deadline_expired"].includes(responseMetadata.error_code ?? "")
      );
      if (!timedOut) {
        continue;
      }
      const observationId = runtimeId("ts_tool_timeout_observation");
      await this.accept("ts-tool-deadline", {
        observationId,
        observerId: "ts-tool-deadline",
        category: "tool",
        code: "tool_timeout",
        status: "timed_out",
        summary: "Tool execution crossed its deterministic host deadline.",
        errorType: "ToolTimeoutError",
        retryable: true,
        terminal: true,
        elapsedMs,
        deadlineMs,
        statusCode: null,
        refs: this.refs({
          observationId,
          toolCallId: request.toolCallId,
          toolName: request.toolName,
        }),
        details: {
          batch_id: batch.batchId,
          batch_index: request.batchIndex,
          batch_size: request.batchSize,
          execution_mode: request.executionMode,
          response_ok: response?.ok ?? false,
          terminal_result_guard: true,
        },
        observedAt: nowIso(),
      });
    }
  }

  async observeProviderError(error: unknown): Promise<void> {
    const context = this.requireContext();
    const structured = structuredError(error);
    const providerId = asString(context.metadata?.provider_id)
      || asString(asObject(context.config.runtimeConstraints).provider_id)
      || asString(context.config.modelName)
      || "configured-provider";
    const observationId = runtimeId("ts_provider_observation");
    await this.accept("ts-provider-terminal", {
      observationId,
      observerId: "ts-provider-terminal",
      category: "provider",
      code: structured.code,
      status: "failed",
      summary: "The configured provider failed before a safe terminal runtime result.",
      errorType: structured.name,
      retryable: structured.retryable,
      terminal: true,
      elapsedMs: null,
      deadlineMs: null,
      statusCode: structured.statusCode,
      refs: this.refs({ observationId, providerId }),
      details: {
        structured_error_code: structured.code,
        source: "query-runtime-terminal-catch",
        message_used_for_identity: false,
      },
      observedAt: nowIso(),
    });
  }

  async observeTransportClosed(
    code: "pipe_closed" | "transport_closed" | "server_exited",
    refs: { workerId?: string; mcpServerId?: string },
    details: JsonObject = {},
  ): Promise<void> {
    const category: ObservationCategory = refs.mcpServerId ? "mcp" : (refs.workerId ? "worker" : "process");
    const observerCode = category === "worker" ? "worker_lost" : code;
    const observationId = runtimeId("ts_transport_observation");
    await this.accept("ts-process-transport", {
      observationId,
      observerId: "ts-process-transport",
      category,
      code: observerCode,
      status: category === "process" ? "exited" : "unavailable",
      summary: "A bound runtime transport crossed an unexpected terminal boundary.",
      errorType: "TransportClosed",
      retryable: true,
      terminal: true,
      elapsedMs: null,
      deadlineMs: null,
      statusCode: null,
      refs: this.refs({ observationId, workerId: refs.workerId, mcpServerId: refs.mcpServerId }),
      details,
      observedAt: nowIso(),
    });
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.typescript-runtime-watchdog/v1",
      source_roles: {
        oh_my_pi: "supplementary process/provider/deadline mechanics",
        zyra_classifier: "primary deterministic classifier contract",
      },
      states: Object.fromEntries(
        [...this.states.entries()].map(([key, value]) => [key, {
          descriptor: value.descriptor as unknown as JsonObject,
          lifecycle: value.lifecycle,
          revision: value.revision,
          observation_count: value.observationCount,
          emitted_count: value.emittedCount,
          duplicate_count: value.duplicateCount,
          last_observation_id: value.lastObservationId,
          last_error: value.lastError,
        }]),
      ) as unknown as JsonObject,
      sequence: this.sequence,
      terminal_guard_size: this.terminalKeys.size,
      requirement_changed_is_fault: false,
      free_text_identity_inference: false,
      cross_runtime_supervision: this.supervision.snapshot(),
    };
  }

  private async accept(observerId: string, observation: WatchdogObservation): Promise<void> {
    const state = this.requireState(observerId);
    if (state.lifecycle !== "running") {
      return;
    }
    const normalized = {
      ...observation,
      refs: {
        ...observation.refs,
        observationId: observation.observationId,
        sourceStateRevision: state.revision + state.observationCount + 1,
      },
    };
    const fingerprint = digest({
      observerId,
      category: normalized.category,
      code: normalized.code,
      refs: normalized.refs,
      details: normalized.details,
    });
    if (this.fingerprints.has(fingerprint)) {
      state.duplicateCount += 1;
      state.revision += 1;
      return;
    }
    const classification = classify(normalized);
    state.observationCount += 1;
    state.lastObservationId = normalized.observationId;
    state.revision += 1;
    this.fingerprints.set(fingerprint, state.revision);
    if (classification === null) {
      return;
    }
    if (!state.descriptor.categories.includes(normalized.category)) {
      state.lastError = "observer emitted undeclared category";
      state.lifecycle = "failed";
      throw new Error(state.lastError);
    }
    if (!state.descriptor.emittedKinds.includes(classification.kind)) {
      state.lastError = "classifier produced undeclared fault kind";
      state.lifecycle = "failed";
      throw new Error(state.lastError);
    }
    const terminalKey = [classification.kind, normalized.refs.toolCallId, normalized.refs.providerId, normalized.refs.mcpServerId, normalized.refs.workerId].join(":");
    if (classification.terminal && this.terminalKeys.has(terminalKey)) {
      state.duplicateCount += 1;
      return;
    }
    if (classification.terminal) {
      this.terminalKeys.add(terminalKey);
    }
    this.sequence += 1;
    state.emittedCount += 1;
    await this.emit({
      phase: "tool_failure_signal",
      sequence: this.sequence,
      canonical_owner: "typescript",
      runtime_id: "zyra-typescript-claude-runtime",
      schema: "zyra.watchdog-fault-signal/v1",
      signal_id: runtimeId("ts_fault_signal"),
      fault_kind: classification.kind,
      severity: classification.severity,
      disposition: classification.disposition,
      retryable: classification.retryable,
      terminal: classification.terminal,
      classification_rule: classification.ruleId,
      observed_code: normalized.code,
      observation: this.observationJson(normalized),
      refs: this.refsJson(normalized.refs),
      provenance: {
        observer_id: observerId,
        source_repo: state.descriptor.sourceRepo,
        source_revision: state.descriptor.sourceRevision,
        observation_point: state.descriptor.observationPoint,
        maturity: state.descriptor.maturity,
      },
      critical_ref_source: "structured_refs_only",
      injection_id: "",
    });
  }

  private refs(overrides: Partial<WatchdogRefs>): WatchdogRefs {
    const context = this.requireContext();
    const metadata = context.metadata ?? {};
    return {
      runId: context.runId,
      taskId: context.taskId,
      observationId: overrides.observationId ?? runtimeId("ts_watchdog_observation"),
      sessionId: overrides.sessionId ?? context.sessionId,
      nodeId: overrides.nodeId ?? (context.nodeId ?? ""),
      attemptId: overrides.attemptId ?? context.workerRequestId,
      toolCallId: overrides.toolCallId ?? "",
      toolName: overrides.toolName ?? "",
      workerId: overrides.workerId ?? asString(metadata.worker_id),
      backendId: overrides.backendId ?? asString(metadata.backend_id),
      providerId: overrides.providerId ?? asString(metadata.provider_id),
      workspaceId: overrides.workspaceId ?? asString(metadata.workspace_id),
      browserSessionId: overrides.browserSessionId ?? asString(metadata.browser_session_id),
      mcpServerId: overrides.mcpServerId ?? "",
      subagentTaskId: overrides.subagentTaskId ?? asString(metadata.subagent_task_id),
      sourceStateRevision: overrides.sourceStateRevision ?? 0,
    };
  }

  private observationJson(value: WatchdogObservation): JsonObject {
    return {
      observation_id: value.observationId,
      observer_id: value.observerId,
      category: value.category,
      code: value.code,
      status: value.status,
      summary: value.summary,
      error_type: value.errorType,
      retryable_hint: value.retryable,
      terminal_hint: value.terminal,
      elapsed_ms: value.elapsedMs,
      deadline_ms: value.deadlineMs,
      status_code: value.statusCode,
      refs: this.refsJson(value.refs),
      details: value.details,
      observed_at: value.observedAt,
    };
  }

  private refsJson(value: WatchdogRefs): JsonObject {
    return {
      run_id: value.runId,
      task_id: value.taskId,
      observation_id: value.observationId,
      session_id: value.sessionId,
      node_id: value.nodeId,
      attempt_id: value.attemptId,
      tool_call_id: value.toolCallId,
      tool_name: value.toolName,
      worker_id: value.workerId,
      backend_id: value.backendId,
      provider_id: value.providerId,
      workspace_id: value.workspaceId,
      browser_session_id: value.browserSessionId,
      mcp_server_id: value.mcpServerId,
      subagent_task_id: value.subagentTaskId,
      source_state_revision: value.sourceStateRevision,
    };
  }

  private requireContext(): RuntimeRunInput {
    if (this.context === null) {
      throw new Error("watchdog has not been configured for a runtime input");
    }
    return this.context;
  }

  private requireState(observerId: string): ObserverState {
    const value = this.states.get(observerId);
    if (value === undefined) {
      throw new Error("unknown watchdog observer " + observerId);
    }
    return value;
  }
}

export function runtimeWatchdogContract(): JsonObject {
  return {
    schema: "zyra.typescript-watchdog-contract/v1",
    observers: DESCRIPTORS as unknown as JsonValue,
    maturity_vocabulary: ["active_real", "experimental", "source_inactive", "injection_only"],
    event_phase: "tool_failure_signal",
    python_ingress: "runtime.events.ingest_runtime_event",
    canonical_signal_owner: "python.FaultStateStore",
    requirement_changed_is_fault: false,
    source_roles: {
      oh_my_pi: "supplementary",
      classifier_contract: "Zyra primary",
    },
    cross_runtime_supervision: crossRuntimeFaultSupervisorContract(),
  };
}
