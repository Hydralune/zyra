import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const QUERY_INPUT_SNAPSHOT_VERSION = "zyra.query-input/v1";

export type InputSource = "user" | "api" | "control" | "hook" | "resume" | "subagent";
export type InputDisposition = "accepted" | "duplicate" | "rejected" | "deferred" | "merged";
export type InputIntent =
  | "task"
  | "question"
  | "clarification"
  | "requirement_change"
  | "control"
  | "command"
  | "feedback"
  | "unknown";

export interface RawQueryInput {
  inputId?: string;
  source: InputSource;
  text: string;
  attachments?: readonly InputAttachment[];
  idempotencyKey?: string;
  correlationId?: string;
  parentInputId?: string | null;
  metadata?: JsonObject;
  createdAt?: string;
}

export interface InputAttachment {
  attachmentId: string;
  kind: "file" | "image" | "document" | "artifact" | "url";
  name: string;
  mediaType: string | null;
  path: string | null;
  uri: string | null;
  content: string | null;
  sizeBytes: number | null;
  digest: string;
  trusted: boolean;
  metadata: JsonObject;
}

export interface InputMention {
  kind: "file" | "agent" | "skill" | "artifact" | "url" | "symbol";
  value: string;
  start: number;
  end: number;
}

export interface ParsedCommand {
  name: string;
  argumentsText: string;
  flags: Readonly<Record<string, string | boolean>>;
  positionals: string[];
  raw: string;
}

export interface NormalizedQueryInput {
  inputId: string;
  source: InputSource;
  originalText: string;
  normalizedText: string;
  displayText: string;
  intent: InputIntent;
  command: ParsedCommand | null;
  mentions: InputMention[];
  attachments: InputAttachment[];
  idempotencyKey: string;
  correlationId: string;
  parentInputId: string | null;
  contentDigest: string;
  semanticDigest: string;
  metadata: JsonObject;
  createdAt: string;
}

export interface InputReceipt {
  receiptId: string;
  inputId: string;
  disposition: InputDisposition;
  reason: string;
  duplicateOf: string | null;
  mergedInto: string | null;
  sequence: number;
  createdAt: string;
}

export interface QueryInputSnapshot {
  version: typeof QUERY_INPUT_SNAPSHOT_VERSION;
  revision: number;
  sequence: number;
  inputs: NormalizedQueryInput[];
  receipts: InputReceipt[];
  pendingInputIds: string[];
  committedIdempotencyKeys: Record<string, string>;
  checksum: string;
}

export class QueryInputRuntime {
  private readonly inputs = new Map<string, NormalizedQueryInput>();
  private readonly receipts: InputReceipt[] = [];
  private readonly pending: string[] = [];
  private readonly committedIdempotencyKeys = new Map<string, string>();
  private revision = 0;
  private sequence = 0;

  normalize_module(value: JsonObject): JsonObject {
    return inputToJson(this.normalize(rawFromJson(value)));
  }

  intent_module(value: JsonObject): JsonObject {
    const normalized = normalizeText(asString(value.text));
    return {
      intent: classifyIntent(normalized),
      command: parseCommand(normalized) ? commandToJson(parseCommand(normalized)!) : null,
      mentions: parseMentions(normalized).map(mentionToJson),
    };
  }

  dedup_module(value: JsonObject): JsonObject {
    const result = this.ingest(rawFromJson(value));
    return {
      input: inputToJson(result.input),
      receipt: receiptToJson(result.receipt),
    };
  }

