import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalJson, cloneJson, redactMcpSecrets } from "./canonical.ts";

export type McpFailureCategory =
  | "configuration"
  | "policy"
  | "transport"
  | "protocol"
  | "authentication"
  | "authorization"
  | "capability"
  | "timeout"
  | "cancelled"
  | "conflict"
  | "restore"
  | "remote"
  | "internal";

export type McpFailureDisposition =
  | "terminal"
  | "retry_same_connection"
  | "reconnect_then_retry"
  | "refresh_then_retry"
  | "reauthorize"
  | "reconfigure"
  | "replan";

export interface McpFailureRecord extends JsonObject {
  version: "zyra.mcp-failure/v1";
  failure_id: string;
  category: McpFailureCategory;
  code: string;
  message: string;
  server_id: string;
  operation: string;
  request_id: string;
  session_id: string;
  retryable: boolean;
  disposition: McpFailureDisposition;
  retry_after_ms: number | null;
  status_code: number | null;
  json_rpc_code: number | null;
  details: JsonObject;
  cause_chain: JsonValue[];
  occurred_at: string;
}

export interface McpFailureInput {
  failureId: string;
  category: McpFailureCategory;
  code: string;
  message: string;
  serverId?: string;
  operation?: string;
  requestId?: string;
  sessionId?: string;
  retryable?: boolean;
  disposition?: McpFailureDisposition;
  retryAfterMs?: number | null;
  statusCode?: number | null;
  jsonRpcCode?: number | null;
  details?: JsonObject;
  causeChain?: JsonValue[];
  occurredAt?: string;
}

export class McpRuntimeError extends Error {
  readonly failure: McpFailureRecord;

  constructor(input: McpFailureInput, options?: ErrorOptions) {
    super(input.message, options);
    this.name = "McpRuntimeError";
    this.failure = createFailure(input);
  }

  get code(): string {
    return this.failure.code;
  }

  get serverId(): string {
    return this.failure.server_id;
  }

  get retryable(): boolean {
    return this.failure.retryable;
  }

  toJSON(): McpFailureRecord {
    return cloneJson(this.failure);
  }
}

export function createFailure(input: McpFailureInput): McpFailureRecord {
  const disposition = input.disposition ?? defaultDisposition(input.category, input.retryable ?? false);
  return canonicalJson({
    version: "zyra.mcp-failure/v1",
    failure_id: input.failureId,
    category: input.category,
    code: input.code,
    message: input.message,
    server_id: input.serverId ?? "",
    operation: input.operation ?? "",
    request_id: input.requestId ?? "",
    session_id: input.sessionId ?? "",
    retryable: input.retryable ?? false,
    disposition,
    retry_after_ms: input.retryAfterMs ?? null,
    status_code: input.statusCode ?? null,
    json_rpc_code: input.jsonRpcCode ?? null,
    details: redactMcpSecrets(input.details ?? {}) as JsonObject,
    cause_chain: (input.causeChain ?? []).map((cause) => redactMcpSecrets(cause)),
    occurred_at: input.occurredAt ?? new Date().toISOString(),
  }) as McpFailureRecord;
}

export function normalizeFailure(
  error: unknown,
  context: Partial<McpFailureInput> = {},
): McpFailureRecord {
  if (error instanceof McpRuntimeError) {
    if (!Object.keys(context).length) return error.toJSON();
    return createFailure({
      failureId: context.failureId ?? error.failure.failure_id,
      category: context.category ?? error.failure.category,
      code: context.code ?? error.failure.code,
      message: context.message ?? error.failure.message,
      serverId: context.serverId ?? error.failure.server_id,
      operation: context.operation ?? error.failure.operation,
      requestId: context.requestId ?? error.failure.request_id,
      sessionId: context.sessionId ?? error.failure.session_id,
      retryable: context.retryable ?? error.failure.retryable,
      disposition: context.disposition ?? error.failure.disposition,
      retryAfterMs: context.retryAfterMs ?? error.failure.retry_after_ms,
      statusCode: context.statusCode ?? error.failure.status_code,
      jsonRpcCode: context.jsonRpcCode ?? error.failure.json_rpc_code,
      details: context.details ?? error.failure.details,
      causeChain: context.causeChain ?? error.failure.cause_chain,
      occurredAt: context.occurredAt ?? error.failure.occurred_at,
    });
  }
  if (isAbortError(error)) {
    return createFailure({
      failureId: context.failureId ?? "mcp-cancelled",
      category: "cancelled",
      code: context.code ?? "request_cancelled",
      message: context.message ?? "MCP request was cancelled",
      ...context,
      retryable: false,
      disposition: "terminal",
      causeChain: [safeCause(error)],
    });
  }
  if (isTimeoutError(error)) {
    return createFailure({
      failureId: context.failureId ?? "mcp-timeout",
      category: "timeout",
      code: context.code ?? "request_timeout",
      message: context.message ?? errorMessage(error),
      ...context,
      retryable: context.retryable ?? true,
      disposition: context.disposition ?? "reconnect_then_retry",
      causeChain: [safeCause(error)],
    });
  }
  return createFailure({
    failureId: context.failureId ?? "mcp-internal",
    category: context.category ?? "internal",
    code: context.code ?? "unclassified_error",
    message: context.message ?? errorMessage(error),
    ...context,
    retryable: context.retryable ?? false,
    disposition: context.disposition ?? "terminal",
    causeChain: [...(context.causeChain ?? []), safeCause(error)],
  });
}

