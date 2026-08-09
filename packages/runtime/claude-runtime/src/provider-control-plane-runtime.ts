import type {
  DispatchMessage,
  DispatchTool,
  ProviderDispatchRequest,
  ProviderDispatchResult,
  ProviderRouteLease,
  ProviderStreamFrame,
} from "../../provider-control-plane/src/contracts.ts";
import { ProviderControlPlaneError } from "../../provider-control-plane/src/errors.ts";
import { digestJson, digestText } from "../../provider-control-plane/src/canonical.ts";
import {
  asBoolean,
  asObject,
  asString,
  type JsonObject,
  type RuntimeConfig,
  type RuntimeRunInput,
  type ToolSpecContract,
  type ToolStep,
} from "./contracts.ts";

export interface ProviderControlPlaneModelResolution {
  ok: boolean;
  turns: ToolStep[][];
  metadata: Record<string, string>;
  error: string | null;
  finalText: string;
  stopReason: string;
  providerRequestId: string | null;
}

type EmitRuntimeEvent = (phase: string, payload?: JsonObject) => Promise<void>;

interface ProviderRouteRefProjection {
  readonly routeId: string;
  readonly routeChecksum: string;
  readonly catalogRevision: number;
  readonly credentialVersion: number;
  readonly credentialFingerprint: string;
  readonly transportId: string;
  readonly sessionId: string;
  readonly turnId: string;
}

interface ToolAccumulator {
  readonly toolCallId: string;
  toolName: string;
  json: string;
  firstSequence: number;
  lastSequence: number;
}

const HIGH_VOLUME_CONTENT_FRAME_KINDS = new Set([
  "text_delta",
  "thinking_delta",
]);

export function providerControlPlaneRequired(config: RuntimeConfig): boolean {
  return asBoolean(config.runtimeConstraints.provider_control_plane_required);
}