  normalize(raw: RawQueryInput): NormalizedQueryInput {
    const originalText = String(raw.text ?? "");
    const normalizedText = normalizeText(originalText);
    if (!normalizedText && (raw.attachments?.length ?? 0) === 0) throw new Error("query input is empty");
    if (normalizedText.length > 2_000_000) throw new Error("query input exceeds maximum length");
    const attachments = normalizeAttachments(raw.attachments ?? []);
    const createdAt = normalizeTimestamp(raw.createdAt);
    const command = parseCommand(normalizedText);
    const mentions = parseMentions(normalizedText);
    const contentDigest = digest({ text: normalizedText, attachments: attachments.map(attachmentIdentity) });
    const semanticDigest = digest({
      text: semanticText(normalizedText),
      attachments: attachments.map((item) => [item.kind, item.digest]).sort(),
    });
    return {
      inputId: raw.inputId?.trim() || randomUUID(),
      source: inputSource(raw.source),
      originalText,
      normalizedText,
      displayText: sanitizeDisplayText(normalizedText),
      intent: command ? "command" : classifyIntent(normalizedText),
      command,
      mentions,
      attachments,
      idempotencyKey: raw.idempotencyKey?.trim() || `input:${contentDigest}`,
      correlationId: raw.correlationId?.trim() || randomUUID(),
      parentInputId: raw.parentInputId?.trim() || null,
      contentDigest,
      semanticDigest,
      metadata: sanitizeMetadata(raw.metadata ?? {}),
      createdAt,
    };
  }

  ingest(raw: RawQueryInput): { input: NormalizedQueryInput; receipt: InputReceipt } {
    const input = this.normalize(raw);
    if (this.inputs.has(input.inputId)) {
      const existing = this.inputs.get(input.inputId)!;
      if (existing.contentDigest !== input.contentDigest) throw new Error(`input id conflict: ${input.inputId}`);
      return { input: structuredClone(existing), receipt: this.receipt(input, "duplicate", "same_input_id", existing.inputId, null) };
    }
    const idempotent = this.committedIdempotencyKeys.get(input.idempotencyKey);
    if (idempotent) {
      const existing = this.inputs.get(idempotent)!;
      return { input: structuredClone(existing), receipt: this.receipt(input, "duplicate", "same_idempotency_key", idempotent, null) };
    }
    const exact = this.findByDigest(input.contentDigest);
    if (exact) {
      return { input: structuredClone(exact), receipt: this.receipt(input, "duplicate", "same_content_digest", exact.inputId, null) };
    }
    const semantic = this.findSemanticDuplicate(input);
    if (semantic && shouldMerge(semantic, input)) {
      const merged = this.mergeInputs(semantic, input);
      this.inputs.set(semantic.inputId, merged);
      this.committedIdempotencyKeys.set(input.idempotencyKey, semantic.inputId);
      this.revision += 1;
      return { input: structuredClone(merged), receipt: this.receipt(input, "merged", "semantic_duplicate", semantic.inputId, semantic.inputId) };
    }
    this.inputs.set(input.inputId, input);
    this.pending.push(input.inputId);
    this.committedIdempotencyKeys.set(input.idempotencyKey, input.inputId);
    this.revision += 1;
    return { input: structuredClone(input), receipt: this.receipt(input, "accepted", "new_input", null, null) };
  }

  dequeue(maximum = 1): NormalizedQueryInput[] {
    const count = Math.max(0, Math.floor(maximum));
    const result: NormalizedQueryInput[] = [];
    while (result.length < count && this.pending.length > 0) {
      const id = this.pending.shift()!;
      const input = this.inputs.get(id);
      if (input) result.push(structuredClone(input));
    }
    if (result.length > 0) this.revision += 1;
    return result;
  }

  defer(inputId: string, reason: string): InputReceipt {
    const input = this.requireInput(inputId);
    const index = this.pending.indexOf(inputId);
    if (index >= 0) this.pending.splice(index, 1);
    this.pending.push(inputId);
    this.revision += 1;
    return this.receipt(input, "deferred", reason.trim().slice(0, 1_024), null, null);
  }

  reject(inputId: string, reason: string): InputReceipt {
    const input = this.requireInput(inputId);
    const index = this.pending.indexOf(inputId);
    if (index >= 0) this.pending.splice(index, 1);
    this.revision += 1;
    return this.receipt(input, "rejected", reason.trim().slice(0, 1_024), null, null);
  }