export function throwFailure(input: McpFailureInput, cause?: unknown): never {
  throw new McpRuntimeError(input, cause === undefined ? undefined : { cause });
}

export function assertMcp(
  condition: unknown,
  input: McpFailureInput,
): asserts condition {
  if (!condition) throw new McpRuntimeError(input);
}

export function defaultDisposition(
  category: McpFailureCategory,
  retryable: boolean,
): McpFailureDisposition {
  if (category === "authentication") return "reauthorize";
  if (category === "configuration") return "reconfigure";
  if (category === "policy" || category === "authorization") return "replan";
  if (!retryable) return "terminal";
  if (category === "transport" || category === "timeout") return "reconnect_then_retry";
  if (category === "remote") return "retry_same_connection";
  return "terminal";
}

export function isAbortError(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const value = error as { name?: unknown; code?: unknown };
  return value.name === "AbortError" || value.code === "ABORT_ERR";
}

export function isTimeoutError(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const value = error as { name?: unknown; code?: unknown; message?: unknown };
  if (value.name === "TimeoutError") return true;
  if (value.code === "ETIMEDOUT" || value.code === "UND_ERR_CONNECT_TIMEOUT") return true;
  return typeof value.message === "string" && /timed?\s*out|timeout/i.test(value.message);
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message || error.name;
  if (typeof error === "string") return error;
  try {
    return JSON.stringify(redactMcpSecrets(error));
  } catch {
    return String(error);
  }
}

export function safeCause(error: unknown): JsonValue {
  if (error instanceof McpRuntimeError) return error.toJSON();
  if (error instanceof Error) {
    return canonicalJson({
      name: error.name,
      message: error.message,
      code: typeof (error as { code?: unknown }).code === "string"
        ? (error as unknown as { code: string }).code
        : "",
    });
  }
  return redactMcpSecrets(error);
}

export function failureFromHttp(
  response: Response,
  serverId: string,
  operation: string,
  requestId: string,
  details: JsonObject = {},
): McpFailureRecord {
  const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
  const authentication = response.status === 401 || response.status === 403;
  const retryable = response.status === 408
    || response.status === 425
    || response.status === 429
    || response.status >= 500;
  return createFailure({
    failureId: `mcp-http-${response.status}-${requestId}`,
    category: authentication ? "authentication" : "remote",
    code: authentication ? "http_authentication_required" : `http_${response.status}`,
    message: `MCP HTTP request failed with ${response.status} ${response.statusText}`.trim(),
    serverId,
    operation,
    requestId,
    retryable,
    disposition: authentication
      ? "refresh_then_retry"
      : retryable
        ? "retry_same_connection"
        : "terminal",
    retryAfterMs: retryAfter,
    statusCode: response.status,
    details,
  });
}

export function failureFromJsonRpc(
  code: number,
  message: string,
  data: JsonValue | undefined,
  serverId: string,
  operation: string,
  requestId: string,
): McpFailureRecord {
  const retryable = code === -32001 || code === -32002 || code === -32003;
  return createFailure({
    failureId: `mcp-rpc-${Math.abs(code)}-${requestId}`,
    category: "remote",
    code: `json_rpc_${code}`,
    message,
    serverId,
    operation,
    requestId,
    retryable,
    disposition: retryable ? "retry_same_connection" : "terminal",
    jsonRpcCode: code,
    details: { data: data ?? null },
  });
}

export function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  if (/^\d+$/.test(value)) return Math.min(Number(value) * 1_000, 3_600_000);
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return null;
  return Math.max(0, Math.min(timestamp - Date.now(), 3_600_000));
}

export function combineFailures(
  failures: readonly McpFailureRecord[],
  failureId: string,
  message: string,
): McpFailureRecord {
  const retryable = failures.length > 0 && failures.every((failure) => failure.retryable);
  const categories = [...new Set(failures.map((failure) => failure.category))];
  return createFailure({
    failureId,
    category: categories.length === 1 ? categories[0] : "internal",
    code: "aggregate_failure",
    message,
    retryable,
    disposition: retryable ? "reconnect_then_retry" : "terminal",
    details: {
      failure_count: failures.length,
      categories,
    },
    causeChain: [...failures],
  });
}
