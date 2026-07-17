import type {
  JsonObject,
  JsonRpcMessage,
  JsonRpcNotification,
  JsonRpcRequest,
  JsonRpcResponse,
  JsonValue,
} from "../contracts.ts";
import {
  boundedJson,
  canonicalJson,
  canonicalObject,
  cloneJson,
  identifier,
  nonNegativeInteger,
  optionalText,
  requiredText,
  sha256,
  uriText,
} from "./canonical.ts";
import { McpRuntimeError } from "./failure.ts";

export const MCP_PROTOCOL_VERSION = "2025-06-18";
export const MCP_JSON_RPC_VERSION = "2.0" as const;
export const MCP_MAX_MESSAGE_BYTES = 16 * 1024 * 1024;
export const MCP_MAX_CONTENT_ITEMS = 8_192;
export const MCP_MAX_PAGE_ITEMS = 10_000;

export type McpRequestId = string | number;
export type McpRole = "user" | "assistant";
export type McpLoggingLevel =
  | "debug"
  | "info"
  | "notice"
  | "warning"
  | "error"
  | "critical"
  | "alert"
  | "emergency";

export interface McpImplementation {
  name: string;
  title: string | null;
  version: string;
  websiteUrl: string | null;
  icons: McpIcon[];
}

export interface McpIcon {
  src: string;
  mimeType: string | null;
  sizes: string[];
}

export interface McpAnnotations {
  audience: McpRole[];
  priority: number | null;
  lastModified: string | null;
}

export interface McpTextContent {
  type: "text";
  text: string;
  annotations: McpAnnotations | null;
  meta: JsonObject;
}

export interface McpImageContent {
  type: "image";
  data: string;
  mimeType: string;
  annotations: McpAnnotations | null;
  meta: JsonObject;
}

export interface McpAudioContent {
  type: "audio";
  data: string;
  mimeType: string;
  annotations: McpAnnotations | null;
  meta: JsonObject;
}

export interface McpResourceLinkContent {
  type: "resource_link";
  uri: string;
  name: string;
  title: string | null;
  description: string | null;
  mimeType: string | null;
  size: number | null;
  annotations: McpAnnotations | null;
  meta: JsonObject;
}

export interface McpEmbeddedTextResource {
  uri: string;
  mimeType: string | null;
  text: string;
  blob: null;
  meta: JsonObject;
}

export interface McpEmbeddedBlobResource {
  uri: string;
  mimeType: string | null;
  text: null;
  blob: string;
  meta: JsonObject;
}

export interface McpEmbeddedResourceContent {
  type: "resource";
  resource: McpEmbeddedTextResource | McpEmbeddedBlobResource;
  annotations: McpAnnotations | null;
  meta: JsonObject;
}

export type McpContent =
  | McpTextContent
  | McpImageContent
  | McpAudioContent
  | McpResourceLinkContent
  | McpEmbeddedResourceContent;

export interface McpToolAnnotations {
  title: string | null;
  readOnlyHint: boolean | null;
  destructiveHint: boolean | null;
  idempotentHint: boolean | null;
  openWorldHint: boolean | null;
}

export interface McpTool {
  name: string;
  title: string | null;
  description: string;
  inputSchema: JsonObject;
  outputSchema: JsonObject | null;
  annotations: McpToolAnnotations | null;
  icons: McpIcon[];
  meta: JsonObject;
}

export interface McpToolResult {
  content: McpContent[];
  structuredContent: JsonObject | null;
  isError: boolean;
  meta: JsonObject;
}

export interface McpResource {
  uri: string;
  name: string;
  title: string | null;
  description: string | null;
  mimeType: string | null;
  size: number | null;
  annotations: McpAnnotations | null;
  icons: McpIcon[];
  meta: JsonObject;
}

export interface McpResourceTemplate {
  uriTemplate: string;
  name: string;
  title: string | null;
  description: string | null;
  mimeType: string | null;
  annotations: McpAnnotations | null;
  icons: McpIcon[];
  meta: JsonObject;
}

export interface McpPromptArgument {
  name: string;
  title: string | null;
  description: string | null;
  required: boolean;
}

export interface McpPrompt {
  name: string;
  title: string | null;
  description: string | null;
  arguments: McpPromptArgument[];
  icons: McpIcon[];
  meta: JsonObject;
}

export interface McpPromptMessage {
  role: McpRole;
  content: McpContent;
}

export interface McpPromptResult {
  description: string | null;
  messages: McpPromptMessage[];
  meta: JsonObject;
}

export interface McpRoot {
  uri: string;
  name: string | null;
  meta: JsonObject;
}

export interface McpModelHint {
  name: string | null;
}

export interface McpModelPreferences {
  hints: McpModelHint[];
  costPriority: number | null;
  speedPriority: number | null;
  intelligencePriority: number | null;
}

export interface McpSamplingMessage {
  role: McpRole;
  content: McpContent;
}

export interface McpCreateMessageRequest {
  messages: McpSamplingMessage[];
  modelPreferences: McpModelPreferences | null;
  systemPrompt: string | null;
  includeContext: "none" | "thisServer" | "allServers";
  temperature: number | null;
  maxTokens: number;
  stopSequences: string[];
  metadata: JsonObject;
}