  snapshot(): QueryInputSnapshot {
    const unsigned: Omit<QueryInputSnapshot, "checksum"> = {
      version: QUERY_INPUT_SNAPSHOT_VERSION,
      revision: this.revision,
      sequence: this.sequence,
      inputs: structuredClone([...this.inputs.values()]),
      receipts: structuredClone(this.receipts),
      pendingInputIds: [...this.pending],
      committedIdempotencyKeys: Object.fromEntries(this.committedIdempotencyKeys),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: QueryInputSnapshot): void {
    if (snapshot.version !== QUERY_INPUT_SNAPSHOT_VERSION) throw new Error("unsupported query input snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("query input snapshot checksum mismatch");
    const ids = new Set(snapshot.inputs.map((item) => item.inputId));
    for (const id of snapshot.pendingInputIds) if (!ids.has(id)) throw new Error(`snapshot pending input is missing: ${id}`);
    this.inputs.clear();
    for (const input of snapshot.inputs) this.inputs.set(input.inputId, structuredClone(input));
    this.receipts.splice(0, this.receipts.length, ...structuredClone(snapshot.receipts));
    this.pending.splice(0, this.pending.length, ...snapshot.pendingInputIds);
    this.committedIdempotencyKeys.clear();
    for (const [key, id] of Object.entries(snapshot.committedIdempotencyKeys)) {
      if (!ids.has(id)) throw new Error(`snapshot idempotency points to missing input: ${id}`);
      this.committedIdempotencyKeys.set(key, id);
    }
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
  }

  private receipt(
    input: NormalizedQueryInput,
    disposition: InputDisposition,
    reason: string,
    duplicateOf: string | null,
    mergedInto: string | null,
  ): InputReceipt {
    this.sequence += 1;
    const receipt: InputReceipt = {
      receiptId: randomUUID(),
      inputId: input.inputId,
      disposition,
      reason,
      duplicateOf,
      mergedInto,
      sequence: this.sequence,
      createdAt: new Date().toISOString(),
    };
    this.receipts.push(receipt);
    return structuredClone(receipt);
  }

  private findByDigest(contentDigest: string): NormalizedQueryInput | null {
    for (const input of this.inputs.values()) if (input.contentDigest === contentDigest) return input;
    return null;
  }

  private findSemanticDuplicate(input: NormalizedQueryInput): NormalizedQueryInput | null {
    const cutoff = Date.parse(input.createdAt) - 5 * 60 * 1_000;
    for (const candidate of [...this.inputs.values()].reverse()) {
      if (Date.parse(candidate.createdAt) < cutoff) break;
      if (candidate.semanticDigest === input.semanticDigest) return candidate;
    }
    return null;
  }

  private mergeInputs(left: NormalizedQueryInput, right: NormalizedQueryInput): NormalizedQueryInput {
    const attachments = normalizeAttachments([...left.attachments, ...right.attachments]);
    const metadata = { ...left.metadata, ...right.metadata, merged_input_ids: [left.inputId, right.inputId] };
    return {
      ...structuredClone(left),
      source: sourcePriority(right.source) > sourcePriority(left.source) ? right.source : left.source,
      originalText: left.originalText.length >= right.originalText.length ? left.originalText : right.originalText,
      normalizedText: left.normalizedText.length >= right.normalizedText.length ? left.normalizedText : right.normalizedText,
      displayText: left.displayText.length >= right.displayText.length ? left.displayText : right.displayText,
      intent: intentPriority(right.intent) > intentPriority(left.intent) ? right.intent : left.intent,
      command: right.command ?? left.command,
      mentions: uniqueMentions([...left.mentions, ...right.mentions]),
      attachments,
      metadata,
      contentDigest: digest({ text: left.normalizedText, attachments: attachments.map(attachmentIdentity) }),
    };
  }

  private requireInput(inputId: string): NormalizedQueryInput {
    const input = this.inputs.get(inputId);
    if (!input) throw new Error(`query input not found: ${inputId}`);
    return input;
  }
}

export function normalizeText(value: string): string {
  return value
    .normalize("NFC")
    .replaceAll("\r\n", "\n")
    .replaceAll("\r", "\n")
    .replace(/[\u0000\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "")
    .split("\n")
    .map((line) => line.replace(/[ \t]+$/g, ""))
    .join("\n")
    .replace(/\n{4,}/g, "\n\n\n")
    .trim();
}

export function classifyIntent(text: string): InputIntent {
  const normalized = text.toLowerCase();
  if (!normalized) return "unknown";
  if (/^\/(compact|clear|cost|doctor|mcp|memory|permissions|resume|tasks)\b/.test(normalized)) return "command";
  if (/^(stop|cancel|pause|resume|abort)\b/.test(normalized)) return "control";
  if (/\b(change|instead|new requirement|update requirement|改成|需求变更|不要再)\b/.test(normalized)) return "requirement_change";
  if (/\b(why|how|what|when|where|which|can you|could you|为什么|怎么|什么|是否|吗[？?]?)\b/.test(normalized) || /[?？]$/.test(normalized)) return "question";
  if (/\b(i mean|clarify|to be clear|specifically|更准确|澄清|我的意思)\b/.test(normalized)) return "clarification";
  if (/\b(feedback|review result|looks wrong|bug|问题|审核|审查|不对)\b/.test(normalized)) return "feedback";
  return "task";
}

export function parseCommand(text: string): ParsedCommand | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("/")) return null;
  const newline = trimmed.indexOf("\n");
  const commandLine = newline < 0 ? trimmed : trimmed.slice(0, newline);
  const tokens = shellTokens(commandLine.slice(1));
  if (tokens.length === 0) return null;
  const name = tokens.shift()!.toLowerCase();
  if (!/^[a-z][a-z0-9_-]{0,63}$/.test(name)) return null;
  const flags: Record<string, string | boolean> = {};
  const positionals: string[] = [];
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token.startsWith("--")) {
      const equals = token.indexOf("=");
      if (equals > 2) flags[token.slice(2, equals)] = token.slice(equals + 1);
      else if (tokens[index + 1] && !tokens[index + 1].startsWith("-")) flags[token.slice(2)] = tokens[++index];
      else flags[token.slice(2)] = true;
    } else if (token.startsWith("-") && token.length > 1) {
      for (const short of token.slice(1)) flags[short] = true;
    } else positionals.push(token);
  }
  return {
    name,
    argumentsText: commandLine.slice(name.length + 1).trim(),
    flags: Object.freeze(flags),
    positionals,
    raw: commandLine,
  };
}

