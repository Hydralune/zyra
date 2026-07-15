import type { JsonObject, JsonValue, ToolStep } from "../contracts.ts";
import {
  ProviderProtocolError,
  type ProviderKind,
  type ProviderResponse,
  type ProviderTextBlock,
  type ProviderToolDefinition,
} from "../provider/model-runtime.ts";

export interface ProviderExecutionProjection {
  ok: boolean;
  status: number;
  headers: Record<string, string>;
  steps: ToolStep[];
  usage: JsonObject;
  error: string;
  model: string;
  providerRequestId: string | null;
  finalText: string;
  stopReason: string;
}

export function normalizedProviderBaseUrl(
  provider: Extract<ProviderKind, "compatible" | "local">,
  configured: string,
): string {
  const fallback = provider === "local"
    ? "http://127.0.0.1:11434"
    : "http://127.0.0.1:8080";
  const candidate = configured.trim() || fallback;
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error(`invalid provider base URL: ${candidate}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`unsupported provider URL protocol: ${parsed.protocol}`);
  }
  parsed.username = "";
  parsed.password = "";
  parsed.hash = "";
  parsed.search = "";
  parsed.pathname = parsed.pathname.replace(/\/+$/u, "");
  return parsed.toString().replace(/\/$/u, "");
}

export function durableMessageRole(value: string): "system" | "user" | "assistant" | "tool" {
  if (value === "system" || value === "assistant" || value === "tool") return value;
  return "user";
}

export function providerSystemBlocks(value: JsonValue): ProviderTextBlock[] {
  const values = Array.isArray(value) ? value : [value];
  const blocks: ProviderTextBlock[] = [];
  for (const item of values) {
    if (typeof item === "string") {
      if (item.trim()) blocks.push(textBlock(item));
      continue;
    }
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const record = item as JsonObject;
    const text = typeof record.text === "string"
      ? record.text
      : typeof record.content === "string"
        ? record.content
        : "";
    if (text.trim()) blocks.push(textBlock(text));
  }
  return blocks;
}

export function providerToolDefinitions(value: JsonValue): ProviderToolDefinition[] {
  if (!Array.isArray(value)) return [];
  const tools: ProviderToolDefinition[] = [];
  const names = new Set<string>();
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const outer = item as JsonObject;
    const rawFunction = outer.function;
    const definition = rawFunction && typeof rawFunction === "object" && !Array.isArray(rawFunction)
      ? rawFunction as JsonObject
      : outer;
    const name = typeof definition.name === "string" ? definition.name.trim() : "";
    if (!name || names.has(name)) continue;
    names.add(name);
    const schemaValue = definition.parameters ?? definition.input_schema ?? {};
    const inputSchema = schemaValue && typeof schemaValue === "object" && !Array.isArray(schemaValue)
      ? schemaValue as JsonObject
      : {};
    tools.push({
      name,
      description: typeof definition.description === "string"
        ? definition.description
        : `Runtime tool ${name}`,
      inputSchema,
      cacheControl: undefined,
      deferred: false,
      strict: false,
    });
  }
  return tools;
}

export function redactProviderHeaders(
  headers: Readonly<Record<string, string>>,
): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    const key = name.toLowerCase();
    result[key] = /authorization|api[-_]?key|token|secret|cookie/u.test(key)
      ? `[redacted:${fingerprint(value)}]`
      : value;
  }
  return result;
}

export function providerExecutionSuccess(
  response: ProviderResponse,
): ProviderExecutionProjection {
  const steps: ToolStep[] = [];
  const text: string[] = [];
  for (const block of response.content) {
    if (block.type === "text") {
      text.push(block.text);
      continue;
    }
    if (block.type !== "tool_use") continue;
    steps.push({
      step_id: block.id,
      tool_name: block.name,
      arguments: cloneObject(block.input),
      prompt: "",
      metadata: {
        provider_model: response.model,
        provider_request_id: response.providerRequestId,
        canonical_owner: "provider_model_runtime",
      },
    });
  }
  return {
    ok: true,
    status: 200,
    headers: {},
    steps,
    usage: {
      input_tokens: response.usage.inputTokens,
      output_tokens: response.usage.outputTokens,
      cache_read_input_tokens: response.usage.cacheReadInputTokens,
      cache_creation_input_tokens: response.usage.cacheCreationInputTokens,
      server_tool_use_tokens: response.usage.serverToolUseTokens,
    },
    error: "",
    model: response.model,
    providerRequestId: response.providerRequestId,
    finalText: text.join(""),
    stopReason: response.stopReason,
  };
}

export function providerExecutionFailure(
  error: unknown,
  model: string,
): ProviderExecutionProjection {
  const protocol = error instanceof ProviderProtocolError ? error : null;
  const details = protocol?.details ?? {};
  const rawHeaders = details.headers ?? details.response_headers;
  const headers = stringRecord(rawHeaders);
  return {
    ok: false,
    status: protocol?.status ?? 0,
    headers,
    steps: [],
    usage: emptyUsage(),
    error: error instanceof Error ? error.message : String(error),
    model,
    providerRequestId: nullableString(details.provider_request_id),
    finalText: "",
    stopReason: "unknown",
  };
}

function textBlock(text: string): ProviderTextBlock {
  return {
    type: "text",
    text,
    cacheControl: undefined,
  };
}

function cloneObject(value: JsonValue): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return structuredClone(value) as JsonObject;
}

function stringRecord(value: JsonValue | undefined): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const result: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "string") result[key.toLowerCase()] = item;
    else if (typeof item === "number" || typeof item === "boolean") result[key.toLowerCase()] = String(item);
  }
  return result;
}

function nullableString(value: JsonValue | undefined): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function fingerprint(value: string): string {
  let hash = 2_166_136_261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16_777_619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function emptyUsage(): JsonObject {
  return {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
    server_tool_use_tokens: 0,
  };
}