export async function resolveProviderControlPlaneTurns(
  input: RuntimeRunInput,
  config: RuntimeConfig,
  tools: ToolSpecContract[],
  emit: EmitRuntimeEvent,
  requestEpoch: number,
  requestRound: number,
  overrideMessages?: readonly JsonObject[],
): Promise<ProviderControlPlaneModelResolution> {
  if (process.env.ZYRA_PROVIDER_CONTROL_PLANE_DISABLED === "1") {
    return failClosed("provider_control_plane_disabled", config.modelName);
  }
  const constraints = config.runtimeConstraints;
  const databasePath = asString(constraints.provider_control_plane_database_path).trim();
  if (!databasePath) {
    return failClosed("provider_control_plane_database_path_missing", config.modelName);
  }
  const routeRef = routeRefFromConstraints(constraints);
  const { ProviderControlPlane } = await import("../../provider-control-plane/src/control-plane.ts");
  const controlPlane = new ProviderControlPlane({ databasePath });
  const dispatchId = [
    "provider_dispatch",
    safeId(input.workerRequestId),
    `epoch${Math.max(0, Math.floor(requestEpoch))}`,
    `round${Math.max(0, Math.floor(requestRound))}`,
  ].join("_");
  try {
    const route = controlPlane.routes.require(routeRef.routeId);
    assertRouteRef(route, routeRef, input);
    const promptMessages = overrideMessages ? [...overrideMessages] : normalizeMessages(input);
    const messages = normalizeProviderMessages(promptMessages);
    const providerTools = normalizeProviderTools(tools);
    const request: ProviderDispatchRequest = {
      dispatchId,
      routeId: route.routeId,
      runId: input.runId,
      taskId: input.taskId,
      nodeId: input.nodeId ?? null,
      sessionId: route.sessionId,
      turnId: route.turnId,
      // The host relays exactly the credential pinned by this route. Cross-route
      // fallback would select a credential that is deliberately absent from the
      // worker process, so transient failures may retry only this route.
      routeFallbackPolicy: "pin_initial_route",
      messages,
      tools: providerTools,
      maximumOutputTokens: boundedPositiveInteger(
        constraints.model_output_token_limit,
        8_192,
        route.requestDefaults.max_output_tokens,
      ),
      temperature: optionalTemperature(constraints.model_temperature),
      stream: true,
      timeoutMilliseconds: boundedPositiveInteger(
        constraints.model_api_timeout_milliseconds,
        120_000,
        numberValue(constraints.model_api_timeout_seconds) * 1_000,
      ),
      chunkTimeoutMilliseconds: boundedPositiveInteger(
        constraints.model_chunk_timeout_milliseconds,
        30_000,
      ),
      idempotencyKey: `${input.workerRequestId}:provider:${requestEpoch}:${requestRound}`,
      extraBody: asObject(constraints.provider_extra_body),
      metadata: {
        owner: "typescript.ProviderControlPlane",
        workerRequestId: input.workerRequestId,
        requestEpoch,
        requestRound,
        backendEnvelopeId: asString(asObject(input.metadata).backend_dispatch_envelope_id) || null,
        backendLeaseId: asString(asObject(input.metadata).backend_lease_id) || null,
        m0ExecutionRef: asString(asObject(input.metadata).m0_execution_ref) || null,
      },
    };
    await emit("model_request_prepared", {
      provider_request: {
        request_id: dispatchId,
        provider: route.providerId,
        model: route.modelId,
        route_id: route.routeId,
        route_checksum: route.checksum,
        catalog_revision: route.catalogRevision,
        credential_version: route.credentialVersion,
        credential_fingerprint: route.credentialFingerprint,
        transport_id: route.transportId,
        message_count: request.messages.length,
        tool_count: request.tools.length,
        messages_digest: digestJson(promptMessages),
        initial_user_message_digest: initialUserMessageDigest(promptMessages),
        tools_digest: digestJson(request.tools),
        // The E01 provider lifecycle consumes the full canonical prompt before
        // QueryEngine emits a commitment-only public runtime event.
        messages: promptMessages,
        tools: request.tools.map((tool) => ({
          type: "function",
          function: {
            name: tool.name,
            description: tool.description,
            parameters: tool.inputSchema,
          },
        })) as unknown as JsonObject[],
        system: [],
        stream: true,
        provider_state_embedded: false,
        secret_bytes_included: false,
      },
    });
    const result = await controlPlane.dispatch(request);
    const steps = toolSteps(result.frames);
    const evidenceFrames = providerControlPlaneEvidenceFrames(result.frames);
    const compactedContentFrames = result.frames.filter(
      (frame) => HIGH_VOLUME_CONTENT_FRAME_KINDS.has(frame.kind),
    );
    for (const frame of evidenceFrames) {
      await emit("model_stream_frame", {
        model_stream_frame: safeFrame(frame),
      });
    }
    const routeChanged = result.routeId !== route.routeId;
    const succeededAttempt = [...result.attempts]
      .reverse()
      .find((attempt) => attempt.outcome === "succeeded");
    await emit("model_stream_report", {
      model_stream: {
        request_id: dispatchId,
        provider_request_id: result.dispatchId,
        provider_attempt_id: succeededAttempt?.attemptId ?? null,
        provider_request_digest: succeededAttempt?.requestDigest ?? null,
        provider: result.providerId,
        model: result.modelId,
        route_id: result.routeId,
        initial_route_id: route.routeId,
        provider_route_changed: routeChanged,
        transport: "provider_control_plane",
        protocol: result.protocol,
        attempt_count: result.attempts.length,
        frame_count: result.frames.length,
        evidence_frame_count: evidenceFrames.length,
        compacted_content_frame_count: compactedContentFrames.length,
        compacted_content_digest: digestJson(compactedContentFrames.map((frame) => ({
          sequence: frame.sequence,
          kind: frame.kind,
          text: frame.text,
        }))),
        stream_evidence_compacted: evidenceFrames.length !== result.frames.length,
        tool_call_count: steps.length,
        stop_reason: result.stopReason,
        usage: result.usage,
        ok: true,
      },
    });
    return {
      ok: true,
      turns: steps.length > 0 ? [steps] : [],
      metadata: {
        model_stream_ok: "true",
        api_retry_ok: "true",
        api_retry_status: routeChanged ? "provider_route_changed" : "primary_selected",
        api_retry_fallback_used: String(routeChanged),
        api_retry_final_model: result.modelId,
        api_retry_recovered: "true",
        api_retry_playbook_ok: "true",
        api_retry_playbook_status: routeChanged ? "recovered" : "not_needed",
        model_stream_watchdog_ok: "true",
        model_stream_watchdog_status: "healthy",
        runtime_budget_state_retry_count: String(Math.max(0, result.attempts.length - 1)),
        runtime_budget_replay_ok: "true",
        provider_control_plane_owner: "typescript.ProviderControlPlane",
        provider_route_id: result.routeId,
        provider_initial_route_id: route.routeId,
        provider_route_changed: String(routeChanged),
        provider_route_checksum: route.checksum,
        provider_catalog_revision: String(route.catalogRevision),
        provider_credential_version: String(route.credentialVersion),
        provider_credential_fingerprint: route.credentialFingerprint,
        provider_transport_id: route.transportId,
        provider_attempt_count: String(result.attempts.length),
      },
      error: null,
      finalText: result.text,
      stopReason: result.stopReason,
      providerRequestId: result.dispatchId,
    };
  } catch (error) {
    const safe = error instanceof ProviderControlPlaneError
      ? error.safe()
      : { kind: "provider_control_plane_unavailable", message: errorMessage(error) };
    await emit("model_stream_report", {
      model_stream: {
        request_id: dispatchId,
        route_id: routeRef.routeId,
        transport: "provider_control_plane",
        ok: false,
        failure: safe as unknown as JsonObject,
        deterministic_fallback_used: false,
        legacy_provider_fallback_used: false,
      },
    });
    return {
      ...failClosed(asString((safe as { kind?: unknown }).kind, "provider_control_plane_failed"), config.modelName),
      metadata: {
        ...failClosed("provider_control_plane_failed", config.modelName).metadata,
        provider_control_plane_owner: "typescript.ProviderControlPlane",
        provider_route_id: routeRef.routeId,
        provider_route_checksum: routeRef.routeChecksum,
        provider_failure: JSON.stringify(safe),
        deterministic_fallback_used: "false",
        legacy_provider_fallback_used: "false",
      },
    };
  } finally {
    controlPlane.close();
  }
}

