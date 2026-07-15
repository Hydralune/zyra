import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";
import type {
  CacheControl,
  ProviderContentBlock,
  ProviderMessage,
  ProviderTextBlock,
  ProviderToolDefinition,
} from "./model-runtime.ts";

export const PROVIDER_PROMPT_SNAPSHOT_VERSION = "zyra.provider-prompt/v1";

export type PromptCacheStrategy = "tool_based" | "system_prompt" | "message_boundary" | "none";
export type PromptSectionKind = "identity" | "policy" | "workspace" | "memory" | "skills" | "tools" | "task" | "runtime";

export interface PromptSection {
  sectionId: string;
  kind: PromptSectionKind;
  title: string;
  content: string;
  priority: number;
  stable: boolean;
  cacheTtl: CacheControl["ttl"] | null;
  sourceDigest: string;
  metadata: JsonObject;
}

export interface PromptToolInput {
  name: string;
  description: string;
  inputSchema: JsonObject;
  deferred: boolean;
  strict: boolean;
  priority: number;
  usedRecently: boolean;
  required: boolean;
}

export interface PromptBuildOptions {
  model: string;
  cacheEnabled: boolean;
  cacheStrategy: PromptCacheStrategy;
  cacheTtl: CacheControl["ttl"];
  maximumSystemChars: number;
  maximumToolsChars: number;
  maximumMedia: number;
  enableDeferredTools: boolean;
  preserveLastUserMessage: boolean;
}

export interface BuiltProviderPrompt {
  promptId: string;
  system: ProviderTextBlock[];
  messages: ProviderMessage[];
  tools: ProviderToolDefinition[];
  sections: PromptSection[];
  deferredToolNames: string[];
  strippedMediaCount: number;
  systemChars: number;
  messagesChars: number;
  toolsChars: number;
  cacheBreakpointCount: number;
  fingerprint: string;
  createdAt: string;
}

export interface ProviderPromptSnapshot {
  version: typeof PROVIDER_PROMPT_SNAPSHOT_VERSION;
  revision: number;
  sections: PromptSection[];
  tools: PromptToolInput[];
  lastPrompt: BuiltProviderPrompt | null;
  checksum: string;
}

export class ProviderPromptRuntime {
  private readonly sections = new Map<string, PromptSection>();
  private readonly tools = new Map<string, PromptToolInput>();
  private lastPrompt: BuiltProviderPrompt | null = null;
  private revision = 0;

