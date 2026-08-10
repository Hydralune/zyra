import { createHash } from "node:crypto";

import { asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const COMPATIBLE_PROTOCOL_VERSION = "zyra.provider-compatible/v1";

export type CompatibleRole = "system" | "user" | "assistant" | "tool";

export interface CompatibleToolDefinition {
  name: string;
  description: string | null;
  inputSchema: JsonObject;
}

export interface CompatibleRequestInput {
  baseUrl: string;
  model: string;
  messages: readonly JsonObject[];
  tools?: readonly JsonObject[];
  maximumTokens?: number;
  temperature?: number | null;
  stream?: boolean;
  metadata?: JsonObject;
}

export interface CompatibleRequestEnvelope {
  protocol: typeof COMPATIBLE_PROTOCOL_VERSION;
  url: string;
  headers: Record<string, string>;
  body: JsonObject;
  requestDigest: string;
}

export interface CompatibleToolCall {
  id: string;
  name: string;
  input: JsonObject;
  rawArguments: string;
  parseError: string | null;
  repaired: boolean;
  originalName: string;
}

export interface CompatibleUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
}

export interface CompatibleResponse {
  protocol: typeof COMPATIBLE_PROTOCOL_VERSION;
  id: string;
  model: string;
  finalText: string;
  toolCalls: CompatibleToolCall[];
  finishReason: string | null;
  usage: CompatibleUsage;
  normalized: JsonObject;
  responseDigest: string;
  eventCount: number;
}

export interface CompatibleStreamEvent {
  event: string;
  data: JsonValue;
  sequence: number;
}

export class CompatibleProtocolError extends Error {
  readonly code: string;
  readonly details: JsonObject;

  constructor(code: string, message: string, details: JsonObject = {}) {
    super(message);
    this.name = "CompatibleProtocolError";
    this.code = code;
    this.details = details;
  }
}

interface MutableToolCall {
  id: string;
  name: string;
  arguments: string;
  index: number;
}

interface MutableResponseState {
  id: string;
  model: string;
  text: string;
  finishReason: string | null;
  usage: CompatibleUsage;
  toolCalls: Map<number, MutableToolCall>;
  eventCount: number;
}