export function providerControlPlaneEvidenceFrames(
  frames: readonly ProviderStreamFrame[],
): ProviderStreamFrame[] {
  // The provider control plane already durably owns the complete raw stream.
  // Replaying every text/reasoning token through E01 duplicates that evidence
  // across the process protocol, journal, telemetry and every later snapshot.
  // Preserve structural frames needed to audit tool calls, usage and terminal
  // delivery; the report carries a count and digest for compacted content.
  return frames.filter((frame) => !HIGH_VOLUME_CONTENT_FRAME_KINDS.has(frame.kind));
}

function routeRefFromConstraints(constraints: JsonObject): ProviderRouteRefProjection {
  const ref: ProviderRouteRefProjection = {
    routeId: asString(constraints.provider_route_id).trim(),
    routeChecksum: asString(constraints.provider_route_checksum).trim(),
    catalogRevision: Math.floor(numberValue(constraints.provider_catalog_revision)),
    credentialVersion: Math.floor(numberValue(constraints.provider_credential_version)),
    credentialFingerprint: asString(constraints.provider_credential_fingerprint).trim(),
    transportId: asString(constraints.provider_transport_id).trim(),
    sessionId: asString(constraints.provider_route_session_id).trim(),
    turnId: asString(constraints.provider_route_turn_id).trim(),
  };
  const missing = Object.entries(ref)
    .filter(([, value]) => typeof value === "string" ? !value : value <= 0)
    .map(([name]) => name);
  if (missing.length > 0) {
    throw new Error(`provider route ref is incomplete: ${missing.join(", ")}`);
  }
  return ref;
}