  prompt_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "section") return sectionToJson(this.upsertSection(sectionFromJson(asObject(value.section))));
    if (action === "tool") return toolInputToJson(this.upsertTool(toolFromJson(asObject(value.tool))));
    if (action === "build") {
      return builtToJson(this.build(messagesFromJson(value.messages), buildOptionsFromJson(asObject(value.options))));
    }
    if (action === "remove_section") return { removed: this.removeSection(asString(value.section_id)) };
    if (action === "remove_tool") return { removed: this.removeTool(asString(value.name)) };
    return {
      section_count: this.sections.size,
      tool_count: this.tools.size,
      last_prompt_fingerprint: this.lastPrompt?.fingerprint ?? null,
      revision: this.revision,
    };
  }

  upsertSection(input: Omit<PromptSection, "sectionId" | "sourceDigest"> & { sectionId?: string; sourceDigest?: string }): PromptSection {
    const sectionId = input.sectionId?.trim() || `${input.kind}:${digest(input.title).slice(-16)}`;
    const content = normalizePromptText(input.content);
    if (!content) throw new Error("prompt section content is empty");
    const section: PromptSection = {
      sectionId,
      kind: sectionKind(input.kind),
      title: input.title.trim().slice(0, 512),
      content,
      priority: boundedInteger(input.priority, -10_000, 10_000),
      stable: input.stable,
      cacheTtl: input.cacheTtl === "1h" ? "1h" : input.cacheTtl === "5m" ? "5m" : null,
      sourceDigest: input.sourceDigest?.trim() || digest(content),
      metadata: sanitizeMetadata(input.metadata),
    };
    this.sections.set(sectionId, section);
    this.revision += 1;
    return structuredClone(section);
  }

  removeSection(sectionId: string): boolean {
    const removed = this.sections.delete(sectionId);
    if (removed) this.revision += 1;
    return removed;
  }

  upsertTool(input: PromptToolInput): PromptToolInput {
    const name = normalizeToolName(input.name);
    const tool: PromptToolInput = {
      name,
      description: input.description.trim().slice(0, 8_000),
      inputSchema: normalizeSchema(input.inputSchema),
      deferred: input.deferred,
      strict: input.strict,
      priority: boundedInteger(input.priority, -10_000, 10_000),
      usedRecently: input.usedRecently,
      required: input.required,
    };
    this.tools.set(name, tool);
    this.revision += 1;
    return structuredClone(tool);
  }

  removeTool(name: string): boolean {
    const removed = this.tools.delete(normalizeToolName(name));
    if (removed) this.revision += 1;
    return removed;
  }

  build(messages: readonly ProviderMessage[], options: PromptBuildOptions): BuiltProviderPrompt {
    const normalizedOptions = normalizeBuildOptions(options);
    const selectedSections = selectSections([...this.sections.values()], normalizedOptions.maximumSystemChars);
    let system = buildSystemBlocks(selectedSections);
    const toolSelection = selectTools([...this.tools.values()], normalizedOptions);
    let tools = toolSelection.selected.map(providerTool);
    const normalizedMessages = normalizeMessages(messages);
    const media = stripExcessMedia(normalizedMessages, normalizedOptions.maximumMedia, normalizedOptions.preserveLastUserMessage);
    if (normalizedOptions.cacheEnabled) {
      if (normalizedOptions.cacheStrategy === "system_prompt") {
        system = addSystemCacheBreakpoints(system, normalizedOptions.cacheTtl);
      } else if (normalizedOptions.cacheStrategy === "tool_based") {
        tools = addToolCacheBreakpoint(tools, normalizedOptions.cacheTtl);
      } else if (normalizedOptions.cacheStrategy === "message_boundary") {
        media.messages = addMessageCacheBreakpoint(media.messages, normalizedOptions.cacheTtl);
      }
    } else {
      system = system.map(stripTextCacheControl);
      tools = tools.map(stripToolCacheControl);
      media.messages = media.messages.map(stripMessageCacheControl);
    }
    const systemChars = jsonChars(system);
    const messagesChars = jsonChars(media.messages);
    const toolsChars = jsonChars(tools);
    if (systemChars > normalizedOptions.maximumSystemChars) throw new Error("built provider system prompt exceeds configured limit");
    if (toolsChars > normalizedOptions.maximumToolsChars) throw new Error("built provider tool catalog exceeds configured limit");
    const prompt: BuiltProviderPrompt = {
      promptId: randomUUID(),
      system,
      messages: media.messages,
      tools,
      sections: selectedSections,
      deferredToolNames: toolSelection.deferred.map((item) => item.name),
      strippedMediaCount: media.stripped,
      systemChars,
      messagesChars,
      toolsChars,
      cacheBreakpointCount: countCacheBreakpoints([system, media.messages, tools] as unknown as JsonValue),
      fingerprint: promptFingerprint(system, media.messages, tools, normalizedOptions.model),
      createdAt: new Date().toISOString(),
    };
    this.lastPrompt = prompt;
    this.revision += 1;
    return structuredClone(prompt);
  }

  getPromptCachingEnabled(model: string, explicitlyDisabled = false): boolean {
    if (explicitlyDisabled) return false;
    const normalized = model.toLowerCase();
    if (normalized.includes("local") || normalized.includes("legacy") || normalized.includes("instant")) return false;
    return true;
  }

  getExtraBodyParams(betaHeaders: readonly string[]): JsonObject {
    const headers = new Set(betaHeaders.map((item) => item.trim()).filter(Boolean));
    const body: JsonObject = {};
    if (headers.has("output-128k-2025-02-19")) body.output_config = { maximum_tokens_beta: 128_000 };
    if (headers.has("context-management-2025-06-27")) {
      body.context_management = {
        edits: [{ type: "clear_tool_uses_20250919", trigger: { type: "input_tokens", value: 120_000 }, keep: { type: "tool_uses", value: 5 } }],
      };
    }
    if (headers.has("structured-outputs-2025-11-13")) body.output_format = { type: "json_schema" };
    return body;
  }

  configureTaskBudgetParams(
    body: JsonObject,
    options: { maximumTokens: number; thinkingTokens: number; effort: string | null },
  ): JsonObject {
    const result = structuredClone(body);
    const maximumTokens = boundedInteger(options.maximumTokens, 1, 128_000);
    result.max_tokens = maximumTokens;
    if (options.thinkingTokens > 0) {
      result.thinking = {
        type: "enabled",
        budget_tokens: boundedInteger(options.thinkingTokens, 1_024, Math.max(1_024, maximumTokens - 1_024)),
      };
      delete result.temperature;
      delete result.top_p;
    }
    if (options.effort) result.output_config = { ...asObject(result.output_config), effort: normalizeEffort(options.effort) };
    return result;
  }

  adjustParamsForNonStreaming(body: JsonObject, maximum = 64_000): JsonObject {
    const result = structuredClone(body);
    result.stream = false;
    result.max_tokens = Math.min(positive(result.max_tokens, maximum), maximum);
    delete result.stream_options;
    return result;
  }

  snapshot(): ProviderPromptSnapshot {
    const unsigned: Omit<ProviderPromptSnapshot, "checksum"> = {
      version: PROVIDER_PROMPT_SNAPSHOT_VERSION,
      revision: this.revision,
      sections: structuredClone([...this.sections.values()]),
      tools: structuredClone([...this.tools.values()]),
      lastPrompt: this.lastPrompt ? structuredClone(this.lastPrompt) : null,
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: ProviderPromptSnapshot): void {
    if (snapshot.version !== PROVIDER_PROMPT_SNAPSHOT_VERSION) throw new Error("unsupported provider prompt snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("provider prompt snapshot checksum mismatch");
    this.sections.clear();
    for (const section of snapshot.sections) this.sections.set(section.sectionId, structuredClone(section));
    this.tools.clear();
    for (const tool of snapshot.tools) this.tools.set(tool.name, structuredClone(tool));
    this.lastPrompt = snapshot.lastPrompt ? structuredClone(snapshot.lastPrompt) : null;
    this.revision = snapshot.revision;
  }
}

function selectSections(values: readonly PromptSection[], maximumChars: number): PromptSection[] {
  const stable = values.filter((item) => item.stable).sort(sectionOrder);
  const dynamic = values.filter((item) => !item.stable).sort(sectionOrder);
  const selected: PromptSection[] = [];
  let chars = 0;
  for (const section of [...stable, ...dynamic]) {
    const formatted = formatSection(section);
    if (chars + formatted.length > maximumChars) continue;
    selected.push(structuredClone(section));
    chars += formatted.length;
  }
  return selected.sort((left, right) => sectionKindOrder(left.kind) - sectionKindOrder(right.kind) || right.priority - left.priority || left.sectionId.localeCompare(right.sectionId));
}

function buildSystemBlocks(sections: readonly PromptSection[]): ProviderTextBlock[] {
  const result: ProviderTextBlock[] = [];
  for (const section of sections) {
    const text = formatSection(section);
    const previous = result.at(-1);
    if (previous && !previous.cacheControl && !section.cacheTtl) previous.text += `\n\n${text}`;
    else result.push({ type: "text", text, ...(section.cacheTtl ? { cacheControl: cacheControl(section.cacheTtl) } : {}) });
  }
  return result;
}

function selectTools(values: readonly PromptToolInput[], options: PromptBuildOptions): { selected: PromptToolInput[]; deferred: PromptToolInput[] } {
  const ordered = [...values].sort((left, right) => Number(right.required) - Number(left.required) || Number(right.usedRecently) - Number(left.usedRecently) || right.priority - left.priority || left.name.localeCompare(right.name));
  const selected: PromptToolInput[] = [];
  const deferred: PromptToolInput[] = [];
  let chars = 0;
  for (const tool of ordered) {
    const size = jsonChars(tool);
    const canDefer = options.enableDeferredTools && tool.deferred && !tool.required && !tool.usedRecently;
    if (canDefer || (chars + size > options.maximumToolsChars && !tool.required)) deferred.push(structuredClone(tool));
    else {
      selected.push(structuredClone(tool));
      chars += size;
    }
  }
  return { selected, deferred };
}

function normalizeMessages(values: readonly ProviderMessage[]): ProviderMessage[] {
  const result: ProviderMessage[] = [];
  for (const value of values) {
    const content = value.content.filter(nonemptyBlock).map((block) => structuredClone(block));
    if (content.length === 0) continue;
    const role = value.role === "assistant" ? "assistant" : "user";
    const previous = result.at(-1);
    if (previous?.role === role) previous.content.push(...content);
    else result.push({ role, content });
  }
  if (result.length > 0 && result[0].role === "assistant") result.unshift({ role: "user", content: [{ type: "text", text: "Continue." }] });
  validateToolPairs(result);
  return result;
}

function stripExcessMedia(messages: ProviderMessage[], maximum: number, preserveLastUser: boolean): { messages: ProviderMessage[]; stripped: number } {
  const media: Array<{ messageIndex: number; blockIndex: number }> = [];
  messages.forEach((message, messageIndex) => message.content.forEach((block, blockIndex) => {
    if (block.type === "image" || block.type === "document") media.push({ messageIndex, blockIndex });
  }));
  const preserve = new Set<string>();
  if (preserveLastUser) {
    const lastUser = findLastIndex(messages, (message) => message.role === "user");
    for (const item of media) if (item.messageIndex === lastUser) preserve.add(`${item.messageIndex}:${item.blockIndex}`);
  }
  const removeCount = Math.max(0, media.length - Math.max(0, maximum));
  const remove = new Set(media.filter((item) => !preserve.has(`${item.messageIndex}:${item.blockIndex}`)).slice(0, removeCount).map((item) => `${item.messageIndex}:${item.blockIndex}`));
  const result = messages.map((message, messageIndex) => ({
    role: message.role,
    content: message.content.flatMap((block, blockIndex): ProviderContentBlock[] => remove.has(`${messageIndex}:${blockIndex}`)
      ? [{ type: "text", text: `[${block.type} removed to satisfy media limit]` }]
      : [structuredClone(block)]),
  }));
  return { messages: result, stripped: remove.size };
}

function addSystemCacheBreakpoints(values: ProviderTextBlock[], ttl: CacheControl["ttl"]): ProviderTextBlock[] {
  if (values.length === 0) return values;
  const result = values.map(stripTextCacheControl);
  const stableIndex = Math.max(0, result.length - 2);
  result[stableIndex] = { ...result[stableIndex], cacheControl: cacheControl(ttl) };
  result[result.length - 1] = { ...result[result.length - 1], cacheControl: cacheControl(ttl) };
  return result;
}

function addToolCacheBreakpoint(values: ProviderToolDefinition[], ttl: CacheControl["ttl"]): ProviderToolDefinition[] {
  if (values.length === 0) return values;
  const result = values.map(stripToolCacheControl);
  result[result.length - 1] = { ...result[result.length - 1], cacheControl: cacheControl(ttl) };
  return result;
}

function addMessageCacheBreakpoint(values: ProviderMessage[], ttl: CacheControl["ttl"]): ProviderMessage[] {
  const result = values.map(stripMessageCacheControl);
  const lastUser = findLastIndex(result, (message) => message.role === "user");
  if (lastUser < 0) return result;
  const content = result[lastUser].content;
  if (content.length === 0) return result;
  const index = content.length - 1;
  const block = content[index];
  if (block.type === "text" || block.type === "tool_result" || block.type === "image" || block.type === "document") {
    content[index] = { ...block, cacheControl: cacheControl(ttl) } as ProviderContentBlock;
  }
  return result;
}

function stripTextCacheControl(value: ProviderTextBlock): ProviderTextBlock {
  return { type: "text", text: value.text };
}

function stripToolCacheControl(value: ProviderToolDefinition): ProviderToolDefinition {
  const { cacheControl: _cacheControl, ...rest } = value;
  return rest;
}

function stripMessageCacheControl(value: ProviderMessage): ProviderMessage {
  return {
    role: value.role,
    content: value.content.map((block): ProviderContentBlock => {
      if (block.type === "text") return { type: "text", text: block.text };
      if (block.type === "tool_result") {
        const { cacheControl: _cacheControl, ...rest } = block;
        return rest;
      }
      if (block.type === "image" || block.type === "document") {
        const { cacheControl: _cacheControl, ...rest } = block;
        return rest;
      }
      return structuredClone(block);
    }),
  };
}

function validateToolPairs(messages: readonly ProviderMessage[]): void {
  const pending = new Set<string>();
  for (const message of messages) {
    for (const block of message.content) {
      if (block.type === "tool_use") {
        if (pending.has(block.id)) throw new Error(`duplicate prompt tool use: ${block.id}`);
        pending.add(block.id);
      }
      if (block.type === "tool_result") {
        if (!pending.delete(block.toolUseId)) throw new Error(`orphan prompt tool result: ${block.toolUseId}`);
      }
    }
  }
  if (pending.size > 0) throw new Error(`prompt has unresolved tool uses: ${[...pending].join(",")}`);
}

function promptFingerprint(system: readonly ProviderTextBlock[], messages: readonly ProviderMessage[], tools: readonly ProviderToolDefinition[], model: string): string {
  return digest({ model, system: stripCacheControlJson(system), messages: stripCacheControlJson(messages), tools: stripCacheControlJson(tools) });
}

function stripCacheControlJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripCacheControlJson);
  if (value && typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) if (key !== "cacheControl" && key !== "cache_control") result[key] = stripCacheControlJson(item);
    return result;
  }
  return value;
}

