import type { JsonRecord, JsonValue } from "../canonical.ts";
import { canonicalize, deepClone } from "../canonical.ts";
import type {
  DispatchTool,
  ProviderDispatchRequest,
  ProviderRouteLease,
} from "../contracts.ts";
import { ProviderControlPlaneError } from "../errors.ts";
import {
  normalizeDispatchMessages,
  type AssistantBlock,
  type AssistantMessage,
  type Message,
  type ToolCall,
  type ToolResultMessage,
} from "./message-normalizer.ts";

// Productized codec control flow is cropped from oh-my-pi provider transforms
// and openai-shared.ts at c6b83c1d. Wire declaration files are deliberately
// not counted as implementation; this module owns executable normalization.

export interface ProviderCodecEvidence {
  readonly protocol: ProviderRouteLease["protocol"];
  readonly messageCount: number;
  readonly assistantToolCallCount: number;
  readonly toolResultCount: number;
  readonly synthesizedToolResultCount: number;
  readonly thinkingBlockCount: number;
  readonly imageBlockCount: number;
  readonly body: JsonRecord;
}

export function encodeNormalizedProviderBody(
  lease: ProviderRouteLease,
  request: ProviderDispatchRequest,
): { readonly json: string; readonly evidence: ProviderCodecEvidence } {
  const messages = normalizeDispatchMessages(request.messages, lease);
  assertMessageSequence(messages, lease);
  const body = lease.protocol === "openai_chat"
    ? openAiChatBody(lease, request, messages)
    : lease.protocol === "openai_responses"
      ? openAiResponsesBody(lease, request, messages)
      : anthropicBody(lease, request, messages);
  const evidence = codecEvidence(lease, messages, body);
  return { json: JSON.stringify(body), evidence };
}

function openAiChatBody(
  lease: ProviderRouteLease,
  request: ProviderDispatchRequest,
  messages: readonly Message[],
): JsonRecord {
  const encoded: JsonValue[] = [];
  for (const message of messages) {
    if (message.role === "toolResult") {
      encoded.push({
        role: "tool",
        tool_call_id: message.toolCallId,
        content: toolResultText(message),
      });
      continue;
    }
    if (message.role !== "assistant") {
      encoded.push({ role: message.role, content: message.content });
      continue;
    }
    const text = assistantText(message.content);
    const reasoning = assistantThinking(message.content);
    const calls = assistantToolCalls(message.content).map((call) => ({
      id: call.id,
      type: "function",
      function: { name: call.name, arguments: JSON.stringify(call.arguments) },
    }));
    encoded.push({
      role: "assistant",
      content: text || null,
      ...(reasoning ? { reasoning_content: reasoning } : {}),
      ...(calls.length > 0 ? { tool_calls: calls } : {}),
    });
  }
  return canonicalBody(lease, request, {
    model: lease.modelId,
    messages: encoded,
    stream: true,
    stream_options: { include_usage: true },
    // ``openai_chat`` is the compatibility protocol used by Zhipu, DeepSeek,
    // Kimi, and other non-OpenAI chat-completions providers.  Their published
    // wire contract uses ``max_tokens``.  Sending OpenAI's newer
    // ``max_completion_tokens`` spelling is silently ignored by Zhipu and can
    // turn a bounded agent turn into an effectively unbounded reasoning stream.
    max_tokens: request.maximumOutputTokens,
    ...(request.temperature === null ? {} : { temperature: request.temperature }),
    ...encodedTools(request.tools, "openai_chat"),
  });
}

function openAiResponsesBody(
  lease: ProviderRouteLease,
  request: ProviderDispatchRequest,
  messages: readonly Message[],
): JsonRecord {
  const input: JsonValue[] = [];
  for (const message of messages) {
    if (message.role === "toolResult") {
      input.push({
        type: "function_call_output",
        call_id: message.toolCallId,
        output: toolResultText(message),
      });
      continue;
    }
    if (message.role !== "assistant") {
      input.push({ type: "message", role: message.role, content: message.content });
      continue;
    }
    const text = assistantText(message.content);
    if (text) input.push({ type: "message", role: "assistant", content: text });
    for (const block of message.content) {
      if (block.type === "thinking" && block.thinkingSignature) {
        input.push({
          type: "reasoning",
          encrypted_content: block.thinkingSignature,
          summary: block.thinking ? [{ type: "summary_text", text: block.thinking }] : [],
        });
      }
      if (block.type === "toolCall") {
        const [callId, itemId] = responsesCallIdentity(block.id);
        input.push({
          type: "function_call",
          call_id: callId,
          ...(itemId ? { id: itemId } : {}),
          name: block.name,
          arguments: JSON.stringify(block.arguments),
        });
      }
    }
  }
  return canonicalBody(lease, request, {
    model: lease.modelId,
    input,
    stream: true,
    max_output_tokens: request.maximumOutputTokens,
    ...(request.temperature === null ? {} : { temperature: request.temperature }),
    ...encodedTools(request.tools, "openai_responses"),
  });
}