function assertRouteRef(
  route: ProviderRouteLease,
  ref: ProviderRouteRefProjection,
  input: RuntimeRunInput,
): void {
  const checks: Array<[string, unknown, unknown]> = [
    ["routeId", ref.routeId, route.routeId],
    ["checksum", ref.routeChecksum, route.checksum],
    ["catalogRevision", ref.catalogRevision, route.catalogRevision],
    ["credentialVersion", ref.credentialVersion, route.credentialVersion],
    ["credentialFingerprint", ref.credentialFingerprint, route.credentialFingerprint],
    ["transportId", ref.transportId, route.transportId],
    ["sessionId", ref.sessionId, route.sessionId],
    ["turnId", ref.turnId, route.turnId],
    ["runId", input.runId, route.runId],
    ["taskId", input.taskId, route.taskId],
  ];
  const mismatches = checks.filter(([, expected, actual]) => expected !== actual);
  if (mismatches.length > 0) {
    throw new Error(`provider route ref mismatch: ${mismatches.map(([name]) => name).join(", ")}`);
  }
}

function initialUserMessageDigest(messages: readonly JsonObject[]): string {
  const first = messages.find(
    (message) => asString(message.role, "user") === "user",
  );
  if (first === undefined) return "";
  return typeof first.content === "string"
    ? digestText(first.content)
    : digestJson(first.content ?? null);
}

function normalizeMessages(input: RuntimeRunInput): JsonObject[] {
  if (Array.isArray(input.messages) && input.messages.length > 0) {
    return input.messages.map((message) => asObject(message));
  }
  return [{
    role: "user",
    content: asString(asObject(input.metadata).raw_input, "Execute the requested coding task."),
  }];
}

function normalizeProviderMessages(values: readonly JsonObject[]): DispatchMessage[] {
  const messages: DispatchMessage[] = [];
  const toolNames = new Map<string, string>();
  for (const value of values) {
    const role = asString(value.role, "user");
    if (!["system", "developer", "user", "assistant", "tool"].includes(role)) continue;
    const rawContent = value.content;
    if (role === "assistant" && Array.isArray(rawContent)) {
      const content = rawContent.map((raw) => {
        const block = asObject(raw);
        if (asString(block.type) !== "tool_use") return block;
        const id = asString(block.id).trim();
        const name = asString(block.name).trim();
        if (id && name) toolNames.set(id, name);
        return {
          type: "tool_call",
          id,
          name,
          arguments: asObject(block.input),
        };
      });
      messages.push({ role: "assistant", content });
      continue;
    }
    if (role === "user" && Array.isArray(rawContent)) {
      const blocks = rawContent.map((raw) => asObject(raw));
      const toolResults = blocks.filter((block) => asString(block.type) === "tool_result");
      if (toolResults.length > 0) {
        const userBlocks = blocks.filter((block) => asString(block.type) !== "tool_result");
        if (userBlocks.length > 0) messages.push({ role: "user", content: userBlocks });
        for (const block of toolResults) {
          const toolCallId = asString(block.tool_use_id || block.toolCallId).trim();
          if (!toolCallId) throw new Error("tool result message requires tool_use_id");
          messages.push({
            role: "tool",
            content: [block],
            toolCallId,
            name: toolNames.get(toolCallId) ?? "unknown_tool",
          });
        }
        continue;
      }
    }
    const content = typeof rawContent === "string"
      ? rawContent
      : Array.isArray(rawContent)
        ? rawContent as unknown as JsonObject[]
        : JSON.stringify(rawContent ?? "");
    messages.push({
      role: role as DispatchMessage["role"],
      content,
      ...(asString(value.name) ? { name: asString(value.name) } : {}),
      ...(asString(value.tool_call_id || value.toolCallId)
        ? { toolCallId: asString(value.tool_call_id || value.toolCallId) }
        : {}),
    });
  }
  if (messages.length === 0) throw new Error("ProviderControlPlane dispatch requires messages");
  return messages;
}