function countCacheBreakpoints(value: JsonValue): number {
  if (Array.isArray(value)) return value.reduce<number>((sum, item) => sum + countCacheBreakpoints(item), 0);
  if (value && typeof value === "object") {
    return Object.entries(value).reduce<number>((sum, [key, item]) => sum + (key === "cacheControl" || key === "cache_control" ? 1 : 0) + countCacheBreakpoints(item), 0);
  }
  return 0;
}

function providerTool(value: PromptToolInput): ProviderToolDefinition {
  return {
    name: value.name,
    description: value.description,
    inputSchema: structuredClone(value.inputSchema),
    strict: value.strict,
    deferred: false,
  };
}

function nonemptyBlock(value: ProviderContentBlock): boolean {
  if (value.type === "text") return value.text.length > 0;
  if (value.type === "thinking") return value.thinking.length > 0;
  return true;
}

function normalizeSchema(value: JsonObject): JsonObject {
  const schema = structuredClone(value);
  if (schema.type === undefined) schema.type = "object";
  if (schema.type !== "object") throw new Error("prompt tool schema must have object root");
  if (schema.properties === undefined) schema.properties = {};
  if (schema.additionalProperties === undefined) schema.additionalProperties = false;
  return sortJson(schema) as JsonObject;
}

function normalizeBuildOptions(value: PromptBuildOptions): PromptBuildOptions {
  return {
    model: required(value.model, "model"),
    cacheEnabled: value.cacheEnabled,
    cacheStrategy: cacheStrategy(value.cacheStrategy),
    cacheTtl: value.cacheTtl === "1h" ? "1h" : "5m",
    maximumSystemChars: boundedInteger(value.maximumSystemChars, 1_000, 2_000_000),
    maximumToolsChars: boundedInteger(value.maximumToolsChars, 1_000, 2_000_000),
    maximumMedia: boundedInteger(value.maximumMedia, 0, 1_000),
    enableDeferredTools: value.enableDeferredTools,
    preserveLastUserMessage: value.preserveLastUserMessage,
  };
}