export interface McpCreateMessageResult {
  role: McpRole;
  content: McpContent;
  model: string;
  stopReason: string | null;
  meta: JsonObject;
}

export interface McpElicitationPrimitiveSchema {
  type: "string" | "number" | "integer" | "boolean";
  title: string | null;
  description: string | null;
  default: JsonValue | null;
  enum: JsonValue[];
  enumNames: string[];
  minimum: number | null;
  maximum: number | null;
  minLength: number | null;
  maxLength: number | null;
  format: string | null;
}

export interface McpElicitationSchema {
  type: "object";
  properties: Record<string, McpElicitationPrimitiveSchema>;
  required: string[];
}

export interface McpElicitationCreateRequest {
  message: string;
  requestedSchema: McpElicitationSchema;
  mode: "form";
  meta: JsonObject;
}

export interface McpElicitationUrlRequest {
  message: string;
  url: string;
  elicitationId: string;
  mode: "url";
  meta: JsonObject;
}

export type McpElicitationRequest = McpElicitationCreateRequest | McpElicitationUrlRequest;

export interface McpElicitationResult {
  action: "accept" | "decline" | "cancel";
  content: JsonObject | null;
  meta: JsonObject;
}

export interface McpTaskMetadata {
  taskId: string;
  status: "working" | "input_required" | "completed" | "failed" | "cancelled";
  statusMessage: string | null;
  createdAt: string | null;
  lastUpdatedAt: string | null;
  ttl: number | null;
  pollInterval: number | null;
}

export interface McpTaskResult {
  task: McpTaskMetadata;
  result: JsonValue | null;
  meta: JsonObject;
}

export interface McpClientCapabilities {
  experimental: JsonObject;
  roots: { listChanged: boolean } | null;
  sampling: { context: boolean; tools: boolean } | null;
  elicitation: { form: boolean; url: boolean } | null;
  tasks: { list: boolean; cancel: boolean; requests: JsonObject } | null;
}

export interface McpServerCapabilities {
  experimental: JsonObject;
  logging: JsonObject | null;
  completions: JsonObject | null;
  prompts: { listChanged: boolean } | null;
  resources: { subscribe: boolean; listChanged: boolean } | null;
  tools: { listChanged: boolean } | null;
  tasks: { list: boolean; cancel: boolean; requests: JsonObject } | null;
}

export interface McpInitializeRequest {
  protocolVersion: string;
  capabilities: McpClientCapabilities;
  clientInfo: McpImplementation;
}

export interface McpInitializeResult {
  protocolVersion: string;
  capabilities: McpServerCapabilities;
  serverInfo: McpImplementation;
  instructions: string | null;
  meta: JsonObject;
}

export interface McpCursorPage<T> {
  items: T[];
  nextCursor: string | null;
  meta: JsonObject;
}

export interface McpProgressNotification {
  progressToken: string | number;
  progress: number;
  total: number | null;
  message: string | null;
}

export interface McpCancelledNotification {
  requestId: McpRequestId;
  reason: string | null;
}

export interface McpResourceUpdatedNotification {
  uri: string;
}

export interface McpLoggingMessageNotification {
  level: McpLoggingLevel;
  logger: string | null;
  data: JsonValue;
}

export interface McpProtocolLimits {
  maximumMessageBytes: number;
  maximumContentItems: number;
  maximumPageItems: number;
  maximumTextBytes: number;
  maximumBinaryBytes: number;
  maximumSchemaBytes: number;
  maximumMetadataBytes: number;
}

export const defaultProtocolLimits: McpProtocolLimits = {
  maximumMessageBytes: MCP_MAX_MESSAGE_BYTES,
  maximumContentItems: MCP_MAX_CONTENT_ITEMS,
  maximumPageItems: MCP_MAX_PAGE_ITEMS,
  maximumTextBytes: 8 * 1024 * 1024,
  maximumBinaryBytes: 12 * 1024 * 1024,
  maximumSchemaBytes: 2 * 1024 * 1024,
  maximumMetadataBytes: 512 * 1024,
};

export class McpProtocolCodec {
  readonly limits: McpProtocolLimits;

  constructor(limits: Partial<McpProtocolLimits> = {}) {
    this.limits = {
      ...defaultProtocolLimits,
      ...limits,
    };
    validateLimits(this.limits);
  }

  decode(value: unknown): JsonRpcMessage {
    const message = canonicalObject(
      boundedJson(value, this.limits.maximumMessageBytes, "MCP message"),
      "MCP message",
    );
    if (message.jsonrpc !== MCP_JSON_RPC_VERSION) this.fail("invalid_jsonrpc_version", "jsonrpc must be 2.0");
    const hasMethod = typeof message.method === "string";
    const hasId = typeof message.id === "string" || typeof message.id === "number" || message.id === null;
    const hasResponse = Object.hasOwn(message, "result") || Object.hasOwn(message, "error");
    if (hasMethod && hasResponse) this.fail("ambiguous_message", "request cannot contain result or error");
    if (hasResponse) return this.parseResponse(message);
    if (hasMethod && hasId && message.id !== null) return this.parseRequest(message);
    if (hasMethod) return this.parseNotification(message);
    this.fail("invalid_message_shape", "message is not a request, response, or notification");
  }

