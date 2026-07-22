import type { JsonRecord, JsonValue } from "../canonical.ts";
import { canonicalize } from "../canonical.ts";
import type { ProviderStreamFrame, TransportProtocol } from "../contracts.ts";
import { ProviderControlPlaneError } from "../errors.ts";

export interface ProviderFrameFactory {
  readonly outputObserved: boolean;
  stopReason: string;
  usage: JsonRecord;
  frame(
    kind: ProviderStreamFrame["kind"],
    update?: Partial<Omit<ProviderStreamFrame, "frameId" | "dispatchId" | "routeId" | "sequence" | "kind" | "createdAt">>,
  ): ProviderStreamFrame;
}

export function decodeCompleteProviderResponse(
  protocol: TransportProtocol,
  value: Record<string, unknown>,
  state: ProviderFrameFactory,
): ProviderStreamFrame[] | null {
  if (protocol === "openai_chat") return decodeOpenAiChatResponse(value, state);
  if (protocol === "openai_responses") return decodeOpenAiResponsesResponse(value, state);
  return decodeAnthropicResponse(value, state);
}

function decodeOpenAiChatResponse(
  value: Record<string, unknown>,
  state: ProviderFrameFactory,
): ProviderStreamFrame[] | null {
  const choices = arrayOfRecords(value.choices);
  if (choices.length === 0 || !choices.some((choice) => isRecord(choice.message))) return null;
  const frames: ProviderStreamFrame[] = [];
  frames.push(state.frame("response_start", {
    providerEvent: "chat.completion",
    metadata: optionalIdentity(value),
  }));
  const toolIds = new Set<string>();
  for (const choice of choices) {
    const message = record(choice.message);
    if (message === null) continue;
    for (const text of chatContentText(message.content)) {
      if (text) frames.push(state.frame("text_delta", { text, providerEvent: "chat.completion" }));
    }
    for (const thinking of reasoningText(message)) {
      if (thinking) frames.push(state.frame("thinking_delta", { text: thinking, providerEvent: "chat.completion" }));
    }
    for (const tool of arrayOfRecords(message.tool_calls)) {
      const functionCall = record(tool.function);
      const toolCallId = optionalString(tool.id);
      const toolName = optionalString(functionCall?.name);
      if (toolCallId !== null) {
        if (toolIds.has(toolCallId)) throw responseError("duplicate tool call id in OpenAI Chat response", state, { toolCallId });
        toolIds.add(toolCallId);
      }
      const argumentsText = normalizeArguments(functionCall?.arguments);
      if (toolCallId === null && toolName === null && argumentsText === null) continue;
      frames.push(state.frame("tool_call_delta", {
        toolCallId,
        toolName,
        jsonDelta: argumentsText,
        providerEvent: "chat.completion",
        metadata: {
          providerIndex: numberOrString(tool.index) ?? "",
          finishReason: optionalString(choice.finish_reason) ?? "",
        },
      }));
    }
    const finishReason = optionalString(choice.finish_reason);
    if (finishReason !== null) state.stopReason = finishReason;
  }
  appendUsage(frames, value.usage, state, "chat.completion");
  frames.push(state.frame("response_end", { providerEvent: "chat.completion" }));
  return frames;
}

function decodeOpenAiResponsesResponse(
  value: Record<string, unknown>,
  state: ProviderFrameFactory,
): ProviderStreamFrame[] | null {
  const response = record(value.response) ?? value;
  const output = arrayOfRecords(response.output);
  const objectType = optionalString(response.object);
  const status = optionalString(response.status);
  if (output.length === 0 && objectType !== "response" && optionalString(response.type) !== "response") return null;
  const frames: ProviderStreamFrame[] = [];
  frames.push(state.frame("response_start", {
    providerEvent: "response",
    metadata: optionalIdentity(response),
  }));
  const toolIds = new Set<string>();
  for (let outputIndex = 0; outputIndex < output.length; outputIndex += 1) {
    const item = output[outputIndex]!;
    const itemType = optionalString(item.type) ?? "";
    if (itemType === "message") {
      for (const content of arrayOfRecords(item.content)) {
        const type = optionalString(content.type) ?? "";
        const text = optionalString(content.text) ?? optionalString(record(content.text)?.value);
        if ((type === "output_text" || type === "text") && text) {
          frames.push(state.frame("text_delta", {
            text,
            providerEvent: "response.output_text.done",
            metadata: { outputIndex },
          }));
        }
        const refusal = optionalString(content.refusal);
        if (type === "refusal" && refusal) {
          frames.push(state.frame("provider_notice", {
            text: refusal,
            providerEvent: "response.refusal",
            metadata: { outputIndex },
          }));
        }
      }
      continue;
    }
    if (itemType === "reasoning") {
      for (const summary of arrayOfRecords(item.summary)) {
        const text = optionalString(summary.text);
        if (text) frames.push(state.frame("thinking_delta", {
          text,
          providerEvent: "response.reasoning_summary.done",
          metadata: { outputIndex },
        }));
      }
      continue;
    }
    if (itemType === "function_call") {
      const toolCallId = optionalString(item.call_id) ?? optionalString(item.id);
      if (toolCallId !== null) {
        if (toolIds.has(toolCallId)) throw responseError("duplicate tool call id in Responses output", state, { toolCallId });
        toolIds.add(toolCallId);
      }
      frames.push(state.frame("tool_call_delta", {
        toolCallId,
        toolName: optionalString(item.name),
        jsonDelta: normalizeArguments(item.arguments),
        providerEvent: "response.function_call_arguments.done",
        metadata: { outputIndex },
      }));
      continue;
    }
    frames.push(state.frame("provider_notice", {
      providerEvent: `response.output.${itemType || "unknown"}`,
      metadata: {
        outputIndex,
        item: canonicalize(item),
      },
    }));
  }
  appendUsage(frames, response.usage, state, "response.completed");
  state.stopReason = status ?? "completed";
  if (status === "failed" || status === "cancelled" || status === "incomplete") {
    const details = canonicalize(record(response.error) ?? record(response.incomplete_details) ?? {}) as JsonRecord;
    throw responseError(`Responses request finished with status ${status}`, state, details);
  }
  frames.push(state.frame("response_end", { providerEvent: "response.completed" }));
  return frames;
}

