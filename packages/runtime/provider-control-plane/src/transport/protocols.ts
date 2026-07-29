import type {
  ProviderDispatchRequest,
  ProviderRouteLease,
  ProviderStreamFrame,
} from "../contracts.ts";
import type { IdFactory, JsonRecord } from "../canonical.ts";
import { canonicalize, deepClone } from "../canonical.ts";
import { ProviderControlPlaneError } from "../errors.ts";
import { encodeNormalizedProviderBody } from "./provider-codecs.ts";
import { decodeCompleteProviderResponse } from "./response-codecs.ts";

export function encodeProviderBody(lease: ProviderRouteLease, request: ProviderDispatchRequest): string {
  return encodeNormalizedProviderBody(lease, request).json;
}

export function protocolHeaders(lease: ProviderRouteLease): Record<string, string> {
  if (lease.protocol === "anthropic_messages") {
    return { "anthropic-version": "2023-06-01", accept: "text/event-stream" };
  }
  return { accept: "text/event-stream" };
}

export function decodeProviderEvent(
  lease: ProviderRouteLease,
  data: string,
  eventName: string | null,
  state: ProtocolFrameState,
): ProviderStreamFrame[] {
  if (data.trim() === "[DONE]") return [state.frame("response_end", { providerEvent: eventName ?? "done" })];
  let decoded: unknown;
  try {
    decoded = JSON.parse(data);
  } catch (error) {
    throw new ProviderControlPlaneError({
      layer: "protocol",
      kind: "response_protocol_error",
      message: "provider emitted malformed JSON in an SSE event",
      retryable: false,
      recoveryIntent: state.outputObserved ? "reconcile_partial_response" : "surface_to_operator",
      routeId: lease.routeId,
      providerId: lease.providerId,
      modelId: lease.modelId,
      outputObserved: state.outputObserved,
      detail: { eventName: eventName ?? "", dataPrefix: data.slice(0, 120) },
    }, { cause: error });
  }
  if (decoded === null || typeof decoded !== "object" || Array.isArray(decoded)) return [];
  const value = decoded as Record<string, unknown>;
  const complete = decodeCompleteProviderResponse(lease.protocol, value, state);
  if (complete !== null) return complete;
  return lease.protocol === "openai_chat"
    ? decodeOpenAiChat(value, eventName, state)
    : lease.protocol === "openai_responses"
      ? decodeOpenAiResponses(value, eventName, state)
      : decodeAnthropic(value, eventName, state);
}

export class ProtocolFrameState {
  private sequence = 0;
  outputObserved = false;
  text = "";
  usage: JsonRecord = {};
  stopReason = "unknown";
  readonly request: ProviderDispatchRequest;
  readonly ids: IdFactory;
  readonly now: () => number;

  get frameCount(): number {
    return this.sequence;
  }

  constructor(
    request: ProviderDispatchRequest,
    ids: IdFactory,
    now: () => number,
  ) {
    this.request = request;
    this.ids = ids;
    this.now = now;
  }

  frame(
    kind: ProviderStreamFrame["kind"],
    update: Partial<Omit<ProviderStreamFrame, "frameId" | "dispatchId" | "routeId" | "sequence" | "kind" | "createdAt">> = {},
  ): ProviderStreamFrame {
    this.sequence += 1;
    const text = update.text ?? null;
    if (kind === "text_delta" && text) {
      this.outputObserved = true;
      this.text += text;
    }
    if (kind === "tool_call_delta") this.outputObserved = true;
    if (kind === "usage" && update.usage) this.usage = canonicalize(update.usage) as JsonRecord;
    return {
      frameId: this.ids.next("provider_frame"),
      dispatchId: this.request.dispatchId,
      routeId: this.request.routeId,
      sequence: this.sequence,
      kind,
      text,
      toolCallId: update.toolCallId ?? null,
      toolName: update.toolName ?? null,
      jsonDelta: update.jsonDelta ?? null,
      usage: deepClone(update.usage ?? {}),
      providerEvent: update.providerEvent ?? null,
      createdAt: this.now(),
      metadata: deepClone(update.metadata ?? {}),
    };
  }
}

function decodeOpenAiChat(value: Record<string, unknown>, eventName: string | null, state: ProtocolFrameState): ProviderStreamFrame[] {
  if (value.error) throw providerProtocolError(value.error, state);
  const frames: ProviderStreamFrame[] = [];
  const choices = Array.isArray(value.choices) ? value.choices : [];
  for (const choice of choices) {
    if (!choice || typeof choice !== "object") continue;
    const item = choice as Record<string, unknown>;
    const delta = item.delta && typeof item.delta === "object" ? item.delta as Record<string, unknown> : {};
    if (typeof delta.content === "string" && delta.content) frames.push(state.frame("text_delta", { text: delta.content, providerEvent: eventName }));
    if (typeof delta.reasoning_content === "string" && delta.reasoning_content) frames.push(state.frame("thinking_delta", { text: delta.reasoning_content, providerEvent: eventName }));
    if (Array.isArray(delta.tool_calls)) {
      for (const raw of delta.tool_calls) {
        if (!raw || typeof raw !== "object") continue;
        const tool = raw as Record<string, unknown>;
        const fn = tool.function && typeof tool.function === "object" ? tool.function as Record<string, unknown> : {};
        frames.push(state.frame("tool_call_delta", {
          toolCallId: typeof tool.id === "string" ? tool.id : null,
          toolName: typeof fn.name === "string" ? fn.name : null,
          jsonDelta: typeof fn.arguments === "string" ? fn.arguments : null,
          providerEvent: eventName,
          metadata: {
            providerIndex: typeof tool.index === "number" || typeof tool.index === "string"
              ? tool.index
              : "",
          },
        }));
      }
    }
    if (typeof item.finish_reason === "string") state.stopReason = item.finish_reason;
  }
  if (value.usage && typeof value.usage === "object") frames.push(state.frame("usage", { usage: canonicalize(value.usage) as JsonRecord, providerEvent: eventName }));
  if (frames.length === 0 && typeof value.id === "string" && state.frameCount === 0) {
    frames.push(state.frame("response_start", {
      providerEvent: eventName,
      metadata: { responseId: value.id },
    }));
  }
  return frames;
}