function sectionOrder(left: PromptSection, right: PromptSection): number {
  return Number(right.stable) - Number(left.stable) || right.priority - left.priority || sectionKindOrder(left.kind) - sectionKindOrder(right.kind) || left.sectionId.localeCompare(right.sectionId);
}

function sectionKindOrder(value: PromptSectionKind): number {
  if (value === "identity") return 0;
  if (value === "policy") return 1;
  if (value === "runtime") return 2;
  if (value === "workspace") return 3;
  if (value === "memory") return 4;
  if (value === "skills") return 5;
  if (value === "tools") return 6;
  return 7;
}

function formatSection(value: PromptSection): string {
  return `${value.title ? `## ${value.title}\n` : ""}${value.content}`.trim();
}

function normalizePromptText(value: string): string {
  return value.normalize("NFC").replaceAll("\r\n", "\n").replaceAll("\r", "\n").replace(/[ \t]+$/gm, "").replace(/\n{4,}/g, "\n\n\n").trim();
}

function sanitizeMetadata(value: JsonObject): JsonObject {
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(value)) result[key] = /secret|password|token|api.?key|authorization/i.test(key) ? "[redacted]" : item;
  return result;
}

function sectionFromJson(value: JsonObject): Parameters<ProviderPromptRuntime["upsertSection"]>[0] {
  return {
    sectionId: asString(value.section_id) || undefined,
    kind: sectionKind(asString(value.kind, "task")),
    title: asString(value.title),
    content: asString(value.content),
    priority: number(value.priority, 0),
    stable: asBoolean(value.stable, false),
    cacheTtl: value.cache_ttl === "1h" ? "1h" : value.cache_ttl === "5m" ? "5m" : null,
    sourceDigest: asString(value.source_digest) || undefined,
    metadata: asObject(value.metadata),
  };
}