function normalizeProviderTools(tools: readonly ToolSpecContract[]): DispatchTool[] {
  return tools.map((tool) => ({
    name: tool.name,
    description: tool.purpose,
    inputSchema: tool.input_schema,
  }));
}

function toolSteps(frames: readonly ProviderStreamFrame[]): ToolStep[] {
  const accumulators = new Map<string, ToolAccumulator>();
  for (const frame of frames) {
    if (frame.kind !== "tool_call_delta" || frame.toolCallId === null) continue;
    const current = accumulators.get(frame.toolCallId) ?? {
      toolCallId: frame.toolCallId,
      toolName: frame.toolName ?? "",
      json: "",
      firstSequence: frame.sequence,
      lastSequence: frame.sequence,
    };
    if (frame.toolName) current.toolName = frame.toolName;
    if (frame.jsonDelta) current.json += frame.jsonDelta;
    current.lastSequence = frame.sequence;
    accumulators.set(frame.toolCallId, current);
  }
  return [...accumulators.values()]
    .sort((left, right) => left.firstSequence - right.firstSequence)
    .map((item) => ({
      tool_name: item.toolName || "unknown_tool",
      arguments: parseArguments(item.json),
      step_id: item.toolCallId,
      metadata: {
        provider_route_owner: "typescript.ProviderControlPlane",
        first_sequence: item.firstSequence,
        last_sequence: item.lastSequence,
        arguments_digest: digestJson(parseArguments(item.json)),
      },
    }));
}

function parseArguments(value: string): JsonObject {
  if (!value.trim()) return {};
  try {
    const parsed = JSON.parse(value) as unknown;
    return asObject(parsed);
  } catch {
    return { _provider_invalid_json: value };
  }
}

function safeFrame(frame: ProviderStreamFrame): JsonObject {
  return {
    frame_id: frame.frameId,
    dispatch_id: frame.dispatchId,
    route_id: frame.routeId,
    sequence: frame.sequence,
    kind: frame.kind,
    text: frame.text,
    tool_call_id: frame.toolCallId,
    tool_name: frame.toolName,
    json_delta: frame.jsonDelta,
    usage: frame.usage,
    provider_event: frame.providerEvent,
    created_at: frame.createdAt,
    metadata: frame.metadata,
  };
}

function failClosed(error: string, model: string): ProviderControlPlaneModelResolution {
  return {
    ok: false,
    turns: [],
    metadata: {
      model_stream_ok: "false",
      api_retry_ok: "false",
      api_retry_status: "provider_control_plane_failed",
      api_retry_fallback_used: "false",
      api_retry_final_model: model,
      api_retry_recovered: "false",
      api_retry_playbook_ok: "false",
      api_retry_playbook_status: "exhausted",
      model_stream_watchdog_ok: "false",
      model_stream_watchdog_status: "exhausted",
      runtime_budget_state_retry_count: "0",
      runtime_budget_replay_ok: "false",
      deterministic_fallback_used: "false",
      legacy_provider_fallback_used: "false",
    },
    error,
    finalText: "",
    stopReason: "model_stream_failed",
    providerRequestId: null,
  };
}

function boundedPositiveInteger(value: unknown, fallback: number, secondFallback?: unknown): number {
  const candidate = Math.floor(numberValue(value) || numberValue(secondFallback) || fallback);
  return Math.max(1, Math.min(candidate, 1_000_000));
}

function optionalTemperature(value: unknown): number | null {
  const parsed = numberValue(value);
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 2 ? parsed : null;
}

function safeId(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]/gu, "_").slice(0, 120) || "request";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function numberValue(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}
