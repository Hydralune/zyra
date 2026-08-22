import type {
  DispatchMessage,
  DispatchTool,
  ProviderDispatchRequest,
  ProviderDispatchResult,
  ProviderRouteLease,
  ProviderStreamFrame,
} from "../../provider-control-plane/src/contracts.ts";
import { ProviderControlPlaneError } from "../../provider-control-plane/src/errors.ts";
import {
  digestJson,
  digestText,
  type JsonRecord,
} from "../../provider-control-plane/src/canonical.ts";
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
  reasoningText?: string;
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
    const providerTimeoutMilliseconds = boundedPositiveInteger(
      constraints.model_api_timeout_milliseconds,
      120_000,
      numberValue(constraints.model_api_timeout_seconds) * 1_000,
    );
    const configuredStreamTotalTimeoutMilliseconds = numberValue(
      constraints.model_stream_total_timeout_milliseconds,
    ) || numberValue(constraints.model_stream_total_timeout_seconds) * 1_000;
    // Route lookup happens before the durable model-request checkpoint is
    // written.  Large long-horizon checkpoints can take tens of seconds, so
    // accepting a route that is technically live but nearly expired races the
    // later transport gate.  Reserve the provider timeout plus one minute;
    // the route store renews the pinned identity without changing provider or
    // credential custody.
    const routeMinimumValidityMilliseconds = Math.min(
      600_000,
      Math.max(60_000, providerTimeoutMilliseconds + 60_000),
    );
    const persistedRoute = controlPlane.routes.requirePersisted(routeRef.routeId);
    assertRouteRef(persistedRoute, routeRef, input);
    const route = controlPlane.renewExpiredRoute(
      routeRef.routeId,
      routeMinimumValidityMilliseconds,
    );
    if (route.routeId === persistedRoute.routeId) {
      assertRouteRef(route, routeRef, input);
    } else {
      const renewalDepth = assertProviderRouteRenewalLineage(
        route,
        persistedRoute,
        input,
        (routeId) => controlPlane.routes.requirePersisted(routeId),
      );
      await emit("provider_route_renewed", {
        provider_route: {
          previous_route_id: route.previousRouteId,
          original_route_id: persistedRoute.routeId,
          route_id: route.routeId,
          route_checksum: route.checksum,
          expires_at: route.expiresAt,
          provider: route.providerId,
          model: route.modelId,
          renewal_depth: renewalDepth,
        },
      });
    }
    const promptMessages = overrideMessages ? [...overrideMessages] : normalizeMessages(input);
    const messages = normalizeProviderMessages(promptMessages);
    const providerTools = normalizeProviderTools(tools);
    const modelDefinition = controlPlane.catalog.model(route.providerId, route.modelId);
    const providerDefinition = controlPlane.catalog.provider(route.providerId);
    const requestedOutputTokens = boundedPositiveInteger(
      constraints.model_output_token_limit,
      modelDefinition.maximumOutputTokens,
      route.requestDefaults.max_output_tokens,
    );
    const providerOutputCap = boundedPositiveInteger(
      providerDefinition.metadata.maximum_output_tokens,
      modelDefinition.maximumOutputTokens,
    );
    const effectiveOutputTokens = Math.min(
      requestedOutputTokens,
      modelDefinition.maximumOutputTokens,
      providerOutputCap,
    );
    const configuredBudget = asObject(constraints.model_output_token_budget);
    const outputTokenBudget: JsonObject = {
      schema: "zyra.model-output-token-budget/v1",
      requested: requestedOutputTokens,
      effective: effectiveOutputTokens,
      model_cap: modelDefinition.maximumOutputTokens,
      provider_cap: providerOutputCap,
      requested_source: asString(configuredBudget.requested_source, "model-catalog"),
      model_cap_source: `provider-catalog://${route.providerId}/${route.modelId}`,
      provider_cap_source: providerDefinition.metadata.maximum_output_tokens
        ? `provider-profile://${route.providerId}`
        : `provider-catalog://${route.providerId}/${route.modelId}`,
    };
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
      maximumOutputTokens: effectiveOutputTokens,
      maximumAttempts: boundedPositiveInteger(
        constraints.api_retry_max_attempts,
        route.retryPolicy.maximumAttempts,
      ),
      temperature: optionalTemperature(constraints.model_temperature),
      stream: true,
      timeoutMilliseconds: providerTimeoutMilliseconds,
      ...(configuredStreamTotalTimeoutMilliseconds > 0
        ? {
          streamTotalTimeoutMilliseconds: boundedPositiveInteger(
            configuredStreamTotalTimeoutMilliseconds,
            providerTimeoutMilliseconds,
          ),
        }
        : {}),
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
        output_token_budget: outputTokenBudget,
      },
    });
    const result = await controlPlane.dispatch(request);
    const steps = providerControlPlaneToolSteps(result.frames);
    const evidenceFrames = providerControlPlaneEvidenceFrames(result.frames);
    const compactedContentFrames = result.frames.filter(
      (frame) => HIGH_VOLUME_CONTENT_FRAME_KINDS.has(frame.kind),
    );
    const compactedToolArgumentFrames = result.frames.filter(
      (frame) => frame.kind === "tool_call_delta",
    );
    for (const frame of evidenceFrames) {
      await emit("model_stream_frame", {
        model_stream_frame: safeFrame(frame),
      });
    }
    const routeChanged = route.routeId !== routeRef.routeId || result.routeId !== route.routeId;
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
        initial_route_id: routeRef.routeId,
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
        compacted_tool_argument_frame_count: compactedToolArgumentFrames.length,
        compacted_tool_argument_digest: digestJson(compactedToolArgumentFrames.map((frame) => ({
          sequence: frame.sequence,
          tool_call_id: frame.toolCallId,
          tool_name: frame.toolName,
          json_delta: frame.jsonDelta,
          provider_index: frame.metadata.providerIndex ?? null,
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
        provider_initial_route_id: routeRef.routeId,
        provider_route_changed: String(routeChanged),
        provider_route_checksum: route.checksum,
        provider_catalog_revision: String(route.catalogRevision),
        provider_credential_version: String(route.credentialVersion),
        provider_credential_fingerprint: route.credentialFingerprint,
        provider_transport_id: route.transportId,
        provider_attempt_count: String(result.attempts.length),
        provider_input_tokens: String(nonnegativeUsage(result.usage.input_tokens ?? result.usage.prompt_tokens)),
        provider_output_tokens: String(nonnegativeUsage(result.usage.output_tokens ?? result.usage.completion_tokens)),
        provider_total_tokens: String(providerTotalUsage(result.usage)),
      },
      error: null,
      finalText: result.text,
      reasoningText: result.frames
        .filter((frame) => frame.kind === "thinking_delta")
        .map((frame) => frame.text ?? "")
        .join(""),
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

function nonnegativeUsage(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.max(0, Math.floor(numeric)) : 0;
}

function providerTotalUsage(usage: JsonRecord): number {
  const input = nonnegativeUsage(usage.input_tokens ?? usage.prompt_tokens);
  const output = nonnegativeUsage(usage.output_tokens ?? usage.completion_tokens);
  return nonnegativeUsage(usage.total_tokens) || input + output;
}

export function providerControlPlaneEvidenceFrames(
  frames: readonly ProviderStreamFrame[],
): ProviderStreamFrame[] {
  // The provider control plane already durably owns the complete raw stream.
  // Replaying every text/reasoning token through E01 duplicates that evidence
  // across the process protocol, journal, telemetry and every later snapshot.
  // Tool arguments have the same amplification problem: a single file write
  // can arrive as hundreds of one-character deltas. Preserve one sanitized
  // structural frame per tool call while the report carries counts and digests
  // for the compacted content and argument streams.
  const toolFrames = new Map<string, ProviderStreamFrame>();
  for (const frame of frames) {
    if (frame.kind !== "tool_call_delta") continue;
    const providerIndex = providerToolIndex(frame);
    const key = providerIndex ?? (frame.toolCallId?.trim() ? `id:${frame.toolCallId.trim()}` : "");
    if (!key) continue;
    const current = toolFrames.get(key);
    if (!current || (!current.toolCallId && frame.toolCallId) || (!current.toolName && frame.toolName)) {
      toolFrames.set(key, { ...frame, jsonDelta: null });
    }
  }
  const structuralToolFrames = new Set([...toolFrames.values()].map((frame) => frame.frameId));
  return frames
    .filter((frame) => (
      !HIGH_VOLUME_CONTENT_FRAME_KINDS.has(frame.kind)
      && (frame.kind !== "tool_call_delta" || structuralToolFrames.has(frame.frameId))
    ))
    .map((frame) => frame.kind === "tool_call_delta" ? { ...frame, jsonDelta: null } : frame);
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

function assertRenewedRoute(
  route: ProviderRouteLease,
  previous: ProviderRouteLease,
  input: Pick<RuntimeRunInput, "runId" | "taskId">,
): void {
  const checks: Array<[string, unknown, unknown]> = [
    ["previousRouteId", previous.routeId, route.previousRouteId],
    ["catalogRevision", previous.catalogRevision, route.catalogRevision],
    ["credentialId", previous.credentialId, route.credentialId],
    ["credentialVersion", previous.credentialVersion, route.credentialVersion],
    ["credentialFingerprint", previous.credentialFingerprint, route.credentialFingerprint],
    ["transportId", previous.transportId, route.transportId],
    ["sessionId", previous.sessionId, route.sessionId],
    ["turnId", previous.turnId, route.turnId],
    ["providerId", previous.providerId, route.providerId],
    ["modelId", previous.modelId, route.modelId],
    ["runId", input.runId, route.runId],
    ["taskId", input.taskId, route.taskId],
  ];
  const mismatches = checks.filter(([, expected, actual]) => expected !== actual);
  if (mismatches.length > 0) {
    throw new Error(`renewed provider route mismatch: ${mismatches.map(([name]) => name).join(", ")}`);
  }
}

export function assertProviderRouteRenewalLineage(
  route: ProviderRouteLease,
  ancestor: ProviderRouteLease,
  input: Pick<RuntimeRunInput, "runId" | "taskId">,
  requirePersisted: (routeId: string) => ProviderRouteLease,
): number {
  if (route.routeId === ancestor.routeId) return 0;
  const seen = new Set<string>();
  let current = route;
  // Route ids are immutable and every hop must move to an earlier creation
  // time.  The bound protects recovery from corrupted or cyclic stores even
  // when a provider session has renewed many times during a long execution.
  const maximumDepth = 1_024;
  for (let depth = 1; depth <= maximumDepth; depth += 1) {
    if (seen.has(current.routeId)) {
      throw new Error("renewed provider route lineage contains a cycle");
    }
    seen.add(current.routeId);
    const parentId = current.previousRouteId;
    if (parentId === null || parentId.length === 0) {
      throw new Error("renewed provider route lineage does not reach the pinned route");
    }
    const parent = requirePersisted(parentId);
    if (parent.createdAt >= current.createdAt) {
      throw new Error("renewed provider route lineage is not monotonic");
    }
    assertRenewedRoute(current, parent, input);
    if (parent.routeId === ancestor.routeId) return depth;
    current = parent;
  }
  throw new Error("renewed provider route lineage exceeds the recovery bound");
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
  return tools
    .filter((tool) => tool.metadata.internal_error_sink !== "true")
    .map((tool) => ({
    name: tool.name,
    description: tool.purpose,
    inputSchema: tool.input_schema,
    }));
}

export function providerControlPlaneToolSteps(frames: readonly ProviderStreamFrame[]): ToolStep[] {
  const accumulators = new Map<string, ToolAccumulator>();
  const providerIndexOwners = new Map<string, string>();
  for (const frame of frames) {
    if (frame.kind !== "tool_call_delta") continue;
    const providerIndex = providerToolIndex(frame);
    const explicitToolCallId = frame.toolCallId?.trim() || null;
    if (providerIndex !== null && explicitToolCallId !== null) {
      providerIndexOwners.set(providerIndex, explicitToolCallId);
    }
    const toolCallId = explicitToolCallId
      ?? (providerIndex === null ? null : providerIndexOwners.get(providerIndex) ?? null);
    if (toolCallId === null) continue;
    const current = accumulators.get(toolCallId) ?? {
      toolCallId,
      toolName: frame.toolName ?? "",
      json: "",
      firstSequence: frame.sequence,
      lastSequence: frame.sequence,
    };
    if (frame.toolName) current.toolName = frame.toolName;
    if (frame.jsonDelta) current.json += frame.jsonDelta;
    current.lastSequence = frame.sequence;
    accumulators.set(toolCallId, current);
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

function providerToolIndex(frame: ProviderStreamFrame): string | null {
  const value = frame.metadata.providerIndex;
  if (typeof value === "number" && Number.isFinite(value)) return `number:${value}`;
  if (typeof value === "string" && value.trim()) return `string:${value.trim()}`;
  return null;
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