function toolFromJson(value: JsonObject): PromptToolInput {
  return {
    name: asString(value.name),
    description: asString(value.description),
    inputSchema: asObject(value.input_schema),
    deferred: asBoolean(value.deferred, false),
    strict: asBoolean(value.strict, true),
    priority: number(value.priority, 0),
    usedRecently: asBoolean(value.used_recently, false),
    required: asBoolean(value.required, false),
  };
}

function buildOptionsFromJson(value: JsonObject): PromptBuildOptions {
  return {
    model: asString(value.model, "unknown"),
    cacheEnabled: asBoolean(value.cache_enabled, true),
    cacheStrategy: cacheStrategy(asString(value.cache_strategy, "system_prompt")),
    cacheTtl: value.cache_ttl === "1h" ? "1h" : "5m",
    maximumSystemChars: positive(value.maximum_system_chars, 200_000),
    maximumToolsChars: positive(value.maximum_tools_chars, 200_000),
    maximumMedia: positive(value.maximum_media, 20),
    enableDeferredTools: asBoolean(value.enable_deferred_tools, true),
    preserveLastUserMessage: asBoolean(value.preserve_last_user_message, true),
  };
}

function messagesFromJson(value: JsonValue | undefined): ProviderMessage[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const message = asObject(item);
    const rawContent = Array.isArray(message.content) ? message.content : [{ type: "text", text: asString(message.content) }];
    return {
      role: asString(message.role) === "assistant" ? "assistant" : "user",
      content: rawContent.map((block) => contentBlockFromJson(asObject(block))),
    };
  });
}