function anthropicBody(
  lease: ProviderRouteLease,
  request: ProviderDispatchRequest,
  messages: readonly Message[],
): JsonRecord {
  const system: string[] = [];
  const encoded: JsonValue[] = [];
  for (const message of messages) {
    if (message.role === "system" || message.role === "developer") {
      system.push(message.content);
      continue;
    }
    if (message.role === "toolResult") {
      encoded.push({
        role: "user",
        content: [{
          type: "tool_result",
          tool_use_id: message.toolCallId,
          content: toolResultText(message),
          is_error: message.isError,
        }],
      });
      continue;
    }
    if (message.role === "user") {
      encoded.push({ role: "user", content: message.content });
      continue;
    }
    if (message.role !== "assistant") continue;
    encoded.push({ role: "assistant", content: anthropicAssistantContent(message) });
  }
  return canonicalBody(lease, request, {
    model: lease.modelId,
    messages: mergeAnthropicUserMessages(encoded),
    max_tokens: request.maximumOutputTokens,
    stream: true,
    ...(system.length === 0 ? {} : { system: system.join("\n\n") }),
    ...(request.temperature === null ? {} : { temperature: request.temperature }),
    ...encodedTools(request.tools, "anthropic_messages"),
  });
}

function anthropicAssistantContent(message: AssistantMessage): JsonValue[] {
  const content: JsonValue[] = [];
  for (const block of message.content) {
    if (block.type === "text") {
      if (block.text) content.push({ type: "text", text: block.text });
      continue;
    }
    if (block.type === "thinking") {
      if (block.thinkingSignature) {
        content.push({
          type: "thinking",
          thinking: block.thinking,
          signature: block.thinkingSignature,
        });
      } else if (block.thinking) {
        content.push({ type: "text", text: `<thinking>\n${block.thinking}\n</thinking>` });
      }
      continue;
    }
    if (block.type === "redactedThinking") {
      if (block.data) content.push({ type: "redacted_thinking", data: block.data });
      continue;
    }
    if (block.type === "fallback") continue;
    if (block.type === "image") {
      content.push(anthropicImage(block.mediaType, block.data));
      continue;
    }
    content.push({
      type: "tool_use",
      id: block.id,
      name: block.name,
      input: deepClone(block.arguments) as JsonRecord,
    });
  }
  return content.length > 0 ? content : [{ type: "text", text: "" }];
}

function anthropicImage(mediaType: string, data: string): JsonRecord {
  if (/^https?:\/\//i.test(data)) {
    return { type: "image", source: { type: "url", url: data } };
  }
  const match = /^data:([^;,]+);base64,(.+)$/s.exec(data);
  if (match) {
    return {
      type: "image",
      source: { type: "base64", media_type: match[1] ?? mediaType, data: match[2] ?? "" },
    };
  }
  return { type: "image", source: { type: "base64", media_type: mediaType, data } };
}

function mergeAnthropicUserMessages(messages: readonly JsonValue[]): JsonValue[] {
  const result: JsonValue[] = [];
  for (const raw of messages) {
    if (!isRecord(raw)) continue;
    const previous = result[result.length - 1];
    if (raw.role !== "user" || !isRecord(previous) || previous.role !== "user") {
      result.push(raw);
      continue;
    }
    const left = anthropicContentArray(previous.content);
    const right = anthropicContentArray(raw.content);
    result[result.length - 1] = { role: "user", content: [...left, ...right] };
  }
  return result;
}

function anthropicContentArray(value: JsonValue | undefined): JsonValue[] {
  if (Array.isArray(value)) return [...value];
  if (typeof value === "string") return [{ type: "text", text: value }];
  return value === undefined ? [] : [value];
}

function encodedTools(
  tools: readonly DispatchTool[],
  protocol: ProviderRouteLease["protocol"],
): JsonRecord {
  if (tools.length === 0) return {};
  if (protocol === "anthropic_messages") {
    return {
      tools: tools.map((tool) => ({
        name: tool.name,
        description: tool.description,
        input_schema: deepClone(tool.inputSchema),
      })),
    };
  }
  if (protocol === "openai_responses") {
    return {
      tools: tools.map((tool) => ({
        type: "function",
        name: tool.name,
        description: tool.description,
        parameters: deepClone(tool.inputSchema),
        strict: true,
      })),
    };
  }
  return {
    tools: tools.map((tool) => ({
      type: "function",
      function: {
        name: tool.name,
        description: tool.description,
        parameters: deepClone(tool.inputSchema),
      },
    })),
  };
}