function decodeAnthropicResponse(
  value: Record<string, unknown>,
  state: ProviderFrameFactory,
): ProviderStreamFrame[] | null {
  if (optionalString(value.type) !== "message" || !Array.isArray(value.content)) return null;
  const frames: ProviderStreamFrame[] = [];
  frames.push(state.frame("response_start", {
    providerEvent: "message",
    metadata: optionalIdentity(value),
  }));
  const toolIds = new Set<string>();
  for (let index = 0; index < value.content.length; index += 1) {
    const block = record(value.content[index]);
    if (block === null) continue;
    const type = optionalString(block.type) ?? "";
    if (type === "text") {
      const text = optionalString(block.text);
      if (text) frames.push(state.frame("text_delta", {
        text,
        providerEvent: "content_block",
        metadata: { providerIndex: index },
      }));
      continue;
    }
    if (type === "thinking") {
      const thinking = optionalString(block.thinking);
      if (thinking) frames.push(state.frame("thinking_delta", {
        text: thinking,
        providerEvent: "content_block",
        metadata: {
          providerIndex: index,
          signature: optionalString(block.signature) ?? "",
        },
      }));
      continue;
    }
    if (type === "redacted_thinking") {
      frames.push(state.frame("provider_notice", {
        providerEvent: "redacted_thinking",
        metadata: {
          providerIndex: index,
          dataDigest: digestOpaque(optionalString(block.data) ?? ""),
        },
      }));
      continue;
    }
    if (type === "tool_use") {
      const toolCallId = optionalString(block.id);
      if (toolCallId !== null) {
        if (toolIds.has(toolCallId)) throw responseError("duplicate tool use id in Anthropic response", state, { toolCallId });
        toolIds.add(toolCallId);
      }
      frames.push(state.frame("tool_call_delta", {
        toolCallId,
        toolName: optionalString(block.name),
        jsonDelta: normalizeArguments(block.input),
        providerEvent: "content_block",
        metadata: { providerIndex: index },
      }));
      continue;
    }
    frames.push(state.frame("provider_notice", {
      providerEvent: `content_block.${type || "unknown"}`,
      metadata: { providerIndex: index, block: canonicalize(block) },
    }));
  }
  appendUsage(frames, value.usage, state, "message");
  state.stopReason = optionalString(value.stop_reason) ?? "end_turn";
  frames.push(state.frame("response_end", { providerEvent: "message_stop" }));
  return frames;
}

function appendUsage(
  frames: ProviderStreamFrame[],
  raw: unknown,
  state: ProviderFrameFactory,
  providerEvent: string,
): void {
  const usage = record(raw);
  if (usage === null) return;
  state.usage = canonicalize(usage) as JsonRecord;
  frames.push(state.frame("usage", { usage: state.usage, providerEvent }));
}

function chatContentText(value: unknown): string[] {
  if (typeof value === "string") return value ? [value] : [];
  const parts = arrayOfRecords(value);
  const output: string[] = [];
  for (const part of parts) {
    const type = optionalString(part.type) ?? "";
    const text = optionalString(part.text) ?? optionalString(record(part.text)?.value);
    if ((type === "text" || type === "output_text" || type === "") && text) output.push(text);
  }
  return output;
}

function reasoningText(message: Record<string, unknown>): string[] {
  const output: string[] = [];
  for (const key of ["reasoning_content", "reasoning", "thinking"]) {
    const value = message[key];
    if (typeof value === "string" && value) output.push(value);
    else {
      for (const part of arrayOfRecords(value)) {
        const text = optionalString(part.text) ?? optionalString(part.thinking);
        if (text) output.push(text);
      }
    }
  }
  return output;
}

function normalizeArguments(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return null;
  return JSON.stringify(canonicalize(value as JsonValue));
}

function optionalIdentity(value: Record<string, unknown>): JsonRecord {
  return {
    responseId: optionalString(value.id) ?? "",
    model: optionalString(value.model) ?? "",
    created: typeof value.created === "number" ? value.created : 0,
  };
}

function responseError(
  message: string,
  state: ProviderFrameFactory,
  detail: Readonly<Record<string, unknown>>,
): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "protocol",
    kind: state.outputObserved ? "partial_response_observed" : "response_protocol_error",
    message,
    retryable: false,
    recoveryIntent: state.outputObserved ? "reconcile_partial_response" : "surface_to_operator",
    outputObserved: state.outputObserved,
    detail: canonicalize(detail) as JsonRecord,
  });
}

function arrayOfRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function record(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function numberOrString(value: unknown): string | number | null {
  return typeof value === "string" || typeof value === "number" ? value : null;
}

function digestOpaque(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `fnv1a:${(hash >>> 0).toString(16).padStart(8, "0")}`;
}
