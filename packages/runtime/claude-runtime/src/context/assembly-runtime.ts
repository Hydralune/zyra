import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";
import {
  ContextTokenRuntime,
  type ContextWindowPlan,
  type ContextWindowSegment,
} from "./token-runtime.ts";

export const CONTEXT_ASSEMBLY_SNAPSHOT_VERSION = "zyra.context-assembly/v1";

export type ContextSource =
  | "system"
  | "user"
  | "assistant"
  | "tool"
  | "memory"
  | "skill"
  | "workspace"
  | "artifact"
  | "hook"
  | "control"
  | "restore";
export type ContextTrust = "untrusted" | "external" | "internal" | "verified" | "system";
export type ContextDisclosure = "public" | "model_only" | "worker_only" | "secret";
export type ContextSectionState = "candidate" | "selected" | "dropped" | "redacted" | "expired";

export interface ContextProvenance {
  source: ContextSource;
  sourceId: string;
  sourceDigest: string;
  parentSectionId: string | null;
  trust: ContextTrust;
  createdAt: string;
  expiresAt: string | null;
}

export interface ContextSection {
  sectionId: string;
  kind: "system" | "instruction" | "message" | "tool_use" | "tool_result" | "memory" | "attachment" | "notice";
  title: string;
  content: JsonValue;
  text: string;
  priority: number;
  pinned: boolean;
  required: boolean;
  disclosure: ContextDisclosure;
  toolPairId: string | null;
  turnIndex: number | null;
  sequence: number;
  provenance: ContextProvenance;
  metadata: JsonObject;
  tokenEstimate: number;
  contentDigest: string;
  state: ContextSectionState;
}

export interface ContextPolicy {
  maximumTokens: number;
  reservedOutputTokens: number;
  reservedToolTokens: number;
  maximumSectionTokens: number;
  maximumMemoryTokens: number;
  maximumAttachmentTokens: number;
  minimumRecentTurns: number;
  includeUntrusted: boolean;
  includeExternal: boolean;
  allowSecretsToModel: boolean;
  redactPatterns: string[];
}

export interface AssembledContext {
  assemblyId: string;
  revision: number;
  selected: ContextSection[];
  dropped: ContextSection[];
  redacted: ContextSection[];
  selectedTokens: number;
  droppedTokens: number;
  budgetTokens: number;
  pairInvariantOk: boolean;
  systemPrompt: string;
  messages: JsonObject[];
  disclosureDigest: string;
  contextDigest: string;
  assembledAt: string;
}

export interface ContextAssemblySnapshot {
  version: typeof CONTEXT_ASSEMBLY_SNAPSHOT_VERSION;
  revision: number;
  sequence: number;
  policy: ContextPolicy;
  sections: ContextSection[];
  lastAssembly: AssembledContext | null;
  tokenRuntime: ReturnType<ContextTokenRuntime["snapshot"]>;
  checksum: string;
}

export class ContextAssemblyRuntime {
  private readonly sections = new Map<string, ContextSection>();
  private readonly tokens: ContextTokenRuntime;
  private policy: ContextPolicy;
  private lastAssembly: AssembledContext | null = null;
  private revision = 0;
  private sequence = 0;

  constructor(policy: Partial<ContextPolicy> = {}) {
    this.policy = normalizePolicy(policy);
    this.tokens = new ContextTokenRuntime(this.policy.maximumTokens);
  }