export function parseMentions(text: string): InputMention[] {
  const mentions: InputMention[] = [];
  const patterns: Array<{ kind: InputMention["kind"]; expression: RegExp }> = [
    { kind: "file", expression: /@file:([^\s,;]+)/g },
    { kind: "agent", expression: /@agent:([A-Za-z0-9_.-]+)/g },
    { kind: "skill", expression: /@skill:([A-Za-z0-9_./-]+)/g },
    { kind: "artifact", expression: /@artifact:([A-Za-z0-9_.:-]+)/g },
    { kind: "symbol", expression: /@symbol:([A-Za-z0-9_.$:#-]+)/g },
    { kind: "url", expression: /https?:\/\/[^\s)\]}>,]+/g },
  ];
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern.expression)) {
      if (match.index === undefined) continue;
      mentions.push({
        kind: pattern.kind,
        value: match[1] ?? match[0],
        start: match.index,
        end: match.index + match[0].length,
      });
    }
  }
  return uniqueMentions(mentions).sort((left, right) => left.start - right.start || left.end - right.end);
}

function normalizeAttachments(values: readonly InputAttachment[]): InputAttachment[] {
  const byDigest = new Map<string, InputAttachment>();
  for (const value of values) {
    const kind = attachmentKind(value.kind);
    const content = value.content === null || value.content === undefined ? null : String(value.content);
    const path = value.path?.trim() || null;
    const uri = value.uri?.trim() || null;
    const sourceDigest = value.digest?.trim() || digest({ kind, path, uri, content });
    const normalized: InputAttachment = {
      attachmentId: value.attachmentId?.trim() || randomUUID(),
      kind,
      name: value.name?.trim() || path?.split(/[\\/]/).at(-1) || uri || "attachment",
      mediaType: value.mediaType?.trim().toLowerCase() || null,
      path,
      uri,
      content,
      sizeBytes: value.sizeBytes === null || value.sizeBytes === undefined ? (content === null ? null : Buffer.byteLength(content)) : Math.max(0, Math.floor(value.sizeBytes)),
      digest: sourceDigest,
      trusted: Boolean(value.trusted),
      metadata: sanitizeMetadata(value.metadata ?? {}),
    };
    const existing = byDigest.get(sourceDigest);
    if (!existing || attachmentQuality(normalized) > attachmentQuality(existing)) byDigest.set(sourceDigest, normalized);
  }
  return [...byDigest.values()].sort((left, right) => left.kind.localeCompare(right.kind) || left.name.localeCompare(right.name));
}