function contentBlockFromJson(value: JsonObject): ProviderContentBlock {
  const type = asString(value.type, "text");
  if (type === "tool_use") return { type, id: asString(value.id), name: asString(value.name), input: asObject(value.input) };
  if (type === "tool_result") return { type, toolUseId: asString(value.tool_use_id), content: Array.isArray(value.content) ? value.content : asString(value.content), isError: asBoolean(value.is_error, false) };
  if (type === "thinking") return { type, thinking: asString(value.thinking), signature: asString(value.signature) || null };
  if (type === "image") return { type, mediaType: asString(value.media_type, "image/png"), data: asString(value.data), width: number(value.width, 0) || undefined, height: number(value.height, 0) || undefined };
  if (type === "document") return { type, mediaType: asString(value.media_type, "application/pdf"), data: asString(value.data), title: asString(value.title) || undefined };
  return { type: "text", text: asString(value.text) };
}

function sectionToJson(value: PromptSection): JsonObject {
  return { section_id: value.sectionId, kind: value.kind, title: value.title, content: value.content, priority: value.priority, stable: value.stable, cache_ttl: value.cacheTtl, source_digest: value.sourceDigest, metadata: value.metadata };
}

function toolInputToJson(value: PromptToolInput): JsonObject {
  return { name: value.name, description: value.description, input_schema: value.inputSchema, deferred: value.deferred, strict: value.strict, priority: value.priority, used_recently: value.usedRecently, required: value.required };
}