  assemble_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "assemble");
    if (action === "add") return sectionToJson(this.add(sectionInputFromJson(asObject(value.section))));
    if (action === "remove") return { removed: this.remove(asString(value.section_id)) };
    if (action === "configure") return policyToJson(this.configure(asObject(value.policy) as Partial<ContextPolicy>));
    return assemblyToJson(this.assemble());
  }

  select_module(value: JsonObject): JsonObject {
    const maximumTokens = positive(value.maximum_tokens, this.availableTokens());
    const plan = this.select(maximumTokens);
    return planToJson(plan);
  }

  disclose_module(value: JsonObject): JsonObject {
    const target = asString(value.target, "model");
    const section = this.requireSection(asString(value.section_id));
    return {
      allowed: this.canDisclose(section, target === "worker" ? "worker" : target === "public" ? "public" : "model"),
      section_id: section.sectionId,
      disclosure: section.disclosure,
      trust: section.provenance.trust,
    };
  }

  pairs_module(value: JsonObject): JsonObject {
    const ids = Array.isArray(value.section_ids) ? new Set(value.section_ids.map(String)) : new Set(this.sections.keys());
    const selected = [...ids].map((id) => this.sections.get(id)).filter((item): item is ContextSection => Boolean(item));
    const result = validateToolPairs(selected);
    return { ok: result.ok, missing_use_ids: result.missingUseIds, missing_result_ids: result.missingResultIds };
  }

  add(input: Omit<ContextSection, "sectionId" | "sequence" | "tokenEstimate" | "contentDigest" | "state"> & { sectionId?: string }): ContextSection {
    const sectionId = input.sectionId?.trim() || randomUUID();
    if (this.sections.has(sectionId)) throw new Error(`context section already exists: ${sectionId}`);
    const sanitized = sanitizeContent(input.content, this.policy.redactPatterns);
    const text = input.text || contentText(sanitized.value);
    const estimate = this.tokens.estimate(text).estimatedTokens;
    if (estimate > this.policy.maximumSectionTokens && input.required) {
      throw new Error(`required context section exceeds per-section limit: ${sectionId}`);
    }
    this.sequence += 1;
    const section: ContextSection = {
      sectionId,
      kind: contextKind(input.kind),
      title: input.title.trim().slice(0, 512),
      content: sanitized.value,
      text,
      priority: bounded(input.priority, -1_000, 1_000),
      pinned: input.pinned,
      required: input.required,
      disclosure: disclosure(input.disclosure),
      toolPairId: input.toolPairId?.trim() || null,
      turnIndex: input.turnIndex === null ? null : Math.max(0, Math.floor(input.turnIndex)),
      sequence: this.sequence,
      provenance: normalizeProvenance(input.provenance),
      metadata: { ...asObject(input.metadata), redaction_count: sanitized.redactionCount },
      tokenEstimate: estimate,
      contentDigest: digest(sanitized.value),
      state: sanitized.redactionCount > 0 ? "redacted" : "candidate",
    };
    this.sections.set(sectionId, section);
    this.revision += 1;
    return structuredClone(section);
  }

  replace(sectionId: string, content: JsonValue, expectedDigest: string): ContextSection {
    const section = this.requireSection(sectionId);
    if (section.contentDigest !== expectedDigest) throw new Error("context section compare-and-swap conflict");
    const sanitized = sanitizeContent(content, this.policy.redactPatterns);
    section.content = sanitized.value;
    section.text = contentText(sanitized.value);
    section.tokenEstimate = this.tokens.estimate(section.text).estimatedTokens;
    section.contentDigest = digest(sanitized.value);
    section.metadata = { ...section.metadata, redaction_count: sanitized.redactionCount };
    section.state = sanitized.redactionCount > 0 ? "redacted" : "candidate";
    this.revision += 1;
    return structuredClone(section);
  }

  remove(sectionId: string): boolean {
    const section = this.sections.get(sectionId);
    if (!section) return false;
    if (section.required || section.pinned) throw new Error(`required context section cannot be removed: ${sectionId}`);
    const removed = this.sections.delete(sectionId);
    if (removed) this.revision += 1;
    return removed;
  }

  expire(now = new Date().toISOString()): string[] {
    const timestamp = Date.parse(now);
    if (!Number.isFinite(timestamp)) throw new Error(`invalid context expiry timestamp: ${now}`);
    const expired: string[] = [];
    for (const section of this.sections.values()) {
      if (!section.provenance.expiresAt || Date.parse(section.provenance.expiresAt) > timestamp) continue;
      if (section.required || section.pinned) continue;
      section.state = "expired";
      expired.push(section.sectionId);
    }
    if (expired.length > 0) this.revision += 1;
    return expired;
  }

  configure(value: Partial<ContextPolicy>): ContextPolicy {
    this.policy = normalizePolicy({ ...this.policy, ...value });
    this.revision += 1;
    return structuredClone(this.policy);
  }

  select(maximumTokens = this.availableTokens()): ContextWindowPlan {
    this.expire();
    const eligible = [...this.sections.values()].filter((section) => this.eligible(section));
    const segments: ContextWindowSegment[] = eligible.map((section) => ({
      id: section.sectionId,
      kind: segmentKind(section.kind),
      content: section.text,
      pinned: section.pinned || section.required,
      toolPairId: section.toolPairId,
      priority: effectivePriority(section, eligible),
      createdSequence: section.sequence,
      metadata: {
        section_kind: section.kind,
        disclosure: section.disclosure,
        trust: section.provenance.trust,
        source: section.provenance.source,
      },
    }));
    const plan = this.tokens.planWindow(segments, Math.max(1, Math.floor(maximumTokens)));
    const selectedIds = new Set(plan.selected.map((item) => item.id));
    for (const section of this.sections.values()) {
      if (section.state === "expired") continue;
      section.state = selectedIds.has(section.sectionId)
        ? section.state === "redacted" ? "redacted" : "selected"
        : "dropped";
    }
    const pairResult = validateToolPairs([...selectedIds].map((id) => this.requireSection(id)));
    if (!pairResult.ok || !plan.pairInvariantOk) throw new Error("context selection violated tool-pair invariants");
    this.revision += 1;
    return plan;
  }

  assemble(maximumTokens = this.availableTokens()): AssembledContext {
    const plan = this.select(maximumTokens);
    const selected = plan.selected.map((segment) => this.requireSection(segment.id));
    const dropped = plan.dropped.map((segment) => this.requireSection(segment.id));
    const redacted = selected.filter((section) => section.state === "redacted");
    const ordered = selected.sort(sectionOrder);
    const systemSections = ordered.filter((section) => section.kind === "system" || section.kind === "instruction" || section.kind === "notice");
    const messageSections = ordered.filter((section) => !systemSections.includes(section));
    const systemPrompt = systemSections.map(formatSection).join("\n\n").trim();
    const messages = toMessages(messageSections);
    const assembled: AssembledContext = {
      assemblyId: randomUUID(),
      revision: this.revision,
      selected: structuredClone(ordered),
      dropped: structuredClone(dropped),
      redacted: structuredClone(redacted),
      selectedTokens: plan.selectedTokens,
      droppedTokens: plan.droppedTokens,
      budgetTokens: plan.budgetTokens,
      pairInvariantOk: plan.pairInvariantOk,
      systemPrompt,
      messages,
      disclosureDigest: digest(ordered.map((section) => [section.sectionId, section.disclosure, section.provenance.trust])),
      contextDigest: digest({ system: systemPrompt, messages, sections: ordered.map((section) => section.contentDigest) }),
      assembledAt: new Date().toISOString(),
    };
    this.lastAssembly = assembled;
    this.revision += 1;
    return structuredClone(assembled);
  }

  canDisclose(section: ContextSection, target: "model" | "worker" | "public"): boolean {
    if (section.disclosure === "secret") return target === "worker" && this.policy.allowSecretsToModel;
    if (section.disclosure === "worker_only") return target === "worker";
    if (section.disclosure === "model_only") return target === "model" || target === "worker";
    return true;
  }

  availableTokens(): number {
    return Math.max(1, this.policy.maximumTokens - this.policy.reservedOutputTokens - this.policy.reservedToolTokens);
  }

  snapshot(): ContextAssemblySnapshot {
    const unsigned: Omit<ContextAssemblySnapshot, "checksum"> = {
      version: CONTEXT_ASSEMBLY_SNAPSHOT_VERSION,
      revision: this.revision,
      sequence: this.sequence,
      policy: structuredClone(this.policy),
      sections: structuredClone([...this.sections.values()]),
      lastAssembly: this.lastAssembly ? structuredClone(this.lastAssembly) : null,
      tokenRuntime: this.tokens.snapshot(),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: ContextAssemblySnapshot): void {
    if (snapshot.version !== CONTEXT_ASSEMBLY_SNAPSHOT_VERSION) throw new Error("unsupported context assembly snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("context assembly snapshot checksum mismatch");
    const ids = new Set<string>();
    for (const section of snapshot.sections) {
      if (ids.has(section.sectionId)) throw new Error(`duplicate context section in snapshot: ${section.sectionId}`);
      if (digest(section.content) !== section.contentDigest) throw new Error(`context section digest mismatch: ${section.sectionId}`);
      ids.add(section.sectionId);
    }
    this.sections.clear();
    for (const section of snapshot.sections) this.sections.set(section.sectionId, structuredClone(section));
    this.policy = normalizePolicy(snapshot.policy);
    this.lastAssembly = snapshot.lastAssembly ? structuredClone(snapshot.lastAssembly) : null;
    this.tokens.restore(snapshot.tokenRuntime);
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
  }

  private eligible(section: ContextSection): boolean {
    if (section.state === "expired") return false;
    if (!this.canDisclose(section, "model")) return false;
    if (section.provenance.trust === "untrusted" && !this.policy.includeUntrusted) return false;
    if (section.provenance.trust === "external" && !this.policy.includeExternal) return false;
    if (section.kind === "memory" && section.tokenEstimate > this.policy.maximumMemoryTokens && !section.required) return false;
    if (section.kind === "attachment" && section.tokenEstimate > this.policy.maximumAttachmentTokens && !section.required) return false;
    return true;
  }

  private requireSection(sectionId: string): ContextSection {
    const section = this.sections.get(sectionId);
    if (!section) throw new Error(`context section not found: ${sectionId}`);
    return section;
  }
}

function normalizePolicy(value: Partial<ContextPolicy>): ContextPolicy {
  const maximumTokens = positive(value.maximumTokens, 200_000);
  const reservedOutputTokens = bounded(value.reservedOutputTokens ?? 8_192, 0, maximumTokens - 1);
  const reservedToolTokens = bounded(value.reservedToolTokens ?? 8_000, 0, maximumTokens - reservedOutputTokens - 1);
  return {
    maximumTokens,
    reservedOutputTokens,
    reservedToolTokens,
    maximumSectionTokens: bounded(value.maximumSectionTokens ?? 50_000, 1, maximumTokens),
    maximumMemoryTokens: bounded(value.maximumMemoryTokens ?? 40_000, 1, maximumTokens),
    maximumAttachmentTokens: bounded(value.maximumAttachmentTokens ?? 50_000, 1, maximumTokens),
    minimumRecentTurns: bounded(value.minimumRecentTurns ?? 3, 0, 100),
    includeUntrusted: value.includeUntrusted ?? false,
    includeExternal: value.includeExternal ?? true,
    allowSecretsToModel: value.allowSecretsToModel ?? false,
    redactPatterns: [...new Set(value.redactPatterns ?? defaultRedactPatterns())],
  };
}

function normalizeProvenance(value: ContextProvenance): ContextProvenance {
  return {
    source: contextSource(value.source),
    sourceId: required(value.sourceId, "context source id"),
    sourceDigest: value.sourceDigest.trim() || digest(value.sourceId),
    parentSectionId: value.parentSectionId?.trim() || null,
    trust: contextTrust(value.trust),
    createdAt: normalizeTimestamp(value.createdAt),
    expiresAt: value.expiresAt ? normalizeTimestamp(value.expiresAt) : null,
  };
}

function sanitizeContent(value: JsonValue, patternSources: readonly string[]): { value: JsonValue; redactionCount: number } {
  const patterns = patternSources.map((source) => {
    try {
      return new RegExp(source, "gi");
    } catch {
      return null;
    }
  }).filter((item): item is RegExp => Boolean(item));
  let redactionCount = 0;
  const visit = (input: JsonValue, depth: number): JsonValue => {
    if (depth > 16) return "[depth-limited]";
    if (typeof input === "string") {
      let result = input;
      for (const pattern of patterns) {
        result = result.replace(pattern, () => {
          redactionCount += 1;
          return "[redacted]";
        });
      }
      return result;
    }
    if (Array.isArray(input)) return input.map((item) => visit(item, depth + 1));
    if (input && typeof input === "object") {
      const result: JsonObject = {};
      for (const [key, item] of Object.entries(input)) {
        if (/password|secret|api.?key|authorization|access.?token|cookie/i.test(key)) {
          result[key] = "[redacted]";
          redactionCount += 1;
        } else result[key] = visit(item, depth + 1);
      }
      return result;
    }
    return input;
  };
  return { value: visit(value, 0), redactionCount };
}

function validateToolPairs(sections: readonly ContextSection[]): { ok: boolean; missingUseIds: string[]; missingResultIds: string[] } {
  const uses = new Set<string>();
  const results = new Set<string>();
  for (const section of sections) {
    if (!section.toolPairId) continue;
    if (section.kind === "tool_use") uses.add(section.toolPairId);
    if (section.kind === "tool_result") results.add(section.toolPairId);
  }
  const missingUseIds = [...results].filter((id) => !uses.has(id)).sort();
  const missingResultIds = [...uses].filter((id) => !results.has(id)).sort();
  return { ok: missingUseIds.length === 0 && missingResultIds.length === 0, missingUseIds, missingResultIds };
}

function effectivePriority(section: ContextSection, all: readonly ContextSection[]): number {
  let priority = section.priority;
  if (section.required) priority += 10_000;
  if (section.pinned) priority += 5_000;
  if (section.provenance.trust === "system") priority += 2_000;
  if (section.provenance.trust === "verified") priority += 1_000;
  if (section.turnIndex !== null) {
    const maximumTurn = Math.max(0, ...all.map((item) => item.turnIndex ?? 0));
    priority += Math.max(0, 500 - (maximumTurn - section.turnIndex) * 50);
  }
  if (section.kind === "tool_use" || section.kind === "tool_result") priority += 250;
  return priority;
}

function sectionOrder(left: ContextSection, right: ContextSection): number {
  const kind = kindOrder(left.kind) - kindOrder(right.kind);
  if (kind !== 0) return kind;
  const turn = (left.turnIndex ?? -1) - (right.turnIndex ?? -1);
  if (turn !== 0) return turn;
  return left.sequence - right.sequence;
}

function kindOrder(value: ContextSection["kind"]): number {
  if (value === "system") return 0;
  if (value === "instruction") return 1;
  if (value === "notice") return 2;
  if (value === "memory") return 3;
  if (value === "message") return 4;
  if (value === "tool_use") return 5;
  if (value === "tool_result") return 6;
  return 7;
}

function formatSection(value: ContextSection): string {
  const title = value.title ? `## ${value.title}\n` : "";
  const provenance = value.provenance.source === "system" ? "" : `\n[source: ${value.provenance.source}:${value.provenance.sourceId}]`;
  return `${title}${value.text}${provenance}`.trim();
}

function toMessages(sections: readonly ContextSection[]): JsonObject[] {
  const messages: JsonObject[] = [];
  for (const section of sections) {
    const role = section.provenance.source === "assistant" ? "assistant" : section.provenance.source === "tool" ? "tool" : "user";
    const previous = messages.at(-1);
    if (previous?.role === role && section.kind === "message") {
      previous.content = `${asString(previous.content)}\n\n${formatSection(section)}`;
    } else {
      messages.push({
        role,
        content: formatSection(section),
        section_id: section.sectionId,
        ...(section.toolPairId ? { tool_pair_id: section.toolPairId } : {}),
      });
    }
  }
  return messages;
}

function sectionInputFromJson(value: JsonObject): Parameters<ContextAssemblyRuntime["add"]>[0] {
  const provenance = asObject(value.provenance);
  return {
    sectionId: asString(value.section_id) || undefined,
    kind: contextKind(asString(value.kind, "message")),
    title: asString(value.title),
    content: value.content ?? asString(value.text),
    text: asString(value.text),
    priority: number(value.priority, 0),
    pinned: asBoolean(value.pinned, false),
    required: asBoolean(value.required, false),
    disclosure: disclosure(asString(value.disclosure, "model_only")),
    toolPairId: asString(value.tool_pair_id) || null,
    turnIndex: value.turn_index === null || value.turn_index === undefined ? null : positive(value.turn_index, 0),
    provenance: {
      source: contextSource(asString(provenance.source, "user")),
      sourceId: asString(provenance.source_id, "unknown"),
      sourceDigest: asString(provenance.source_digest),
      parentSectionId: asString(provenance.parent_section_id) || null,
      trust: contextTrust(asString(provenance.trust, "internal")),
      createdAt: asString(provenance.created_at, new Date().toISOString()),
      expiresAt: asString(provenance.expires_at) || null,
    },
    metadata: asObject(value.metadata),
  };
}

function sectionToJson(value: ContextSection): JsonObject {
  return {
    section_id: value.sectionId,
    kind: value.kind,
    title: value.title,
    content: value.content,
    text: value.text,
    priority: value.priority,
    pinned: value.pinned,
    required: value.required,
    disclosure: value.disclosure,
    tool_pair_id: value.toolPairId,
    turn_index: value.turnIndex,
    sequence: value.sequence,
    provenance: provenanceToJson(value.provenance),
    metadata: value.metadata,
    token_estimate: value.tokenEstimate,
    content_digest: value.contentDigest,
    state: value.state,
  };
}

function provenanceToJson(value: ContextProvenance): JsonObject {
  return {
    source: value.source,
    source_id: value.sourceId,
    source_digest: value.sourceDigest,
    parent_section_id: value.parentSectionId,
    trust: value.trust,
    created_at: value.createdAt,
    expires_at: value.expiresAt,
  };
}

function assemblyToJson(value: AssembledContext): JsonObject {
  return {
    assembly_id: value.assemblyId,
    revision: value.revision,
    selected: value.selected.map(sectionToJson),
    dropped: value.dropped.map(sectionToJson),
    redacted: value.redacted.map(sectionToJson),
    selected_tokens: value.selectedTokens,
    dropped_tokens: value.droppedTokens,
    budget_tokens: value.budgetTokens,
    pair_invariant_ok: value.pairInvariantOk,
    system_prompt: value.systemPrompt,
    messages: value.messages,
    disclosure_digest: value.disclosureDigest,
    context_digest: value.contextDigest,
    assembled_at: value.assembledAt,
  };
}

function planToJson(value: ContextWindowPlan): JsonObject {
  return {
    selected: value.selected.map(segmentToJson),
    dropped: value.dropped.map(segmentToJson),
    selected_tokens: value.selectedTokens,
    dropped_tokens: value.droppedTokens,
    budget_tokens: value.budgetTokens,
    pair_invariant_ok: value.pairInvariantOk,
    digest: value.digest,
  };
}

function segmentToJson(value: ContextWindowSegment): JsonObject {
  return {
    id: value.id,
    kind: value.kind,
    content: value.content,
    pinned: value.pinned,
    tool_pair_id: value.toolPairId,
    priority: value.priority,
    created_sequence: value.createdSequence,
    metadata: value.metadata,
  };
}

function policyToJson(value: ContextPolicy): JsonObject {
  return {
    maximum_tokens: value.maximumTokens,
    reserved_output_tokens: value.reservedOutputTokens,
    reserved_tool_tokens: value.reservedToolTokens,
    maximum_section_tokens: value.maximumSectionTokens,
    maximum_memory_tokens: value.maximumMemoryTokens,
    maximum_attachment_tokens: value.maximumAttachmentTokens,
    minimum_recent_turns: value.minimumRecentTurns,
    include_untrusted: value.includeUntrusted,
    include_external: value.includeExternal,
    allow_secrets_to_model: value.allowSecretsToModel,
    redact_patterns: value.redactPatterns,
  };
}

function contentText(value: JsonValue): string {
  if (typeof value === "string") return value;
  return canonicalJson(value);
}

function segmentKind(value: ContextSection["kind"]): ContextWindowSegment["kind"] {
  if (value === "system" || value === "instruction" || value === "notice") return "system";
  if (value === "tool_use") return "tool_use";
  if (value === "tool_result") return "tool_result";
  if (value === "attachment") return "attachment";
  return "user";
}

function contextKind(value: string): ContextSection["kind"] {
  if (value === "system" || value === "instruction" || value === "tool_use" || value === "tool_result" || value === "memory" || value === "attachment" || value === "notice") return value;
  return "message";
}

function contextSource(value: string): ContextSource {
  if (value === "system" || value === "assistant" || value === "tool" || value === "memory" || value === "skill" || value === "workspace" || value === "artifact" || value === "hook" || value === "control" || value === "restore") return value;
  return "user";
}

function contextTrust(value: string): ContextTrust {
  if (value === "untrusted" || value === "external" || value === "verified" || value === "system") return value;
  return "internal";
}

function disclosure(value: string): ContextDisclosure {
  if (value === "public" || value === "worker_only" || value === "secret") return value;
  return "model_only";
}

function defaultRedactPatterns(): string[] {
  return [
    "sk-ant-[A-Za-z0-9_-]{16,}",
    "Bearer\\s+[A-Za-z0-9._~+/-]{16,}",
    "AKIA[0-9A-Z]{16}",
    "gh[pousr]_[A-Za-z0-9]{20,}",
    "-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\\s\\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
  ];
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : fallback;
}

function number(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function bounded(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
}

function normalizeTimestamp(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid context timestamp: ${value}`);
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