function sanitizeMetadata(value: JsonObject): JsonObject {
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(value)) {
    if (/password|secret|api.?key|authorization|token|cookie/i.test(key)) result[key] = "[redacted]";
    else result[key.slice(0, 128)] = sanitizeValue(item, 0);
  }
  return result;
}

function sanitizeValue(value: JsonValue, depth: number): JsonValue {
  if (depth >= 8) return "[depth-limited]";
  if (typeof value === "string") return value.slice(0, 16_384);
  if (Array.isArray(value)) return value.slice(0, 512).map((item) => sanitizeValue(item, depth + 1));
  if (value && typeof value === "object") return sanitizeMetadata(value);
  return value;
}

function sanitizeDisplayText(value: string): string {
  return value
    .replace(/(sk-ant-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+/g, "$1...[redacted]")
    .replace(/(Bearer\s+)[A-Za-z0-9._~+/-]+/gi, "$1[redacted]")
    .replace(/([?&](?:token|key|secret)=)[^&\s]+/gi, "$1[redacted]");
}

function semanticText(value: string): string {
  return value
    .toLowerCase()
    .replace(/```[\s\S]*?```/g, (block) => digest(block))
    .replace(/\b[0-9a-f]{7,64}\b/g, "<hash>")
    .replace(/\b\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?z\b/g, "<timestamp>")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function shellTokens(value: string): string[] {
  const result: string[] = [];
  let token = "";
  let quote: "'" | '"' | null = null;
  let escaped = false;
  for (const character of value) {
    if (escaped) {
      token += character;
      escaped = false;
      continue;
    }
    if (character === "\\" && quote !== "'") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (character === quote) quote = null;
      else token += character;
      continue;
    }
    if (character === "'" || character === '"') {
      quote = character;
      continue;
    }
    if (/\s/.test(character)) {
      if (token) result.push(token);
      token = "";
      continue;
    }
    token += character;
  }
  if (escaped) token += "\\";
  if (quote) throw new Error("unterminated command quote");
  if (token) result.push(token);
  return result;
}

function shouldMerge(left: NormalizedQueryInput, right: NormalizedQueryInput): boolean {
  if (left.intent === "requirement_change" || right.intent === "requirement_change") return false;
  if (left.command || right.command) return false;
  if (left.source === "control" || right.source === "control") return false;
  return true;
}

function uniqueMentions(values: readonly InputMention[]): InputMention[] {
  const seen = new Set<string>();
  const result: InputMention[] = [];
  for (const value of values) {
    const key = `${value.kind}:${value.value}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(structuredClone(value));
  }
  return result;
}

function sourcePriority(value: InputSource): number {
  if (value === "control") return 100;
  if (value === "user") return 80;
  if (value === "api") return 60;
  if (value === "resume") return 40;
  if (value === "hook") return 20;
  return 10;
}

function intentPriority(value: InputIntent): number {
  if (value === "requirement_change") return 100;
  if (value === "control" || value === "command") return 90;
  if (value === "clarification") return 70;
  if (value === "feedback") return 60;
  if (value === "task") return 50;
  if (value === "question") return 40;
  return 0;
}

function attachmentQuality(value: InputAttachment): number {
  return (value.trusted ? 100 : 0) + (value.content ? 20 : 0) + (value.path ? 10 : 0) + (value.uri ? 5 : 0);
}

function attachmentIdentity(value: InputAttachment): JsonObject {
  return { kind: value.kind, digest: value.digest, path: value.path, uri: value.uri, media_type: value.mediaType };
}

function rawFromJson(value: JsonObject): RawQueryInput {
  const attachments = Array.isArray(value.attachments) ? value.attachments.map((item) => attachmentFromJson(asObject(item))) : [];
  return {
    inputId: asString(value.input_id) || undefined,
    source: inputSource(asString(value.source, "user")),
    text: asString(value.text),
    attachments,
    idempotencyKey: asString(value.idempotency_key) || undefined,
    correlationId: asString(value.correlation_id) || undefined,
    parentInputId: asString(value.parent_input_id) || null,
    metadata: asObject(value.metadata),
    createdAt: asString(value.created_at) || undefined,
  };
}

function attachmentFromJson(value: JsonObject): InputAttachment {
  return {
    attachmentId: asString(value.attachment_id, randomUUID()),
    kind: attachmentKind(asString(value.kind, "file")),
    name: asString(value.name),
    mediaType: asString(value.media_type) || null,
    path: asString(value.path) || null,
    uri: asString(value.uri) || null,
    content: typeof value.content === "string" ? value.content : null,
    sizeBytes: typeof value.size_bytes === "number" ? value.size_bytes : null,
    digest: asString(value.digest),
    trusted: asBoolean(value.trusted, false),
    metadata: asObject(value.metadata),
  };
}

function inputToJson(value: NormalizedQueryInput): JsonObject {
  return {
    input_id: value.inputId,
    source: value.source,
    original_text: value.originalText,
    normalized_text: value.normalizedText,
    display_text: value.displayText,
    intent: value.intent,
    command: value.command ? commandToJson(value.command) : null,
    mentions: value.mentions.map(mentionToJson),
    attachments: value.attachments.map(attachmentToJson),
    idempotency_key: value.idempotencyKey,
    correlation_id: value.correlationId,
    parent_input_id: value.parentInputId,
    content_digest: value.contentDigest,
    semantic_digest: value.semanticDigest,
    metadata: value.metadata,
    created_at: value.createdAt,
  };
}

function commandToJson(value: ParsedCommand): JsonObject {
  return { name: value.name, arguments_text: value.argumentsText, flags: value.flags, positionals: value.positionals, raw: value.raw };
}

function mentionToJson(value: InputMention): JsonObject {
  return { kind: value.kind, value: value.value, start: value.start, end: value.end };
}

function attachmentToJson(value: InputAttachment): JsonObject {
  return {
    attachment_id: value.attachmentId,
    kind: value.kind,
    name: value.name,
    media_type: value.mediaType,
    path: value.path,
    uri: value.uri,
    content: value.content,
    size_bytes: value.sizeBytes,
    digest: value.digest,
    trusted: value.trusted,
    metadata: value.metadata,
  };
}

function receiptToJson(value: InputReceipt): JsonObject {
  return {
    receipt_id: value.receiptId,
    input_id: value.inputId,
    disposition: value.disposition,
    reason: value.reason,
    duplicate_of: value.duplicateOf,
    merged_into: value.mergedInto,
    sequence: value.sequence,
    created_at: value.createdAt,
  };
}

function inputSource(value: string): InputSource {
  if (value === "api" || value === "control" || value === "hook" || value === "resume" || value === "subagent") return value;
  return "user";
}

function attachmentKind(value: string): InputAttachment["kind"] {
  if (value === "image" || value === "document" || value === "artifact" || value === "url") return value;
  return "file";
}

function normalizeTimestamp(value?: string): string {
  if (!value) return new Date().toISOString();
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid input timestamp: ${value}`);
  return new Date(timestamp).toISOString();
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