  decodeText(text: string): JsonRpcMessage {
    if (Buffer.byteLength(text, "utf8") > this.limits.maximumMessageBytes) {
      this.fail("message_too_large", `message exceeds ${this.limits.maximumMessageBytes} bytes`);
    }
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch (error) {
      this.fail("parse_error", error instanceof Error ? error.message : String(error));
    }
    return this.decode(value);
  }

  encode(message: JsonRpcMessage): string {
    const normalized = this.decode(message);
    const text = JSON.stringify(canonicalJson(normalized));
    if (Buffer.byteLength(text, "utf8") > this.limits.maximumMessageBytes) {
      this.fail("message_too_large", `encoded message exceeds ${this.limits.maximumMessageBytes} bytes`);
    }
    return text;
  }

  request(id: McpRequestId, method: string, params: JsonObject = {}): JsonRpcRequest {
    return this.parseRequest({
      jsonrpc: MCP_JSON_RPC_VERSION,
      id,
      method,
      params,
    });
  }

  notification(method: string, params: JsonObject = {}): JsonRpcNotification {
    return this.parseNotification({
      jsonrpc: MCP_JSON_RPC_VERSION,
      method,
      params,
    });
  }

  success(id: McpRequestId, result: JsonValue): JsonRpcResponse {
    return this.parseResponse({
      jsonrpc: MCP_JSON_RPC_VERSION,
      id,
      result,
    });
  }

  error(id: McpRequestId | null, code: number, message: string, data?: JsonValue): JsonRpcResponse {
    const value: JsonObject = {
      jsonrpc: MCP_JSON_RPC_VERSION,
      id,
      error: {
        code,
        message,
        data: data ?? null,
      },
    };
    return this.parseResponse(value);
  }

  fingerprint(message: JsonRpcMessage): string {
    return sha256(this.decode(message));
  }