function canonicalBody(
  lease: ProviderRouteLease,
  request: ProviderDispatchRequest,
  owned: JsonRecord,
): JsonRecord {
  const forbidden = new Set([
    "model",
    "messages",
    "input",
    "stream",
    "tools",
    "max_tokens",
    "max_output_tokens",
    "max_completion_tokens",
  ]);
  const defaults = safeExtensionFields(lease.requestDefaults, forbidden);
  const extras = safeExtensionFields(request.extraBody, forbidden);
  return canonicalize({ ...defaults, ...extras, ...owned }) as JsonRecord;
}

function safeExtensionFields(value: JsonRecord, forbidden: ReadonlySet<string>): JsonRecord {
  const result: Record<string, JsonValue> = {};
  for (const [key, item] of Object.entries(value)) {
    if (forbidden.has(key)) continue;
    result[key] = deepClone(item);
  }
  return result;
}

function assertMessageSequence(messages: readonly Message[], lease: ProviderRouteLease): void {
  const pending = new Set<string>();
  const seen = new Set<string>();
  for (const message of messages) {
    if (message.role === "assistant") {
      for (const call of assistantToolCalls(message.content)) {
        if (seen.has(call.id)) throw codecError(lease, "duplicate normalized tool call id", { toolCallId: call.id });
        seen.add(call.id);
        pending.add(call.id);
      }
      continue;
    }
    if (message.role === "toolResult") {
      if (!seen.has(message.toolCallId)) throw codecError(lease, "orphan tool result survived normalization", { toolCallId: message.toolCallId });
      if (!pending.delete(message.toolCallId)) throw codecError(lease, "duplicate tool result survived normalization", { toolCallId: message.toolCallId });
      continue;
    }
    if (pending.size > 0) throw codecError(lease, "tool result is not contiguous with assistant tool use", { pending: [...pending] });
  }
  if (pending.size > 0) throw codecError(lease, "tool calls remain unresolved after normalization", { pending: [...pending] });
}

function assistantText(blocks: readonly AssistantBlock[]): string {
  return blocks.filter((block) => block.type === "text").map((block) => block.text).join("\n");
}

function assistantThinking(blocks: readonly AssistantBlock[]): string {
  return blocks.filter((block) => block.type === "thinking").map((block) => block.thinking).join("\n");
}

function assistantToolCalls(blocks: readonly AssistantBlock[]): ToolCall[] {
  return blocks.filter((block): block is ToolCall => block.type === "toolCall");
}

function toolResultText(message: ToolResultMessage): string {
  return message.content.map((block) => block.text).join("\n");
}

function responsesCallIdentity(value: string): readonly [string, string] {
  const [callId = "", itemId = ""] = value.split("|", 2);
  return [callId || value, itemId];
}

function codecEvidence(
  lease: ProviderRouteLease,
  messages: readonly Message[],
  body: JsonRecord,
): ProviderCodecEvidence {
  let assistantToolCallCount = 0;
  let toolResultCount = 0;
  let synthesizedToolResultCount = 0;
  let thinkingBlockCount = 0;
  let imageBlockCount = 0;
  for (const message of messages) {
    if (message.role === "toolResult") {
      toolResultCount += 1;
      if (message.isError && ["aborted", "No result provided"].includes(toolResultText(message))) synthesizedToolResultCount += 1;
      continue;
    }
    if (message.role !== "assistant") continue;
    assistantToolCallCount += assistantToolCalls(message.content).length;
    thinkingBlockCount += message.content.filter((block) => block.type === "thinking").length;
    imageBlockCount += message.content.filter((block) => block.type === "image").length;
  }
  return {
    protocol: lease.protocol,
    messageCount: messages.length,
    assistantToolCallCount,
    toolResultCount,
    synthesizedToolResultCount,
    thinkingBlockCount,
    imageBlockCount,
    body: deepClone(body),
  };
}

function codecError(
  lease: ProviderRouteLease,
  message: string,
  detail: JsonRecord,
): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "protocol",
    kind: "response_protocol_error",
    message,
    retryable: false,
    recoveryIntent: "surface_to_operator",
    providerId: lease.providerId,
    modelId: lease.modelId,
    routeId: lease.routeId,
    detail,
  });
}

function isRecord(value: unknown): value is Record<string, JsonValue> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