function decodeOpenAiResponses(value: Record<string, unknown>, eventName: string | null, state: ProtocolFrameState): ProviderStreamFrame[] {
  const type = typeof value.type === "string" ? value.type : eventName ?? "";
  if (type === "error" || value.error) throw providerProtocolError(value.error ?? value, state);
  if (type === "response.output_text.delta" && typeof value.delta === "string") return [state.frame("text_delta", { text: value.delta, providerEvent: type })];
  if (type === "response.reasoning_text.delta" && typeof value.delta === "string") return [state.frame("thinking_delta", { text: value.delta, providerEvent: type })];
  if (type === "response.function_call_arguments.delta" && typeof value.delta === "string") {
    return [state.frame("tool_call_delta", {
      toolCallId: typeof value.item_id === "string" ? value.item_id : null,
      toolName: typeof value.name === "string" ? value.name : null,
      jsonDelta: value.delta,
      providerEvent: type,
      metadata: {
        providerIndex: typeof value.output_index === "number" || typeof value.output_index === "string"
          ? value.output_index
          : typeof value.item_id === "string" ? value.item_id : "",
      },
    })];
  }
  if (type === "response.completed") {
    const response = value.response && typeof value.response === "object" ? value.response as Record<string, unknown> : {};
    if (response.usage && typeof response.usage === "object") state.usage = canonicalize(response.usage) as JsonRecord;
    state.stopReason = typeof response.status === "string" ? response.status : "completed";
    return [
      ...(Object.keys(state.usage).length === 0 ? [] : [state.frame("usage", { usage: state.usage, providerEvent: type })]),
      state.frame("response_end", { providerEvent: type }),
    ];
  }
  if (type === "response.created" || type === "response.in_progress") return [state.frame("response_start", { providerEvent: type })];
  return [state.frame("provider_notice", { providerEvent: type, metadata: canonicalize(value) as JsonRecord })];
}

function decodeAnthropic(value: Record<string, unknown>, eventName: string | null, state: ProtocolFrameState): ProviderStreamFrame[] {
  const type = typeof value.type === "string" ? value.type : eventName ?? "";
  if (type === "error" || value.error) throw providerProtocolError(value.error ?? value, state);
  if (type === "message_start") return [state.frame("response_start", { providerEvent: type })];
  if (type === "content_block_start") {
    const block = value.content_block && typeof value.content_block === "object" ? value.content_block as Record<string, unknown> : {};
    if (block.type === "tool_use") return [state.frame("tool_call_delta", {
      toolCallId: typeof block.id === "string" ? block.id : null,
      toolName: typeof block.name === "string" ? block.name : null,
      jsonDelta: block.input && typeof block.input === "object" && Object.keys(block.input as object).length > 0
        ? JSON.stringify(block.input)
        : null,
      providerEvent: type,
      metadata: {
        providerIndex: typeof value.index === "number" || typeof value.index === "string"
          ? value.index
          : "",
      },
    })];
    if (block.type === "text" && typeof block.text === "string" && block.text) return [state.frame("text_delta", { text: block.text, providerEvent: type })];
    return [];
  }
  if (type === "content_block_delta") {
    const delta = value.delta && typeof value.delta === "object" ? value.delta as Record<string, unknown> : {};
    if (delta.type === "text_delta" && typeof delta.text === "string") return [state.frame("text_delta", { text: delta.text, providerEvent: type })];
    if (delta.type === "thinking_delta" && typeof delta.thinking === "string") return [state.frame("thinking_delta", { text: delta.thinking, providerEvent: type })];
    if (delta.type === "input_json_delta" && typeof delta.partial_json === "string") return [state.frame("tool_call_delta", {
      jsonDelta: delta.partial_json,
      providerEvent: type,
      metadata: {
        providerIndex: typeof value.index === "number" || typeof value.index === "string"
          ? value.index
          : "",
      },
    })];
    return [];
  }
  if (type === "message_delta") {
    const delta = value.delta && typeof value.delta === "object" ? value.delta as Record<string, unknown> : {};
    if (typeof delta.stop_reason === "string") state.stopReason = delta.stop_reason;
    if (value.usage && typeof value.usage === "object") return [state.frame("usage", { usage: canonicalize(value.usage) as JsonRecord, providerEvent: type })];
    return [];
  }
  if (type === "message_stop") return [state.frame("response_end", { providerEvent: type })];
  return [];
}

function providerProtocolError(value: unknown, state: ProtocolFrameState): ProviderControlPlaneError {
  const detail = value && typeof value === "object" ? canonicalize(value) as JsonRecord : { value: String(value) };
  return new ProviderControlPlaneError({
    layer: "protocol",
    kind: state.outputObserved ? "partial_response_observed" : "response_protocol_error",
    message: typeof (value as { message?: unknown })?.message === "string" ? String((value as { message: string }).message) : "provider stream emitted an error event",
    retryable: false,
    recoveryIntent: state.outputObserved ? "reconcile_partial_response" : "surface_to_operator",
    routeId: state.request.routeId,
    outputObserved: state.outputObserved,
    detail,
  });
}