function builtToJson(value: BuiltProviderPrompt): JsonObject {
  return {
    prompt_id: value.promptId,
    system: value.system as unknown as JsonValue,
    messages: value.messages as unknown as JsonValue,
    tools: value.tools as unknown as JsonValue,
    sections: value.sections.map(sectionToJson),
    deferred_tool_names: value.deferredToolNames,
    stripped_media_count: value.strippedMediaCount,
    system_chars: value.systemChars,
    messages_chars: value.messagesChars,
    tools_chars: value.toolsChars,
    cache_breakpoint_count: value.cacheBreakpointCount,
    fingerprint: value.fingerprint,
    created_at: value.createdAt,
  };
}

function cacheControl(ttl: CacheControl["ttl"]): CacheControl {
  return { type: "ephemeral", ttl };
}

function cacheStrategy(value: string): PromptCacheStrategy {
  if (value === "tool_based" || value === "message_boundary" || value === "none") return value;
  return "system_prompt";
}

function sectionKind(value: string): PromptSectionKind {
  if (value === "identity" || value === "policy" || value === "workspace" || value === "memory" || value === "skills" || value === "tools" || value === "runtime") return value;
  return "task";
}

function normalizeToolName(value: string): string {
  const name = value.trim();
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(name)) throw new Error(`invalid prompt tool name: ${value}`);
  return name;
}

function normalizeEffort(value: string): string {
  return value === "low" || value === "medium" || value === "high" || value === "max" ? value : "medium";
}

function findLastIndex<T>(values: readonly T[], predicate: (value: T) => boolean): number {
  for (let index = values.length - 1; index >= 0; index -= 1) if (predicate(values[index])) return index;
  return -1;
}

function sortJson(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(sortJson);
  if (value && typeof value === "object") {
    const result: JsonObject = {};
    for (const key of Object.keys(value).sort()) result[key] = sortJson(value[key]);
    return result;
  }
  return value;
}

function jsonChars(value: unknown): number {
  return canonicalJson(value).length;
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function number(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function boundedInteger(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