  parseInitializeResult(value: unknown): McpInitializeResult {
    const object = canonicalObject(value, "initialize result");
    return {
      protocolVersion: requiredText(object.protocolVersion, "protocolVersion", 64),
      capabilities: parseServerCapabilities(object.capabilities),
      serverInfo: parseImplementation(object.serverInfo, "serverInfo"),
      instructions: optionalText(object.instructions, "instructions", this.limits.maximumTextBytes),
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parseToolsPage(value: unknown): McpCursorPage<McpTool> {
    const object = canonicalObject(value, "tools/list result");
    const values = arrayValue(object.tools, "tools", this.limits.maximumPageItems);
    return {
      items: values.map((tool, index) => parseTool(tool, `tools[${index}]`, this.limits)),
      nextCursor: optionalText(object.nextCursor, "nextCursor", 8_192),
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parseResourcesPage(value: unknown): McpCursorPage<McpResource> {
    const object = canonicalObject(value, "resources/list result");
    const values = arrayValue(object.resources, "resources", this.limits.maximumPageItems);
    return {
      items: values.map((resource, index) => parseResource(resource, `resources[${index}]`, this.limits)),
      nextCursor: optionalText(object.nextCursor, "nextCursor", 8_192),
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parseResourceTemplatesPage(value: unknown): McpCursorPage<McpResourceTemplate> {
    const object = canonicalObject(value, "resources/templates/list result");
    const values = arrayValue(object.resourceTemplates, "resourceTemplates", this.limits.maximumPageItems);
    return {
      items: values.map((resource, index) => parseResourceTemplate(resource, `resourceTemplates[${index}]`, this.limits)),
      nextCursor: optionalText(object.nextCursor, "nextCursor", 8_192),
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parsePromptsPage(value: unknown): McpCursorPage<McpPrompt> {
    const object = canonicalObject(value, "prompts/list result");
    const values = arrayValue(object.prompts, "prompts", this.limits.maximumPageItems);
    return {
      items: values.map((prompt, index) => parsePrompt(prompt, `prompts[${index}]`, this.limits)),
      nextCursor: optionalText(object.nextCursor, "nextCursor", 8_192),
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parseToolResult(value: unknown): McpToolResult {
    const object = canonicalObject(value, "tools/call result");
    const contentValues = arrayValue(object.content ?? [], "content", this.limits.maximumContentItems);
    return {
      content: contentValues.map((content, index) => parseContent(content, `content[${index}]`, this.limits)),
      structuredContent: object.structuredContent === undefined
        ? null
        : canonicalObject(
          boundedJson(object.structuredContent, this.limits.maximumMessageBytes, "structuredContent"),
          "structuredContent",
        ),
      isError: object.isError === true,
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parsePromptResult(value: unknown): McpPromptResult {
    const object = canonicalObject(value, "prompts/get result");
    const messages = arrayValue(object.messages, "messages", this.limits.maximumContentItems);
    return {
      description: optionalText(object.description, "description", this.limits.maximumTextBytes),
      messages: messages.map((message, index) => {
        const record = canonicalObject(message, `messages[${index}]`);
        const role = record.role;
        if (role !== "user" && role !== "assistant") this.fail("invalid_role", `messages[${index}].role is invalid`);
        return {
          role,
          content: parseContent(record.content, `messages[${index}].content`, this.limits),
        };
      }),
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parseResourceContents(value: unknown): (McpEmbeddedTextResource | McpEmbeddedBlobResource)[] {
    const object = canonicalObject(value, "resources/read result");
    const contents = arrayValue(object.contents, "contents", this.limits.maximumContentItems);
    return contents.map((content, index) => parseEmbeddedResource(content, `contents[${index}]`, this.limits));
  }

  parseSamplingRequest(value: unknown): McpCreateMessageRequest {
    return parseSamplingRequest(value, this.limits);
  }

  parseSamplingResult(value: unknown): McpCreateMessageResult {
    const object = canonicalObject(value, "sampling result");
    const role = object.role;
    if (role !== "assistant" && role !== "user") this.fail("invalid_role", "sampling result role is invalid");
    return {
      role,
      content: parseContent(object.content, "content", this.limits),
      model: requiredText(object.model, "model", 1_024),
      stopReason: optionalText(object.stopReason, "stopReason", 1_024),
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parseElicitationRequest(value: unknown): McpElicitationRequest {
    return parseElicitationRequest(value, this.limits);
  }

  parseElicitationResult(value: unknown): McpElicitationResult {
    const object = canonicalObject(value, "elicitation result");
    const action = object.action;
    if (action !== "accept" && action !== "decline" && action !== "cancel") {
      this.fail("invalid_elicitation_action", "elicitation action is invalid");
    }
    return {
      action,
      content: object.content === undefined || object.content === null
        ? null
        : canonicalObject(object.content, "content"),
      meta: parseMeta(object._meta, this.limits),
    };
  }

  parseTask(value: unknown): McpTaskMetadata {
    return parseTask(value);
  }

  private parseRequest(value: JsonObject): JsonRpcRequest {
    const id = value.id;
    if (typeof id !== "string" && typeof id !== "number") this.fail("invalid_request_id", "request id must be string or number");
    if (typeof id === "number" && !Number.isSafeInteger(id)) this.fail("invalid_request_id", "numeric request id must be safe integer");
    const method = requiredText(value.method, "method", 1_024);
    const params = value.params === undefined ? undefined : canonicalObject(value.params, "params");
    return params === undefined
      ? { jsonrpc: MCP_JSON_RPC_VERSION, id, method }
      : { jsonrpc: MCP_JSON_RPC_VERSION, id, method, params };
  }

  private parseNotification(value: JsonObject): JsonRpcNotification {
    const method = requiredText(value.method, "method", 1_024);
    const params = value.params === undefined ? undefined : canonicalObject(value.params, "params");
    return params === undefined
      ? { jsonrpc: MCP_JSON_RPC_VERSION, method }
      : { jsonrpc: MCP_JSON_RPC_VERSION, method, params };
  }

  private parseResponse(value: JsonObject): JsonRpcResponse {
    const id = value.id;
    if (id !== null && typeof id !== "string" && typeof id !== "number") this.fail("invalid_response_id", "response id is invalid");
    const hasResult = Object.hasOwn(value, "result");
    const hasError = Object.hasOwn(value, "error");
    if (hasResult === hasError) this.fail("invalid_response_shape", "response requires exactly one of result or error");
    if (hasError) {
      const error = canonicalObject(value.error, "error");
      if (!Number.isSafeInteger(error.code)) this.fail("invalid_error_code", "error.code must be an integer");
      return {
        jsonrpc: MCP_JSON_RPC_VERSION,
        id: id as McpRequestId | null,
        error: {
          code: error.code as number,
          message: requiredText(error.message, "error.message", 32_768),
          ...(error.data === undefined ? {} : { data: canonicalJson(error.data) }),
        },
      };
    }
    return {
      jsonrpc: MCP_JSON_RPC_VERSION,
      id: id as McpRequestId | null,
      result: canonicalJson(value.result),
    };
  }

  private fail(code: string, message: string): never {
    throw new McpRuntimeError({
      failureId: `mcp-protocol-${code}`,
      category: "protocol",
      code,
      message,
      retryable: false,
      disposition: "terminal",
    });
  }
}

export function parseImplementation(value: unknown, label: string): McpImplementation {
  const object = canonicalObject(value, label);
  const icons = arrayValue(object.icons ?? [], `${label}.icons`, 128);
  return {
    name: identifier(object.name, `${label}.name`, 256),
    title: optionalText(object.title, `${label}.title`, 1_024),
    version: requiredText(object.version, `${label}.version`, 256),
    websiteUrl: object.websiteUrl === undefined ? null : uriText(object.websiteUrl, `${label}.websiteUrl`),
    icons: icons.map((icon, index) => parseIcon(icon, `${label}.icons[${index}]`)),
  };
}

export function parseIcon(value: unknown, label: string): McpIcon {
  const object = canonicalObject(value, label);
  return {
    src: uriText(object.src, `${label}.src`),
    mimeType: optionalText(object.mimeType, `${label}.mimeType`, 256),
    sizes: arrayValue(object.sizes ?? [], `${label}.sizes`, 128)
      .map((size, index) => requiredText(size, `${label}.sizes[${index}]`, 64)),
  };
}

export function parseAnnotations(value: unknown, label: string): McpAnnotations | null {
  if (value === undefined || value === null) return null;
  const object = canonicalObject(value, label);
  const audience = arrayValue(object.audience ?? [], `${label}.audience`, 2).map((role) => {
    if (role !== "user" && role !== "assistant") throw protocolValueError("invalid_audience", `${label}.audience is invalid`);
    return role;
  });
  const priority = object.priority === undefined || object.priority === null
    ? null
    : numberInRange(object.priority, `${label}.priority`, 0, 1);
  return {
    audience: [...new Set(audience)],
    priority,
    lastModified: optionalText(object.lastModified, `${label}.lastModified`, 64),
  };
}

export function parseContent(value: unknown, label: string, limits: McpProtocolLimits): McpContent {
  const object = canonicalObject(value, label);
  const type = object.type;
  const annotations = parseAnnotations(object.annotations, `${label}.annotations`);
  const meta = parseMeta(object._meta, limits);
  if (type === "text") {
    return {
      type,
      text: requiredText(object.text, `${label}.text`, limits.maximumTextBytes),
      annotations,
      meta,
    };
  }
  if (type === "image" || type === "audio") {
    const data = requiredText(object.data, `${label}.data`, limits.maximumBinaryBytes * 2);
    validateBase64(data, `${label}.data`, limits.maximumBinaryBytes);
    const mimeType = requiredText(object.mimeType, `${label}.mimeType`, 256);
    return type === "image"
      ? { type, data, mimeType, annotations, meta }
      : { type, data, mimeType, annotations, meta };
  }
  if (type === "resource_link") {
    return {
      type,
      uri: uriText(object.uri, `${label}.uri`),
      name: requiredText(object.name, `${label}.name`, 1_024),
      title: optionalText(object.title, `${label}.title`, 1_024),
      description: optionalText(object.description, `${label}.description`, limits.maximumTextBytes),
      mimeType: optionalText(object.mimeType, `${label}.mimeType`, 256),
      size: object.size === undefined || object.size === null
        ? null
        : nonNegativeInteger(object.size, `${label}.size`),
      annotations,
      meta,
    };
  }
  if (type === "resource") {
    return {
      type,
      resource: parseEmbeddedResource(object.resource, `${label}.resource`, limits),
      annotations,
      meta,
    };
  }
  throw protocolValueError("unsupported_content_type", `${label}.type is unsupported`);
}

export function parseEmbeddedResource(
  value: unknown,
  label: string,
  limits: McpProtocolLimits,
): McpEmbeddedTextResource | McpEmbeddedBlobResource {
  const object = canonicalObject(value, label);
  const uri = uriText(object.uri, `${label}.uri`);
  const mimeType = optionalText(object.mimeType, `${label}.mimeType`, 256);
  const meta = parseMeta(object._meta, limits);
  const hasText = typeof object.text === "string";
  const hasBlob = typeof object.blob === "string";
  if (hasText === hasBlob) throw protocolValueError("invalid_resource_contents", `${label} requires exactly one of text or blob`);
  if (hasText) {
    return {
      uri,
      mimeType,
      text: requiredText(object.text, `${label}.text`, limits.maximumTextBytes),
      blob: null,
      meta,
    };
  }
  const blob = requiredText(object.blob, `${label}.blob`, limits.maximumBinaryBytes * 2);
  validateBase64(blob, `${label}.blob`, limits.maximumBinaryBytes);
  return { uri, mimeType, text: null, blob, meta };
}

export function parseTool(value: unknown, label: string, limits: McpProtocolLimits): McpTool {
  const object = canonicalObject(value, label);
  const schema = canonicalObject(
    boundedJson(object.inputSchema, limits.maximumSchemaBytes, `${label}.inputSchema`),
    `${label}.inputSchema`,
  );
  if (schema.type !== undefined && schema.type !== "object") {
    throw protocolValueError("invalid_tool_schema", `${label}.inputSchema.type must be object`);
  }
  return {
    name: identifier(object.name, `${label}.name`, 256),
    title: optionalText(object.title, `${label}.title`, 1_024),
    description: optionalText(object.description, `${label}.description`, limits.maximumTextBytes) ?? "",
    inputSchema: schema,
    outputSchema: object.outputSchema === undefined
      ? null
      : canonicalObject(
        boundedJson(object.outputSchema, limits.maximumSchemaBytes, `${label}.outputSchema`),
        `${label}.outputSchema`,
      ),
    annotations: parseToolAnnotations(object.annotations, `${label}.annotations`),
    icons: arrayValue(object.icons ?? [], `${label}.icons`, 128)
      .map((icon, index) => parseIcon(icon, `${label}.icons[${index}]`)),
    meta: parseMeta(object._meta, limits),
  };
}

export function parseToolAnnotations(value: unknown, label: string): McpToolAnnotations | null {
  if (value === undefined || value === null) return null;
  const object = canonicalObject(value, label);
  return {
    title: optionalText(object.title, `${label}.title`, 1_024),
    readOnlyHint: optionalBoolean(object.readOnlyHint, `${label}.readOnlyHint`),
    destructiveHint: optionalBoolean(object.destructiveHint, `${label}.destructiveHint`),
    idempotentHint: optionalBoolean(object.idempotentHint, `${label}.idempotentHint`),
    openWorldHint: optionalBoolean(object.openWorldHint, `${label}.openWorldHint`),
  };
}

export function parseResource(value: unknown, label: string, limits: McpProtocolLimits): McpResource {
  const object = canonicalObject(value, label);
  return {
    uri: uriText(object.uri, `${label}.uri`),
    name: requiredText(object.name, `${label}.name`, 1_024),
    title: optionalText(object.title, `${label}.title`, 1_024),
    description: optionalText(object.description, `${label}.description`, limits.maximumTextBytes),
    mimeType: optionalText(object.mimeType, `${label}.mimeType`, 256),
    size: object.size === undefined || object.size === null
      ? null
      : nonNegativeInteger(object.size, `${label}.size`),
    annotations: parseAnnotations(object.annotations, `${label}.annotations`),
    icons: arrayValue(object.icons ?? [], `${label}.icons`, 128)
      .map((icon, index) => parseIcon(icon, `${label}.icons[${index}]`)),
    meta: parseMeta(object._meta, limits),
  };
}

export function parseResourceTemplate(value: unknown, label: string, limits: McpProtocolLimits): McpResourceTemplate {
  const object = canonicalObject(value, label);
  const template = requiredText(object.uriTemplate, `${label}.uriTemplate`, 32_768);
  if (!template.includes("{") || !template.includes("}")) {
    throw protocolValueError("invalid_uri_template", `${label}.uriTemplate has no template expression`);
  }
  return {
    uriTemplate: template,
    name: requiredText(object.name, `${label}.name`, 1_024),
    title: optionalText(object.title, `${label}.title`, 1_024),
    description: optionalText(object.description, `${label}.description`, limits.maximumTextBytes),
    mimeType: optionalText(object.mimeType, `${label}.mimeType`, 256),
    annotations: parseAnnotations(object.annotations, `${label}.annotations`),
    icons: arrayValue(object.icons ?? [], `${label}.icons`, 128)
      .map((icon, index) => parseIcon(icon, `${label}.icons[${index}]`)),
    meta: parseMeta(object._meta, limits),
  };
}

export function parsePrompt(value: unknown, label: string, limits: McpProtocolLimits): McpPrompt {
  const object = canonicalObject(value, label);
  const argumentsValue = arrayValue(object.arguments ?? [], `${label}.arguments`, 1_024);
  return {
    name: identifier(object.name, `${label}.name`, 256),
    title: optionalText(object.title, `${label}.title`, 1_024),
    description: optionalText(object.description, `${label}.description`, limits.maximumTextBytes),
    arguments: argumentsValue.map((argument, index) => {
      const record = canonicalObject(argument, `${label}.arguments[${index}]`);
      return {
        name: identifier(record.name, `${label}.arguments[${index}].name`, 256),
        title: optionalText(record.title, `${label}.arguments[${index}].title`, 1_024),
        description: optionalText(record.description, `${label}.arguments[${index}].description`, limits.maximumTextBytes),
        required: record.required === true,
      };
    }),
    icons: arrayValue(object.icons ?? [], `${label}.icons`, 128)
      .map((icon, index) => parseIcon(icon, `${label}.icons[${index}]`)),
    meta: parseMeta(object._meta, limits),
  };
}

export function parseServerCapabilities(value: unknown): McpServerCapabilities {
  const object = canonicalObject(value ?? {}, "capabilities");
  return {
    experimental: object.experimental === undefined ? {} : canonicalObject(object.experimental, "capabilities.experimental"),
    logging: optionalObject(object.logging, "capabilities.logging"),
    completions: optionalObject(object.completions, "capabilities.completions"),
    prompts: parseChangedCapability(object.prompts, "capabilities.prompts"),
    resources: parseResourceCapability(object.resources, "capabilities.resources"),
    tools: parseChangedCapability(object.tools, "capabilities.tools"),
    tasks: parseTaskCapability(object.tasks, "capabilities.tasks"),
  };
}

export function parseClientCapabilities(value: unknown): McpClientCapabilities {
  const object = canonicalObject(value ?? {}, "capabilities");
  const roots = optionalObject(object.roots, "capabilities.roots");
  const sampling = optionalObject(object.sampling, "capabilities.sampling");
  const elicitation = optionalObject(object.elicitation, "capabilities.elicitation");
  return {
    experimental: object.experimental === undefined ? {} : canonicalObject(object.experimental, "capabilities.experimental"),
    roots: roots === null ? null : { listChanged: roots.listChanged === true },
    sampling: sampling === null
      ? null
      : { context: sampling.context === true, tools: sampling.tools === true },
    elicitation: elicitation === null
      ? null
      : { form: elicitation.form !== false, url: elicitation.url === true },
    tasks: parseTaskCapability(object.tasks, "capabilities.tasks"),
  };
}

export function parseSamplingRequest(value: unknown, limits: McpProtocolLimits): McpCreateMessageRequest {
  const object = canonicalObject(value, "sampling/createMessage params");
  const messages = arrayValue(object.messages, "messages", limits.maximumContentItems);
  const includeContext = object.includeContext ?? "none";
  if (includeContext !== "none" && includeContext !== "thisServer" && includeContext !== "allServers") {
    throw protocolValueError("invalid_include_context", "includeContext is invalid");
  }
  return {
    messages: messages.map((message, index) => {
      const record = canonicalObject(message, `messages[${index}]`);
      const role = record.role;
      if (role !== "user" && role !== "assistant") throw protocolValueError("invalid_role", `messages[${index}].role is invalid`);
      return {
        role,
        content: parseContent(record.content, `messages[${index}].content`, limits),
      };
    }),
    modelPreferences: parseModelPreferences(object.modelPreferences),
    systemPrompt: optionalText(object.systemPrompt, "systemPrompt", limits.maximumTextBytes),
    includeContext,
    temperature: object.temperature === undefined || object.temperature === null
      ? null
      : numberInRange(object.temperature, "temperature", 0, 2),
    maxTokens: nonNegativeInteger(object.maxTokens, "maxTokens"),
    stopSequences: arrayValue(object.stopSequences ?? [], "stopSequences", 1_024)
      .map((sequence, index) => requiredText(sequence, `stopSequences[${index}]`, 4_096)),
    metadata: object.metadata === undefined ? {} : canonicalObject(object.metadata, "metadata"),
  };
}

export function parseModelPreferences(value: unknown): McpModelPreferences | null {
  if (value === undefined || value === null) return null;
  const object = canonicalObject(value, "modelPreferences");
  const hints = arrayValue(object.hints ?? [], "modelPreferences.hints", 128);
  return {
    hints: hints.map((hint, index) => {
      const record = canonicalObject(hint, `modelPreferences.hints[${index}]`);
      return { name: optionalText(record.name, `modelPreferences.hints[${index}].name`, 1_024) };
    }),
    costPriority: optionalPriority(object.costPriority, "modelPreferences.costPriority"),
    speedPriority: optionalPriority(object.speedPriority, "modelPreferences.speedPriority"),
    intelligencePriority: optionalPriority(object.intelligencePriority, "modelPreferences.intelligencePriority"),
  };
}

export function parseElicitationRequest(value: unknown, limits: McpProtocolLimits): McpElicitationRequest {
  const object = canonicalObject(value, "elicitation/create params");
  const mode = object.mode ?? (object.url === undefined ? "form" : "url");
  if (mode === "url") {
    return {
      mode,
      message: requiredText(object.message, "message", limits.maximumTextBytes),
      url: uriText(object.url, "url"),
      elicitationId: identifier(object.elicitationId, "elicitationId", 512),
      meta: parseMeta(object._meta, limits),
    };
  }
  if (mode !== "form") throw protocolValueError("invalid_elicitation_mode", "elicitation mode is invalid");
  return {
    mode,
    message: requiredText(object.message, "message", limits.maximumTextBytes),
    requestedSchema: parseElicitationSchema(object.requestedSchema, limits),
    meta: parseMeta(object._meta, limits),
  };
}

export function parseElicitationSchema(value: unknown, limits: McpProtocolLimits): McpElicitationSchema {
  const object = canonicalObject(
    boundedJson(value, limits.maximumSchemaBytes, "requestedSchema"),
    "requestedSchema",
  );
  if (object.type !== "object") throw protocolValueError("invalid_elicitation_schema", "requestedSchema.type must be object");
  const properties = canonicalObject(object.properties ?? {}, "requestedSchema.properties");
  const output: Record<string, McpElicitationPrimitiveSchema> = {};
  for (const [name, property] of Object.entries(properties)) {
    const record = canonicalObject(property, `requestedSchema.properties.${name}`);
    const type = record.type;
    if (type !== "string" && type !== "number" && type !== "integer" && type !== "boolean") {
      throw protocolValueError("invalid_elicitation_property", `property ${name} type is unsupported`);
    }
    output[name] = {
      type,
      title: optionalText(record.title, `property ${name}.title`, 1_024),
      description: optionalText(record.description, `property ${name}.description`, limits.maximumTextBytes),
      default: record.default === undefined ? null : canonicalJson(record.default),
      enum: arrayValue(record.enum ?? [], `property ${name}.enum`, 1_024).map((entry) => canonicalJson(entry)),
      enumNames: arrayValue(record.enumNames ?? [], `property ${name}.enumNames`, 1_024)
        .map((entry, index) => requiredText(entry, `property ${name}.enumNames[${index}]`, 1_024)),
      minimum: optionalNumber(record.minimum, `property ${name}.minimum`),
      maximum: optionalNumber(record.maximum, `property ${name}.maximum`),
      minLength: optionalInteger(record.minLength, `property ${name}.minLength`),
      maxLength: optionalInteger(record.maxLength, `property ${name}.maxLength`),
      format: optionalText(record.format, `property ${name}.format`, 256),
    };
  }
  const required = arrayValue(object.required ?? [], "requestedSchema.required", 1_024)
    .map((entry, index) => requiredText(entry, `requestedSchema.required[${index}]`, 256));
  for (const name of required) {
    if (!Object.hasOwn(output, name)) throw protocolValueError("invalid_elicitation_required", `required property ${name} is missing`);
  }
  return { type: "object", properties: output, required: [...new Set(required)] };
}

export function parseTask(value: unknown): McpTaskMetadata {
  const object = canonicalObject(value, "task");
  const status = object.status;
  if (status !== "working" && status !== "input_required" && status !== "completed" && status !== "failed" && status !== "cancelled") {
    throw protocolValueError("invalid_task_status", "task.status is invalid");
  }
  return {
    taskId: identifier(object.taskId, "task.taskId", 512),
    status,
    statusMessage: optionalText(object.statusMessage, "task.statusMessage", 32_768),
    createdAt: optionalText(object.createdAt, "task.createdAt", 64),
    lastUpdatedAt: optionalText(object.lastUpdatedAt, "task.lastUpdatedAt", 64),
    ttl: optionalInteger(object.ttl, "task.ttl"),
    pollInterval: optionalInteger(object.pollInterval, "task.pollInterval"),
  };
}

function parseMeta(value: unknown, limits: McpProtocolLimits): JsonObject {
  if (value === undefined || value === null) return {};
  return canonicalObject(boundedJson(value, limits.maximumMetadataBytes, "_meta"), "_meta");
}

function parseChangedCapability(value: unknown, label: string): { listChanged: boolean } | null {
  const object = optionalObject(value, label);
  return object === null ? null : { listChanged: object.listChanged === true };
}

function parseResourceCapability(value: unknown, label: string): { subscribe: boolean; listChanged: boolean } | null {
  const object = optionalObject(value, label);
  return object === null
    ? null
    : { subscribe: object.subscribe === true, listChanged: object.listChanged === true };
}

function parseTaskCapability(value: unknown, label: string): { list: boolean; cancel: boolean; requests: JsonObject } | null {
  const object = optionalObject(value, label);
  return object === null
    ? null
    : {
      list: object.list === true,
      cancel: object.cancel === true,
      requests: object.requests === undefined ? {} : canonicalObject(object.requests, `${label}.requests`),
    };
}

function optionalObject(value: unknown, label: string): JsonObject | null {
  if (value === undefined || value === null) return null;
  return canonicalObject(value, label);
}

function optionalBoolean(value: unknown, label: string): boolean | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "boolean") throw protocolValueError("invalid_boolean", `${label} must be boolean`);
  return value;
}

function optionalNumber(value: unknown, label: string): number | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) throw protocolValueError("invalid_number", `${label} must be finite number`);
  return value;
}

function optionalInteger(value: unknown, label: string): number | null {
  if (value === undefined || value === null) return null;
  return nonNegativeInteger(value, label);
}

function optionalPriority(value: unknown, label: string): number | null {
  if (value === undefined || value === null) return null;
  return numberInRange(value, label, 0, 1);
}

function numberInRange(value: unknown, label: string, minimum: number, maximum: number): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw protocolValueError("number_out_of_range", `${label} must be in [${minimum}, ${maximum}]`);
  }
  return value;
}

function arrayValue(value: unknown, label: string, maximum: number): JsonValue[] {
  if (!Array.isArray(value)) throw protocolValueError("invalid_array", `${label} must be an array`);
  if (value.length > maximum) throw protocolValueError("array_too_large", `${label} exceeds ${maximum} entries`);
  return value.map((entry) => canonicalJson(entry));
}

function validateBase64(value: string, label: string, maximumBytes: number): void {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw protocolValueError("invalid_base64", `${label} is invalid base64`);
  }
  const estimated = Math.floor(value.length * 3 / 4);
  if (estimated > maximumBytes) throw protocolValueError("binary_too_large", `${label} exceeds ${maximumBytes} decoded bytes`);
}

function validateLimits(limits: McpProtocolLimits): void {
  for (const [key, value] of Object.entries(limits)) {
    if (!Number.isSafeInteger(value) || value <= 0) throw protocolValueError("invalid_protocol_limit", `${key} must be positive integer`);
  }
  if (limits.maximumTextBytes > limits.maximumMessageBytes) {
    throw protocolValueError("invalid_protocol_limit", "maximumTextBytes cannot exceed maximumMessageBytes");
  }
  if (limits.maximumSchemaBytes > limits.maximumMessageBytes) {
    throw protocolValueError("invalid_protocol_limit", "maximumSchemaBytes cannot exceed maximumMessageBytes");
  }
}

function protocolValueError(code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: `mcp-protocol-${code}`,
    category: "protocol",
    code,
    message,
    retryable: false,
    disposition: "terminal",
  });
}

export function cloneTool(value: McpTool): McpTool {
  return cloneJson(value);
}

export function cloneResource(value: McpResource): McpResource {
  return cloneJson(value);
}

export function clonePrompt(value: McpPrompt): McpPrompt {
  return cloneJson(value);
}