function hash(value: unknown): string {
  return createHash("sha256").update(stableStringify(value)).digest("hex");
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((entry) => stableStringify(entry)).join(",")}]`;
  }
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`)
    .join(",")}}`;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function objectValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function jsonObject(value: unknown): JsonObject {
  return objectValue(value) as JsonObject;
}

function redactHeaderValue(name: string, value: string): string {
  const normalized = name.toLowerCase();
  if (normalized.includes("authorization") || normalized.includes("api-key") || normalized.includes("token")) {
    return `<redacted:${hash(value).slice(0, 12)}>`;
  }
  return value;
}

function ensureNonEmpty(value: string, label: string): string {
  const normalized = value.trim();
  if (!normalized) {
    throw new CompatibleProtocolError("compatible_invalid_request", `${label} must not be empty`, { label });
  }
  return normalized;
}

function normalizeBaseUrl(baseUrl: string): URL {
  const normalized = ensureNonEmpty(baseUrl, "baseUrl");
  let parsed: URL;
  try {
    parsed = new URL(normalized);
  } catch (error) {
    throw new CompatibleProtocolError("compatible_invalid_base_url", `invalid compatible provider base URL: ${normalized}`, {
      cause: error instanceof Error ? error.message : String(error),
    });
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new CompatibleProtocolError("compatible_invalid_base_url", "compatible provider URL must use http or https", {
      protocol: parsed.protocol,
    });
  }
  parsed.hash = "";
  parsed.search = "";
  return parsed;
}

export function compatibleRequestUrl(baseUrl: string): string {
  const parsed = normalizeBaseUrl(baseUrl);
  const path = parsed.pathname.replace(/\/+$/, "");
  if (/\/chat\/completions$/i.test(path)) {
    parsed.pathname = path;
  } else if (/\/v1$/i.test(path)) {
    parsed.pathname = `${path}/chat/completions`;
  } else if (path === "" || path === "/") {
    parsed.pathname = "/v1/chat/completions";
  } else {
    parsed.pathname = `${path}/v1/chat/completions`;
  }
  return parsed.toString();
}

export function compatibleHeaders(
  apiKey: string | null | undefined,
  supplied: Readonly<Record<string, string>> = {},
): Record<string, string> {
  const headers: Record<string, string> = {
    accept: "text/event-stream, application/json",
    "content-type": "application/json",
  };
  for (const [name, value] of Object.entries(supplied)) {
    const normalized = name.trim().toLowerCase();
    if (!normalized || normalized === "anthropic-version" || normalized === "x-api-key") {
      continue;
    }
    headers[normalized] = value;
  }
  if (apiKey && !headers.authorization) {
    headers.authorization = `Bearer ${apiKey}`;
  }
  return headers;
}

export function compatibleHeaderReceipt(headers: Readonly<Record<string, string>>): JsonObject {
  const receipt: JsonObject = {};
  for (const [name, value] of Object.entries(headers)) {
    receipt[name.toLowerCase()] = redactHeaderValue(name, value);
  }
  return receipt;
}

function contentBlocks(content: unknown): Record<string, unknown>[] {
  if (Array.isArray(content)) {
    return content.map((entry) => objectValue(entry));
  }
  if (typeof content === "string") {
    return [{ type: "text", text: content }];
  }
  if (content === null || content === undefined) {
    return [];
  }
  return [objectValue(content)];
}

function blockText(block: Record<string, unknown>): string {
  if (typeof block.text === "string") {
    return block.text;
  }
  if (typeof block.content === "string") {
    return block.content;
  }
  return "";
}

function stringifyToolResultContent(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    const text = value
      .map((entry) => {
        const block = objectValue(entry);
        return blockText(block) || stableStringify(entry);
      })
      .filter(Boolean)
      .join("\n");
    return text || "null";
  }
  return stableStringify(value ?? null);
}

function compatibleAssistantMessage(message: Record<string, unknown>): JsonObject[] {
  const blocks = contentBlocks(message.content);
  const text = blocks
    .filter((block) => block.type === "text" || typeof block.text === "string")
    .map((block) => blockText(block))
    .filter(Boolean)
    .join("\n");
  const calls: JsonObject[] = [];
  for (const block of blocks) {
    if (block.type !== "tool_use") {
      continue;
    }
    const id = stringValue(block.id) || `call_${hash(block).slice(0, 16)}`;
    const name = ensureNonEmpty(stringValue(block.name), "tool name");
    calls.push({
      id,
      type: "function",
      function: {
        name,
        arguments: stableStringify(objectValue(block.input)),
      },
    });
  }
  const output: JsonObject = {
    role: "assistant",
    content: text || (calls.length === 0 ? "" : null),
  };
  if (calls.length > 0) {
    output.tool_calls = calls;
  }
  return [output];
}

function compatibleUserMessages(message: Record<string, unknown>): JsonObject[] {
  const blocks = contentBlocks(message.content);
  const output: JsonObject[] = [];
  const ordinary: JsonValue[] = [];
  for (const block of blocks) {
    if (block.type !== "tool_result") {
      ordinary.push(block as JsonObject);
      continue;
    }
    const callId = ensureNonEmpty(
      stringValue(block.tool_use_id) || stringValue(block.tool_call_id),
      "tool result call id",
    );
    output.push({
      role: "tool",
      tool_call_id: callId,
      content: stringifyToolResultContent(block.content),
    });
  }
  if (ordinary.length > 0 || output.length === 0) {
    const plainText = ordinary
      .map((entry) => blockText(objectValue(entry)) || stableStringify(entry))
      .filter(Boolean)
      .join("\n");
    output.unshift({ role: "user", content: plainText });
  }
  return output;
}

export function compatibleMessages(messages: readonly JsonObject[]): JsonObject[] {
  const output: JsonObject[] = [];
  for (const original of messages) {
    const message = objectValue(original);
    const role = stringValue(message.role, "user").toLowerCase();
    if (role === "assistant") {
      output.push(...compatibleAssistantMessage(message));
      continue;
    }
    if (role === "tool") {
      output.push({
        role: "tool",
        tool_call_id: ensureNonEmpty(
          stringValue(message.tool_call_id) || stringValue(message.tool_use_id),
          "tool result call id",
        ),
        content: stringifyToolResultContent(message.content),
      });
      continue;
    }
    if (role === "system") {
      output.push({ role: "system", content: stringifyToolResultContent(message.content) });
      continue;
    }
    output.push(...compatibleUserMessages(message));
  }
  if (output.length === 0) {
    throw new CompatibleProtocolError("compatible_missing_messages", "compatible request requires at least one message");
  }
  return output;
}

export function compatibleTools(tools: readonly JsonObject[] = []): JsonObject[] {
  const output: JsonObject[] = [];
  const names = new Set<string>();
  for (const source of tools) {
    const tool = objectValue(source);
    const metadata = objectValue(tool.metadata);
    if (stringValue(metadata.internal_error_sink).toLowerCase() === "true") continue;
    const functionRecord = objectValue(tool.function);
    const name = ensureNonEmpty(stringValue(tool.name) || stringValue(functionRecord.name), "tool name");
    if (names.has(name)) {
      throw new CompatibleProtocolError("compatible_duplicate_tool", `duplicate compatible tool: ${name}`, { name });
    }
    names.add(name);
    const inputSchema = objectValue(tool.input_schema);
    const parameters = Object.keys(inputSchema).length > 0 ? inputSchema : objectValue(functionRecord.parameters);
    output.push({
      type: "function",
      function: {
        name,
        description: stringValue(tool.description) || stringValue(functionRecord.description),
        parameters: Object.keys(parameters).length > 0 ? (parameters as JsonObject) : { type: "object", properties: {} },
      },
    });
  }
  return output;
}

export function compatibleRequestBody(input: CompatibleRequestInput): JsonObject {
  const model = ensureNonEmpty(input.model, "model");
  const maximumTokens = Math.max(1, Math.trunc(input.maximumTokens ?? 4096));
  const body: JsonObject = {
    model,
    messages: compatibleMessages(input.messages),
    max_tokens: maximumTokens,
    stream: input.stream !== false,
  };
  const tools = compatibleTools(input.tools);
  if (tools.length > 0) {
    body.tools = tools;
    body.tool_choice = "auto";
  }
  if (input.temperature !== null && input.temperature !== undefined) {
    body.temperature = input.temperature;
  }
  if (input.metadata && Object.keys(input.metadata).length > 0) {
    body.metadata = input.metadata;
  }
  return body;
}

export function createCompatibleEnvelope(
  input: CompatibleRequestInput,
  apiKey?: string | null,
  suppliedHeaders: Readonly<Record<string, string>> = {},
): CompatibleRequestEnvelope {
  const url = compatibleRequestUrl(input.baseUrl);
  const headers = compatibleHeaders(apiKey, suppliedHeaders);
  const body = compatibleRequestBody(input);
  return {
    protocol: COMPATIBLE_PROTOCOL_VERSION,
    url,
    headers,
    body,
    requestDigest: hash({ url, headers: compatibleHeaderReceipt(headers), body }),
  };
}

function closeIncompleteJson(raw: string): string | null {
  if (raw.length > 1_048_576 || !raw.trimStart().startsWith("{")) return null;
  const stack: string[] = [];
  let inString = false;
  let escaped = false;
  for (const character of raw) {
    if (inString) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === '"') inString = false;
      continue;
    }
    if (character === '"') inString = true;
    else if (character === "{" || character === "[") stack.push(character);
    else if (character === "}" || character === "]") {
      const expected = character === "}" ? "{" : "[";
      if (stack.pop() !== expected) return null;
    }
  }
  // Never invent bytes inside a string. A truncated path, command, or file
  // body is semantically ambiguous even if adding a quote would make it valid
  // JSON, so only missing structural delimiters may be repaired.
  if (escaped || inString) return null;
  let repaired = raw;
  while (stack.length > 0) repaired += stack.pop() === "{" ? "}" : "]";
  return repaired === raw ? null : repaired;
}

function parseArguments(rawArguments: string): { input: JsonObject; error: string | null; repaired: boolean } {
  if (!rawArguments.trim()) {
    return { input: {}, error: null, repaired: false };
  }
  const candidates = [rawArguments, closeIncompleteJson(rawArguments)].filter(
    (value): value is string => value !== null,
  );
  let lastError = "tool arguments are invalid JSON";
  for (const [index, candidate] of candidates.entries()) {
    try {
      const parsed = JSON.parse(candidate) as unknown;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { input: {}, error: "tool arguments must decode to an object", repaired: false };
      }
      return { input: parsed as JsonObject, error: null, repaired: index > 0 };
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
  }
  return { input: {}, error: lastError, repaired: false };
}

function normalizeFinishReason(value: unknown): string | null {
  const finish = stringValue(value).trim();
  if (!finish) {
    return null;
  }
  if (finish === "tool_calls" || finish === "function_call") {
    return "tool_use";
  }
  if (finish === "length") {
    return "max_tokens";
  }
  if (finish === "stop") {
    return "end_turn";
  }
  return finish;
}

function usageFrom(value: unknown): CompatibleUsage {
  const usage = objectValue(value);
  const inputTokens = Math.max(0, Math.trunc(numberValue(usage.prompt_tokens, numberValue(usage.input_tokens))));
  const outputTokens = Math.max(0, Math.trunc(numberValue(usage.completion_tokens, numberValue(usage.output_tokens))));
  const totalTokens = Math.max(inputTokens + outputTokens, Math.trunc(numberValue(usage.total_tokens)));
  return { inputTokens, outputTokens, totalTokens };
}

function initialState(): MutableResponseState {
  return {
    id: "",
    model: "",
    text: "",
    finishReason: null,
    usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
    toolCalls: new Map(),
    eventCount: 0,
  };
}

function mergeUsage(current: CompatibleUsage, incoming: CompatibleUsage): CompatibleUsage {
  return {
    inputTokens: Math.max(current.inputTokens, incoming.inputTokens),
    outputTokens: Math.max(current.outputTokens, incoming.outputTokens),
    totalTokens: Math.max(current.totalTokens, incoming.totalTokens),
  };
}

function applyCompatibleChoice(state: MutableResponseState, choiceValue: unknown): void {
  const choice = objectValue(choiceValue);
  const message = objectValue(choice.message);
  const delta = objectValue(choice.delta);
  const content = typeof message.content === "string" ? message.content : stringValue(delta.content);
  if (content) {
    state.text += content;
  }
  state.finishReason = normalizeFinishReason(choice.finish_reason) ?? state.finishReason;
  const toolCalls = arrayValue(message.tool_calls).length > 0 ? arrayValue(message.tool_calls) : arrayValue(delta.tool_calls);
  for (const callValue of toolCalls) {
    const call = objectValue(callValue);
    const index = Math.max(0, Math.trunc(numberValue(call.index, state.toolCalls.size)));
    const functionValue = objectValue(call.function);
    const existing = state.toolCalls.get(index) ?? {
      id: "",
      name: "",
      arguments: "",
      index,
    };
    if (typeof call.id === "string" && call.id) {
      existing.id = call.id;
    }
    if (typeof functionValue.name === "string" && functionValue.name) {
      existing.name += functionValue.name;
    }
    if (typeof functionValue.arguments === "string") {
      existing.arguments += functionValue.arguments;
    }
    state.toolCalls.set(index, existing);
  }
}

function applyAnthropicEvent(state: MutableResponseState, event: Record<string, unknown>): boolean {
  const type = stringValue(event.type);
  if (!type.startsWith("message_") && !type.startsWith("content_block_")) {
    return false;
  }
  if (type === "message_start") {
    const message = objectValue(event.message);
    state.id = stringValue(message.id, state.id);
    state.model = stringValue(message.model, state.model);
    state.usage = mergeUsage(state.usage, usageFrom(message.usage));
    return true;
  }
  if (type === "message_delta") {
    const delta = objectValue(event.delta);
    state.finishReason = stringValue(delta.stop_reason) || state.finishReason;
    state.usage = mergeUsage(state.usage, usageFrom(event.usage));
    return true;
  }
  if (type === "content_block_start") {
    const index = Math.max(0, Math.trunc(numberValue(event.index, state.toolCalls.size)));
    const block = objectValue(event.content_block);
    if (block.type === "text") {
      state.text += stringValue(block.text);
    }
    if (block.type === "tool_use") {
      state.toolCalls.set(index, {
        id: stringValue(block.id),
        name: stringValue(block.name),
        arguments: stableStringify(objectValue(block.input)),
        index,
      });
    }
    return true;
  }
  if (type === "content_block_delta") {
    const index = Math.max(0, Math.trunc(numberValue(event.index)));
    const delta = objectValue(event.delta);
    if (delta.type === "text_delta") {
      state.text += stringValue(delta.text);
    }
    if (delta.type === "input_json_delta") {
      const existing = state.toolCalls.get(index) ?? { id: "", name: "", arguments: "", index };
      existing.arguments += stringValue(delta.partial_json);
      state.toolCalls.set(index, existing);
    }
    return true;
  }
  return true;
}

function applyResponseObject(state: MutableResponseState, payload: unknown): void {
  const response = objectValue(payload);
  state.eventCount += 1;
  if (applyAnthropicEvent(state, response)) {
    return;
  }
  state.id = stringValue(response.id, state.id);
  state.model = stringValue(response.model, state.model);
  state.usage = mergeUsage(state.usage, usageFrom(response.usage));
  const choices = arrayValue(response.choices);
  for (const choice of choices) {
    applyCompatibleChoice(state, choice);
  }
  if (choices.length === 0 && response.error) {
    const error = objectValue(response.error);
    throw new CompatibleProtocolError(
      stringValue(error.code, "compatible_provider_error"),
      stringValue(error.message, "compatible provider returned an error"),
      { type: stringValue(error.type), param: stringValue(error.param) },
    );
  }
}

function finalizeResponse(state: MutableResponseState): CompatibleResponse {
  const toolCalls: CompatibleToolCall[] = [...state.toolCalls.values()]
    .sort((left, right) => left.index - right.index)
    .map((call, index) => {
      const parsed = parseArguments(call.arguments);
      const originalName = call.name || "unknown_tool";
      return {
        id: call.id || `call_${index}_${hash(call).slice(0, 12)}`,
        name: parsed.error ? "__zyra_invalid_tool_arguments__" : originalName,
        input: parsed.error
          ? {
              original_tool_name: originalName,
              parse_error: parsed.error.slice(0, 500),
              raw_arguments_digest: hash(call.arguments),
              side_effect_executed: false,
            }
          : parsed.input,
        rawArguments: call.arguments,
        parseError: parsed.error,
        repaired: parsed.repaired,
        originalName,
      };
    });
  const content: JsonObject[] = [];
  if (state.text) {
    content.push({ type: "text", text: state.text });
  }
  for (const call of toolCalls) {
    content.push({ type: "tool_use", id: call.id, name: call.name, input: call.input });
  }
  const finishReason = state.finishReason ?? (toolCalls.length > 0 ? "tool_use" : "end_turn");
  const normalized: JsonObject = {
    id: state.id || `msg_${hash({ text: state.text, toolCalls }).slice(0, 20)}`,
    type: "message",
    role: "assistant",
    model: state.model,
    content,
    stop_reason: finishReason,
    usage: {
      input_tokens: state.usage.inputTokens,
      output_tokens: state.usage.outputTokens,
      total_tokens: state.usage.totalTokens,
    },
  };
  return {
    protocol: COMPATIBLE_PROTOCOL_VERSION,
    id: asString(normalized.id) ?? "",
    model: state.model,
    finalText: state.text,
    toolCalls,
    finishReason,
    usage: state.usage,
    normalized,
    responseDigest: hash(normalized),
    eventCount: state.eventCount,
  };
}

export function decodeCompatibleResponse(payload: unknown): CompatibleResponse {
  const record = objectValue(payload);
  if (Array.isArray(record.content) && (record.role === "assistant" || record.type === "message")) {
    return compatibleResponseFromAnthropic(record as JsonObject);
  }
  const state = initialState();
  applyResponseObject(state, payload);
  return finalizeResponse(state);
}

export function parseServerSentEvents(input: string): CompatibleStreamEvent[] {
  const normalized = input.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const frames = normalized.split("\n\n");
  const events: CompatibleStreamEvent[] = [];
  for (const frame of frames) {
    if (!frame.trim()) {
      continue;
    }
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (!line || line.startsWith(":")) {
        continue;
      }
      const colon = line.indexOf(":");
      const field = colon >= 0 ? line.slice(0, colon) : line;
      const raw = colon >= 0 ? line.slice(colon + 1) : "";
      const value = raw.startsWith(" ") ? raw.slice(1) : raw;
      if (field === "event") {
        event = value || "message";
      }
      if (field === "data") {
        dataLines.push(value);
      }
    }
    if (dataLines.length === 0) {
      continue;
    }
    const rawData = dataLines.join("\n");
    if (rawData === "[DONE]") {
      events.push({ event: "done", data: "[DONE]", sequence: events.length });
      continue;
    }
    let data: JsonValue;
    try {
      data = JSON.parse(rawData) as JsonValue;
    } catch (error) {
      throw new CompatibleProtocolError("compatible_invalid_sse_json", "invalid JSON in provider SSE frame", {
        event,
        sequence: events.length,
        preview: rawData.slice(0, 256),
        cause: error instanceof Error ? error.message : String(error),
      });
    }
    events.push({ event, data, sequence: events.length });
  }
  return events;
}

export async function consumeCompatibleStream(
  source: AsyncIterable<Uint8Array | string>,
): Promise<CompatibleResponse> {
  const decoder = new TextDecoder();
  const state = initialState();
  let buffer = "";
  let sequence = 0;
  for await (const chunk of source) {
    buffer += typeof chunk === "string" ? chunk : decoder.decode(chunk, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const events = parseServerSentEvents(`${frame}\n\n`);
      for (const event of events) {
        if (event.event === "done" || event.data === "[DONE]") {
          continue;
        }
        try {
          applyResponseObject(state, event.data);
        } catch (error) {
          if (error instanceof CompatibleProtocolError) {
            throw error;
          }
          throw new CompatibleProtocolError("compatible_stream_event_failed", "failed to apply provider stream event", {
            sequence,
            cause: error instanceof Error ? error.message : String(error),
          });
        }
        sequence += 1;
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) {
    const events = parseServerSentEvents(`${buffer}\n\n`);
    for (const event of events) {
      if (event.event !== "done" && event.data !== "[DONE]") {
        applyResponseObject(state, event.data);
      }
    }
  }
  if (state.eventCount === 0) {
    throw new CompatibleProtocolError("compatible_empty_stream", "provider stream closed without a response event");
  }
  return finalizeResponse(state);
}

export async function decodeCompatibleBody(
  contentType: string | null,
  source: AsyncIterable<Uint8Array | string>,
): Promise<CompatibleResponse> {
  if ((contentType ?? "").toLowerCase().includes("text/event-stream")) {
    return consumeCompatibleStream(source);
  }
  const decoder = new TextDecoder();
  let raw = "";
  for await (const chunk of source) {
    raw += typeof chunk === "string" ? chunk : decoder.decode(chunk, { stream: true });
  }
  raw += decoder.decode();
  if (!raw.trim()) {
    throw new CompatibleProtocolError("compatible_empty_response", "provider returned an empty response body");
  }
  try {
    return decodeCompatibleResponse(JSON.parse(raw));
  } catch (error) {
    if (error instanceof CompatibleProtocolError) {
      throw error;
    }
    throw new CompatibleProtocolError("compatible_invalid_json", "provider returned invalid JSON", {
      preview: raw.slice(0, 256),
      cause: error instanceof Error ? error.message : String(error),
    });
  }
}

export function compatibleResponseFromAnthropic(payload: JsonObject): CompatibleResponse {
  const response = asObject(payload) ?? {};
  const state = initialState();
  state.id = asString(response.id) ?? "";
  state.model = asString(response.model) ?? "";
  state.finishReason = asString(response.stop_reason) ?? null;
  state.usage = usageFrom(response.usage);
  for (const [index, value] of arrayValue(response.content).entries()) {
    const block = objectValue(value);
    if (block.type === "text") {
      state.text += stringValue(block.text);
    }
    if (block.type === "tool_use") {
      state.toolCalls.set(index, {
        id: stringValue(block.id),
        name: stringValue(block.name),
        arguments: stableStringify(objectValue(block.input)),
        index,
      });
    }
  }
  state.eventCount = 1;
  return finalizeResponse(state);
}

export function assertCompatibleResponse(response: CompatibleResponse): void {
  if (response.protocol !== COMPATIBLE_PROTOCOL_VERSION) {
    throw new CompatibleProtocolError("compatible_version_mismatch", "unexpected compatible response version");
  }
  const ids = new Set<string>();
  for (const call of response.toolCalls) {
    if (!call.id || !call.name) {
      throw new CompatibleProtocolError("compatible_invalid_tool_call", "provider tool calls require id and name");
    }
    if (ids.has(call.id)) {
      throw new CompatibleProtocolError("compatible_duplicate_tool_call", `duplicate provider tool call id: ${call.id}`);
    }
    ids.add(call.id);
    if (call.parseError && call.name !== "__zyra_invalid_tool_arguments__") {
      throw new CompatibleProtocolError(
        "compatible_invalid_tool_arguments_unpaired",
        "invalid tool arguments were not converted to a safe paired error call",
        { call_id: call.id, tool_name: call.name },
      );
    }
  }
  const normalizedDigest = hash(response.normalized);
  if (response.responseDigest !== normalizedDigest) {
    throw new CompatibleProtocolError("compatible_response_digest_mismatch", "compatible response digest mismatch");
  }
}
